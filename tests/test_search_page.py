"""Rejected exits still search over HTTP; never relax the requested intent."""
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

import swoop
from swoop import flights_pb2 as PB
from swoop import rpc
from swoop._search_page import _page_payload
from swoop.decoder import detect_error_envelope
from swoop.exceptions import SwoopHTTPError, SwoopParseError, SwoopRateLimitError, SwoopUpstreamError


@pytest.fixture
def rejected_exit(monkeypatch):
    payload = json.loads((Path(__file__).parent / "fixtures/responses/shopping_oneway.json").read_text())
    class Client:
        gets = []
        posts = []
        status = 200
        html = "<script>AF_initDataCallback({key: 'ds:1', hash:'1', data:" + json.dumps(payload) + ", sideChannel: {}});</script>"
        rejection = ['wrb.fr', None, None, None, None, [13]]
        def post(self, *args, **kwargs):
            self.posts.append((args, kwargs))
            return SimpleNamespace(status_code=200, text=json.dumps([self.rejection]))
        def get(self, url, **kwargs):
            self.gets.append((url, kwargs))
            return SimpleNamespace(status_code=self.status, text=self.html)
    client = Client()
    monkeypatch.setattr(rpc, '_get_client', lambda *args: client)
    return client


def test_compact_rpc_rejection_uses_page_and_preserves_intent(rejected_exit):
    result = swoop.search('JFK', 'LAX', '2026-11-15', cabin='business',
                          passengers=swoop.Passengers(adults=2), max_stops=0, airlines=['AA'],
                          transport=swoop.TransportConfig(country='US', timeout=17))
    assert result.results
    assert result.result_scope == "limited"
    assert result.truncation_reasons == ["limited_transport"]
    assert result.is_complete is False
    assert len(rejected_exit.gets) == 1
    url, kwargs = rejected_exit.gets[0]
    params = parse_qs(urlparse(url).query)
    info = PB.Info.FromString(base64.b64decode(params['tfs'][0]))
    assert info.seat == PB.Seat.BUSINESS
    assert list(info.passengers) == [PB.Passenger.ADULT] * 2
    assert info.data[0].date == '2026-11-15'
    assert info.data[0].max_stops == 0
    assert info.data[0].HasField('max_stops')
    assert list(info.data[0].airlines) == ['AA']
    assert params['gl'] == ['US']
    assert kwargs['timeout'] == 17


def test_default_basic_exclusion_is_encoded(rejected_exit):
    swoop.search('JFK', 'LAX', '2026-11-15')
    params = parse_qs(urlparse(rejected_exit.gets[0][0]).query)
    info = PB.Info.FromString(base64.b64decode(params['tfs'][0]))
    assert info.exclude_basic_economy


@pytest.mark.parametrize('excluded', [False, True])
def test_separate_ticket_filter_survives_rpc_page_fallback(rejected_exit, excluded):
    result = swoop.search('JFK', 'LAX', '2026-11-15', exclude_separate_tickets=excluded)
    params = parse_qs(urlparse(rejected_exit.gets[0][0]).query)
    info = PB.Info.FromString(base64.b64decode(params['tfs'][0]))
    assert info.exclude_separate_tickets is excluded
    from swoop._selection import decode_trip_selector
    assert decode_trip_selector(result.results[0].selector)['exclude_separate_tickets'] is excluded


def test_roundtrip_encodes_both_dates(rejected_exit):
    swoop.search('JFK', 'LAX', '2026-11-15', return_date='2026-11-22')
    params = parse_qs(urlparse(rejected_exit.gets[0][0]).query)
    info = PB.Info.FromString(base64.b64decode(params['tfs'][0]))
    assert info.trip == PB.Trip.ROUND_TRIP
    assert [leg.date for leg in info.data] == ['2026-11-15', '2026-11-22']


