"""Regression #2149: Google checkbox exclusion is native RPC slot 5 / TFS field 7."""
import base64
from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import unquote

import pytest
import swoop
import swoop._selection as selection
from swoop import flights_pb2 as PB
from swoop._airlines import normalize_exclusions
from swoop.builders import TFSData, SearchLeg, _PBPassengers
from swoop.decoder import RawSearchResult
from swoop.models import Passengers
from swoop.rpc import _build_segment_from_leg, _normalize_rpc_leg
from tests.factories import make_simple_itinerary

CAPTURE = json.loads((Path(__file__).parent / 'fixtures/airline-exclusion-checkbox.json').read_text())
LEGS = [_normalize_rpc_leg('JFK', 'LAX', '2026-12-15'), _normalize_rpc_leg('LAX', 'JFK', '2026-12-22')]

def flight(airline='AA', returning=False, number='117'):
    return make_simple_itinerary(origin='LAX' if returning else 'JFK', destination='JFK' if returning else 'LAX',
        date='2026-12-22' if returning else '2026-12-15', airline=airline, flight_number=number, booking_token='')

def test_regression_2149_matches_actual_unchecked_airline_wire():
    assert CAPTURE['transition'] == {'carrier': 'AA', 'before_checked': True, 'after_checked': False}
    query = _normalize_rpc_leg('JFK', 'LAX', '2026-12-15', exclude_airlines=['AA'])
    expected = CAPTURE['rpc_legs']['exclude-american']
    assert _build_segment_from_leg(query) == expected
    captured = PB.Info()
    encoded_capture = unquote(CAPTURE['tfs_excluded_url'])
    captured.ParseFromString(base64.urlsafe_b64decode(encoded_capture + '=' * (-len(encoded_capture) % 4)))
    assert list(captured.data[0].exclude_airlines) == ['AA']
    assert captured.data[0].DESCRIPTOR.fields_by_name['exclude_airlines'].number == 7
    encoded = TFSData.from_interface(flight_data=[SearchLeg(date=CAPTURE['date'], from_airport='JFK', to_airport='LAX', exclude_airlines=['AA'])],
        trip='one-way', seat='economy', passengers=_PBPassengers()).pb()
    assert list(encoded.data[0].exclude_airlines) == list(captured.data[0].exclude_airlines)

@pytest.mark.parametrize('excluded', ['AA', [None], ['STAR_ALLIANCE'], ['AAA'], [True], ['A!']])
def test_regression_2149_invalid_exclusions_fail_before_provider_io(monkeypatch, excluded):
    monkeypatch.setattr(selection, '_search_from_legs', lambda *a, **k: pytest.fail('provider called'))
    with pytest.raises(ValueError):
        swoop.search('JFK', 'LAX', CAPTURE['date'], exclude_airlines=excluded)

def test_regression_2149_include_exclude_intersection_fails_closed(monkeypatch):
    assert normalize_exclusions(['aa', 'AA', 'DL']) == ['AA', 'DL']
    monkeypatch.setattr(selection, '_search_from_legs', lambda *a, **k: pytest.fail('provider called'))
    with pytest.raises(ValueError):
        swoop.search('JFK', 'LAX', CAPTURE['date'], airlines=['AA'], exclude_airlines=['AA'])

def test_regression_2149_search_and_explicit_legs_preserve_exclusion_context(monkeypatch):
    calls = []
    def provider(legs, **kwargs):
        calls.append(deepcopy(legs)); return RawSearchResult(other=[flight('DL')])
    monkeypatch.setattr(selection, '_search_from_legs', provider)
    for result in [swoop.search('JFK', 'LAX', CAPTURE['date'], exclude_airlines=['AA']),
        swoop.search_legs([SearchLeg(date=CAPTURE['date'], from_airport='JFK', to_airport='LAX', exclude_airlines=['AA'])])]:
        assert selection.decode_trip_selector(result.results[0].selector)['query_legs'][0]['exclude_airlines'] == ['AA']
    assert all(call[0]['exclude_airlines'] == ['AA'] for call in calls)

