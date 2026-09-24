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
        status = 200
        html = "<script>AF_initDataCallback({key: 'ds:1', hash:'1', data:" + json.dumps(payload) + ", sideChannel: {}});</script>"
        rejection = ['wrb.fr', None, None, None, None, [13]]
        def post(self, *args, **kwargs):
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


def test_roundtrip_encodes_both_dates(rejected_exit):
    swoop.search('JFK', 'LAX', '2026-11-15', return_date='2026-11-22')
    params = parse_qs(urlparse(rejected_exit.gets[0][0]).query)
    info = PB.Info.FromString(base64.b64decode(params['tfs'][0]))
    assert info.trip == PB.Trip.ROUND_TRIP
    assert [leg.date for leg in info.data] == ['2026-11-15', '2026-11-22']


@pytest.mark.parametrize('leg_changes', [
    {'earliest_departure': 6}, {'latest_arrival': 23},
    {'selected_legs': [['JFK', '2026-11-15', 'LAX', None, 'AA', '1']]},
])
def test_unsupported_constraints_remain_errors(rejected_exit, leg_changes):
    leg = rpc._normalize_rpc_leg('JFK', 'LAX', '2026-11-15')
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