@pytest.mark.parametrize('origin,destination', [
    (['JFK', 'EWR'], 'LAX'), ('JFK', ['LAX', 'BUR']),
    (['JFK', 'EWR'], ['LAX', 'BUR']),
])
def test_multi_airport_rpc_rejection_preserves_native_page_route(rejected_exit, origin, destination):
    origins = origin if isinstance(origin, list) else [origin]
    destinations = destination if isinstance(destination, list) else [destination]
    result = swoop.search_raw(
        origin, destination, '2026-10-26', max_stops=0, cabin='business',
        passengers=swoop.Passengers(adults=2, children=1, infants_in_seat=1, infants_on_lap=1),
        airlines=['UA', 'AA'], exclude_basic_economy=True, exclude_separate_tickets=True,
        transport=swoop.TransportConfig(country='US', timeout=17),
    )
    assert result.best or result.other
    assert result._result_scope == 'limited'
    assert len(rejected_exit.posts) == len(rejected_exit.gets) == 1
    url, kwargs = rejected_exit.gets[0]
    params = parse_qs(urlparse(url).query)
    info = PB.Info.FromString(base64.b64decode(params['tfs'][0]))
    assert len(info.data) == 1  # endpoint alternatives are one native pass
    assert [entry.airport for entry in info.data[0].from_flight] == origins
    assert [entry.airport for entry in info.data[0].to_flight] == destinations
    assert info.data[0].date == '2026-10-26'
    assert info.data[0].HasField('max_stops') and info.data[0].max_stops == 0
    assert list(info.data[0].airlines) == ['AA', 'UA']
    assert info.seat == PB.Seat.BUSINESS and info.trip == PB.Trip.ONE_WAY
    assert list(info.passengers) == [1, 1, 2, 3, 4]
    assert info.exclude_basic_economy and info.exclude_separate_tickets
    assert params['gl'] == ['US'] and kwargs['timeout'] == 17


def test_actual_alternative_origin_survives_page_decode(rejected_exit):
    fixture = json.loads((Path(__file__).parent /
                          'fixtures/tfs/shopping_page_airport_alternatives.json').read_text())
    rejected_exit.html = "AF_initDataCallback({key:'ds:1',data:" + json.dumps(
        fixture['sanitized_page_payload']) + ",sideChannel:{}});"
    result = swoop.search_raw(fixture['origin_airports'], fixture['destination_airports'],
                              fixture['departure_date'], max_stops=0)
    rows = [*result.best, *result.other]
    assert {(row.departure_airport_code, row.arrival_airport_code) for row in rows} == {
        ('JFK', 'LAX'), ('EWR', 'LAX')}
    assert {(row.segments[0].departure_airport_code, row.segments[-1].arrival_airport_code)
            for row in rows} == {('JFK', 'LAX'), ('EWR', 'LAX')}
    assert all(row.departure_date == (2026, 10, 26) for row in rows)
    assert all(row.booking_token == '' for row in rows)
    assert result._result_scope == 'limited'
    assert len(rejected_exit.posts) == len(rejected_exit.gets) == 1


def test_roundtrip_airport_alternatives_stay_two_legs(rejected_exit):
    origins, destinations = ['JFK', 'EWR', 'LGA'], ['LAX', 'BUR', 'SNA']
    result = swoop.search_raw(origins, destinations, '2026-10-26', return_date='2026-11-02',
                              airlines=['AA'], max_stops=0, exclude_basic_economy=True,
                              exclude_separate_tickets=True)
    assert result._result_scope == 'limited'
    assert len(rejected_exit.posts) == len(rejected_exit.gets) == 1
    params = parse_qs(urlparse(rejected_exit.gets[0][0]).query)
    info = PB.Info.FromString(base64.b64decode(params['tfs'][0]))
    assert info.trip == PB.Trip.ROUND_TRIP and len(info.data) == 2
    assert [leg.date for leg in info.data] == ['2026-10-26', '2026-11-02']
    assert [[entry.airport for entry in leg.from_flight] for leg in info.data] == [origins, destinations]
    assert [[entry.airport for entry in leg.to_flight] for leg in info.data] == [destinations, origins]
    assert all(leg.HasField('max_stops') and leg.max_stops == 0 and list(leg.airlines) == ['AA']
               for leg in info.data)
    assert info.exclude_basic_economy and info.exclude_separate_tickets
    assert origins == ['JFK', 'EWR', 'LGA'] and destinations == ['LAX', 'BUR', 'SNA']


def test_singleton_airport_lists_preserve_the_page_intent(rejected_exit):
    origins, destinations = ['JFK'], ['LAX']
    result = swoop.search_raw(origins, destinations, '2026-11-15', cabin='business',
                              passengers=swoop.Passengers(adults=2), max_stops=0,
                              airlines=['AA'], return_date='2026-11-22')
    assert result.best or result.other
    assert len(rejected_exit.posts) == len(rejected_exit.gets) == 1
    params = parse_qs(urlparse(rejected_exit.gets[0][0]).query)
    info = PB.Info.FromString(base64.b64decode(params['tfs'][0]))
    assert [(leg.from_flight[0].airport, leg.to_flight[0].airport, leg.date) for leg in info.data] == [
        ('JFK', 'LAX', '2026-11-15'), ('LAX', 'JFK', '2026-11-22')]
    assert info.seat == PB.Seat.BUSINESS
    assert list(info.passengers) == [PB.Passenger.ADULT] * 2
    assert all(leg.HasField('max_stops') and leg.max_stops == 0 and list(leg.airlines) == ['AA'] for leg in info.data)
    assert origins == ['JFK'] and destinations == ['LAX']