def test_regression_2149_mixed_carrier_exclusion_admission_is_not_silent_truncation(monkeypatch):
    mixed = flight('DL')
    mixed.segments[0].arrival_airport_code = 'ORD'
    connection = make_simple_itinerary(origin='ORD', destination='LAX', date=CAPTURE['date'], airline='AA', flight_number='118')
    mixed.segments.append(connection.segments[0])
    monkeypatch.setattr(selection, '_search_from_legs', lambda *a, **k: RawSearchResult(other=[mixed, flight('DL')]))
    result = swoop.search('JFK', 'LAX', CAPTURE['date'], exclude_airlines=['AA'])
    assert len(result.results) == 1
    assert not result.is_complete and 'airline_exclusion_mismatch' in result.truncation_reasons

def test_regression_2149_new_return_exclusion_leaves_selected_outbound_untouched_and_prices(monkeypatch):
    outbound, inbound = flight('AA'), flight('DL', returning=True)
    selector = selection.encode_trip_selector(request_legs=LEGS, itineraries=[outbound], cabin='business',
        passengers=Passengers(adults=2), include_basic_economy=True, exclude_separate_tickets=True)
    calls = []
    def provider(legs, **kwargs):
        calls.append((deepcopy(legs), kwargs))
        return RawSearchResult(other=[inbound if legs[0].get('selected_legs') else outbound])
    monkeypatch.setattr(selection, '_search_from_legs', provider)
    result = swoop.search_next_leg(selector, next_leg_filters={'exclude_airlines': ['AA']})
    assert len(result.results) == 1
    payload = selection.decode_trip_selector(result.results[0].selector)
    assert 'exclude_airlines' not in payload['query_legs'][0]
    assert payload['query_legs'][1]['exclude_airlines'] == ['AA']
    assert calls[0][0][1].get('exclude_airlines') is None  # replay uses saved intent
    assert calls[1][0][1]['exclude_airlines'] == ['AA']
    assert swoop.price_selector(result.results[0].selector) is not None
    assert calls[-1][0][1]['exclude_airlines'] == ['AA']
    assert calls[-1][1]['passengers'] == Passengers(adults=2)

def test_regression_2149_exclusions_accumulate_without_clearing_old_constraints():
    from swoop._continuation import narrow_next_leg
    old = dict(LEGS[1], exclude_airlines=['AA'])
    assert narrow_next_leg(old, {'exclude_airlines': ['DL']})['exclude_airlines'] == ['AA', 'DL']
    assert narrow_next_leg(old, {'exclude_airlines': None})['exclude_airlines'] == ['AA']

@pytest.mark.parametrize('method', [swoop.calendar_prices, swoop.get_calendar, swoop.get_price_graph])
def test_regression_2149_calendar_preserves_native_negative_filter(monkeypatch, method):
    from tests.test_calendar import FIX, _serve, _payload
    calls = _serve(monkeypatch, (FIX / 'oneway_pty_cdg_business.txt').read_text())
    method('PTY', 'CDG', '2026-12-15', '2026-12-20', airlines=['AA', 'DL'], exclude_airlines=['AA'])
    filters = _payload(calls[0][1])[1]
    assert filters[13][0][4:6] == [['AA', 'DL'], ['AA']]

@pytest.mark.parametrize('arguments', [
    ['search', 'JFK', 'LAX', '2026-12-15'],
    ['search', '--leg', 'JFK', 'LAX', '2026-12-15'],
])
def test_regression_2149_cli_exclusion_reaches_sdk(monkeypatch, arguments):
    from click.testing import CliRunner
    from swoop.cli import main
    calls = []
    def provider(legs, **kwargs):
        calls.append(legs); return RawSearchResult(other=[flight('DL')])
    monkeypatch.setattr(selection, '_search_from_legs', provider)
    result = CliRunner().invoke(main, [*arguments, '--exclude-airline', 'AA', '-o', 'json', '-q'])
    assert result.exit_code == 0, result.output
    assert calls[0][0]['exclude_airlines'] == ['AA']