@pytest.mark.parametrize('origin', ['JFK', ['JFK', 'EWR']])
@pytest.mark.parametrize('leg_changes', [
    {'earliest_departure': 6}, {'latest_arrival': 23},
    {'selected_legs': [['JFK', '2026-11-15', 'LAX', None, 'AA', '1']]},
])
def test_unsupported_constraints_remain_errors(rejected_exit, leg_changes, origin):
    leg = rpc._normalize_rpc_leg(origin, 'LAX', '2026-11-15')
    leg.update(leg_changes)
    with pytest.raises(SwoopUpstreamError):
        rpc._search_from_legs([leg])
    assert not rejected_exit.gets


def test_multicity_does_not_become_oneway(rejected_exit):
    legs = [rpc._normalize_rpc_leg('JFK', 'LAX', '2026-11-15'),
            rpc._normalize_rpc_leg('LAX', 'SFO', '2026-11-22')]
    with pytest.raises(SwoopUpstreamError):
        rpc._search_from_legs(legs)
    assert not rejected_exit.gets


@pytest.mark.parametrize('status,error', [(429, SwoopRateLimitError), (503, SwoopHTTPError)])
def test_page_http_errors_are_not_empty_results(rejected_exit, status, error):
    rejected_exit.status = status
    with pytest.raises(error):
        swoop.search('JFK', 'LAX', '2026-11-15')
    assert len(rejected_exit.gets) == 1


@pytest.mark.parametrize('html', ['<html>consent</html>', "AF_initDataCallback({key:'ds:1', data:not-json});"])
def test_missing_or_malformed_page_data_fails(rejected_exit, html):
    rejected_exit.html = html
    with pytest.raises(SwoopParseError):
        swoop.search('JFK', 'LAX', '2026-11-15')


@pytest.mark.parametrize('status,html,error', [
    (429, '', SwoopRateLimitError), (503, '', SwoopHTTPError),
    (200, '<html>consent</html>', SwoopParseError),
    (200, "AF_initDataCallback({key:'ds:1',data:not-json});", SwoopParseError),
    (200, "AF_initDataCallback({key:'ds:1',data:[13,null,null,null]});", SwoopUpstreamError),
])
def test_alternative_airport_fallback_errors_keep_one_request_budget(rejected_exit, status, html, error):
    rejected_exit.status, rejected_exit.html = status, html
    with pytest.raises(error):
        swoop.search_raw(['JFK', 'EWR'], ['LAX', 'BUR'], '2026-10-26')
    assert len(rejected_exit.posts) == len(rejected_exit.gets) == 1


def test_empty_alternative_airport_page_stays_limited_empty(rejected_exit):
    rejected_exit.html = "AF_initDataCallback({key:'ds:1',data:[null,null,null,null]});"
    result = swoop.search_raw(['JFK', 'EWR'], ['LAX', 'BUR'], '2026-10-26')
    assert result.best == result.other == []
    assert result._result_scope == 'limited'
    assert len(rejected_exit.posts) == len(rejected_exit.gets) == 1


def test_page_rejection_is_not_inventory():
    with pytest.raises(SwoopUpstreamError):
        _page_payload("AF_initDataCallback({key:'ds:1', data:[13,null,[]], sideChannel:{}});")


@pytest.mark.parametrize('status', [7, 8, 14, 16])
def test_other_rpc_errors_do_not_trigger_page_fetch(rejected_exit, status):
    rejected_exit.rejection = ['wrb.fr', None, None, None, None, [status]]
    with pytest.raises(SwoopUpstreamError) as exc:
        swoop.search('JFK', 'LAX', '2026-11-15')
    assert exc.value.grpc_code == status
    assert not rejected_exit.gets


def test_detailed_internal_error_does_not_trigger_page_fetch(rejected_exit):
    rejected_exit.rejection = ['wrb.fr', None, None, None, None,
                              [13, None, [['type.googleapis.com/travel.frontend.flights.ErrorResponse']]]]
    with pytest.raises(SwoopUpstreamError):
        swoop.search('JFK', 'LAX', '2026-11-15')
    assert not rejected_exit.gets


@pytest.mark.parametrize('frame', [
    ['wrb.fr', None, '[]', None, None, [13]],
    ['other', None, None, None, None, [13]],
    ['wrb.fr', None, None, None, None, [True]],
    ['wrb.fr', None, None, None, None, [0]],
])
def test_status_detection_does_not_mistake_payload_or_metadata_for_error(frame):
    assert detect_error_envelope(frame) is None
