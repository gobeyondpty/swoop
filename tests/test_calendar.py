"""Offline tests for calendar_prices() (GetCalendarGrid)."""

import json
import urllib.parse
from pathlib import Path

import pytest

import swoop
from swoop import CalendarPrice, SwoopParseError, SwoopUpstreamError
from swoop import _calendar

FIX = Path(__file__).parent / "fixtures" / "responses" / "calendar"


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text


def _serve(monkeypatch: pytest.MonkeyPatch, text: str) -> list[tuple[str, bytes]]:
    calls: list[tuple[str, bytes]] = []

    def fake_post(url: str, content: bytes, *, transport: object) -> _Response:
        calls.append((url, content))
        return _Response(text)

    monkeypatch.setattr(_calendar, "_http_post", fake_post)
    return calls


def _payload(body: bytes) -> list:
    f_req = urllib.parse.unquote(body.decode().removeprefix("f.req="))
    return json.loads(json.loads(f_req)[1])


def test_roundtrip_grid_returns_every_date_combination(monkeypatch: pytest.MonkeyPatch) -> None:
    # Google streams the grid as many wrb.fr entries on one line. Reading only
    # the first entry yields a single cell out of 36.
    _serve(monkeypatch, (FIX / "roundtrip_pty_cdg_business.txt").read_text())

    prices = swoop.calendar_prices(
        "PTY", "CDG", "2026-10-10", "2026-10-15",
        return_start="2026-11-10", return_end="2026-11-15", cabin="business",
    )

    assert len(prices) == 36
    assert {(p.departure_date, p.return_date) for p in prices} == {
        (f"2026-10-{out}", f"2026-11-{back}") for out in range(10, 16) for back in range(10, 16)
    }
    assert all(p.price > 0 and p.currency == "USD" for p in prices)


def test_oneway_window_has_no_return_dates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _serve(monkeypatch, (FIX / "oneway_pty_cdg_business.txt").read_text())

    prices = swoop.calendar_prices("PTY", "CDG", "2026-10-10", "2026-10-15", cabin="business")

    assert prices[0] == CalendarPrice("2026-10-10", None, 1891, "USD")
    assert [p.departure_date for p in prices] == [f"2026-10-{day}" for day in range(10, 16)]
    payload = _payload(calls[0][1])
    assert payload[0] is None and payload[2:] == [["2026-10-10", "2026-10-15"]]


def test_request_reuses_the_shopping_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _serve(monkeypatch, (FIX / "roundtrip_pty_cdg_business.txt").read_text())

    swoop.calendar_prices(
        "PTY", "CDG", "2026-10-10", "2026-10-15", return_start="2026-11-10", return_end="2026-11-15",
        cabin="business", max_stops=1, airlines=["AF"], exclude_separate_tickets=True,
    )

    url, body = calls[0]
    assert url.endswith("FlightsFrontendService/GetCalendarGrid")
    expected = swoop.rpc._build_filters_from_legs(
        [
            swoop.rpc._normalize_rpc_leg("PTY", "CDG", "2026-10-10", max_stops=1, airlines=["AF"]),
            swoop.rpc._normalize_rpc_leg("CDG", "PTY", "2026-11-10", max_stops=1, airlines=["AF"]),
        ],
        cabin="business", exclude_separate_tickets=True,
    )[1]
    assert _payload(body) == [None, expected, ["2026-10-10", "2026-10-15"], ["2026-11-10", "2026-11-15"]]


def test_half_a_return_window_is_rejected_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _serve(monkeypatch, "")
    with pytest.raises(ValueError):
        swoop.calendar_prices("PTY", "CDG", "2026-10-10", "2026-10-15", return_start="2026-11-10")
    assert calls == []


def test_error_envelope_and_unparseable_responses_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    envelope = (Path(__file__).parent / "fixtures" / "responses" / "explore" / "error_response.txt").read_text()
    _serve(monkeypatch, envelope)
    with pytest.raises(SwoopUpstreamError):
        swoop.calendar_prices("PTY", "CDG", "2026-10-10", "2026-10-15")

    _serve(monkeypatch, "<!doctype html><title>Before you continue</title>")
    with pytest.raises(SwoopParseError):
        swoop.calendar_prices("PTY", "CDG", "2026-10-10", "2026-10-15")


@pytest.mark.parametrize('method', [swoop.calendar_prices, swoop.get_calendar, swoop.get_price_graph])
def test_regression_2981_calendar_airport_alternatives_use_one_rpc(monkeypatch, method):
    calls = _serve(monkeypatch, (FIX / 'oneway_pty_cdg_business.txt').read_text())
    origin, destination = ['JFK', 'EWR'], ['LHR', 'LGW']
    result = method(origin, destination, '2026-10-10', '2026-10-15')
    assert result
    assert len(calls) == 1
    expected = swoop.rpc._build_filters_from_legs([
        swoop.rpc._normalize_rpc_leg(origin, destination, '2026-10-10'),
    ])[1]
    assert _payload(calls[0][1])[1] == expected


@pytest.mark.parametrize('endpoint', [[], ['JFK', 'JFK'], ['NYC'], ['jfk'], ['JFK'] * 9])
def test_regression_2981_invalid_calendar_airport_alternatives_fail_before_rpc(monkeypatch, endpoint):
    calls = _serve(monkeypatch, '')
    with pytest.raises(ValueError):
        swoop.calendar_prices(endpoint, 'PTY', '2026-10-10', '2026-10-15')
    assert calls == []


@pytest.mark.parametrize('method', [swoop.calendar_prices, swoop.get_calendar, swoop.get_price_graph])
def test_regression_2985_real_prices_survive_metadata_trailer_in_one_rpc(monkeypatch, method):
    # Archive call01M5W8FGF0G3478CBM9NY442MD, production2026-10-26.
    # Dates/prices/frame structure retained; selectors and metadata capabilities redacted.
    calls = _serve(monkeypatch, (FIX / 'picker_jfk_pty_trailer_2985.txt').read_text())
    result = method('JFK', 'PTY', '2026-10-26', '2027-04-30')
    days = result.days if isinstance(result, swoop.CalendarResult) else result
    assert len(calls) == 1
    assert len(days) == 187
    assert days[0].departure_date == '2026-10-26'
    assert days[-1].departure_date == '2027-04-30'
    assert min(day.price for day in days) == 359
    assert max(day.price for day in days) == 781
    assert {day.currency for day in days} == {'USD'}


def test_regression_2985_metadata_alone_is_not_empty_calendar_success():
    frames = _calendar._calendar_frames((FIX / 'picker_jfk_pty_trailer_2985.txt').read_text())
    with pytest.raises(SwoopParseError, match='missing inner payload'):
        _calendar._parse_calendar_days(json.dumps([frames[1]]))


@pytest.mark.parametrize('tail', [[['unexpected']], [[], 'bad-fare-cells']])
def test_regression_2985_unknown_or_malformed_tail_still_fails(tail):
    frames = _calendar._calendar_frames((FIX / 'picker_jfk_pty_trailer_2985.txt').read_text())
    frames[1][2] = json.dumps(tail)
    with pytest.raises(SwoopParseError, match='missing price cells'):
        _calendar._parse_calendar_days(json.dumps(frames))


def test_regression_2985_upstream_error_after_valid_prices_still_fails():
    frames = _calendar._calendar_frames((FIX / 'picker_jfk_pty_trailer_2985.txt').read_text())
    error = (Path(__file__).parent / 'fixtures' / 'responses' / 'explore' / 'error_response.txt').read_text()
    frames.extend(_calendar._calendar_frames(error))
    with pytest.raises(SwoopUpstreamError):
        _calendar._parse_calendar_days(json.dumps(frames))


def test_regression_3037_streamed_grid_keeps_fares_before_numbered_trailer(monkeypatch):
    # Production DEN->LIM grid, 2026-10-30: one fare frame per cell (sequence
    # 0-16), then the completion frame numbered 17. Dates/prices/frame structure
    # retained; metadata tokens and booking selectors redacted.
    calls = _serve(monkeypatch, (FIX / 'grid_den_lim_streamed_3037.txt').read_text())
    days = swoop.calendar_prices('DEN', 'LIM', '2026-11-29', '2026-11-29',
                                 return_start='2026-11-29', return_end='2027-01-16')
    assert len(calls) == 1
    assert len(days) == 49
    assert {day.departure_date for day in days} == {'2026-11-29'}
    assert min(day.return_date for day in days) == '2026-11-29'
    assert max(day.return_date for day in days) == '2027-01-16'
    assert min(day.price for day in days) == 630
    assert max(day.price for day in days) == 1824
    assert {day.currency for day in days} == {'USD'}


@pytest.mark.parametrize('method', [swoop.calendar_prices, swoop.get_calendar, swoop.get_price_graph])
def test_regression_3048_departure_window_over_200_dates_fails_before_rpc(monkeypatch, method):
    # Live probes 2026-10-30: Google answers a 201+ date departure window with
    # gRPC 3 INVALID_ARGUMENT on grid, picker and graph; 200 dates are priced.
    calls = _serve(monkeypatch, '')
    with pytest.raises(ValueError, match='200 dates'):
        method('JFK', 'PTY', '2026-11-01', '2027-05-20')
    assert calls == []


def test_regression_3048_200_date_departure_window_is_sent(monkeypatch):
    calls = _serve(monkeypatch, (FIX / 'picker_jfk_pty_trailer_2985.txt').read_text())
    swoop.get_calendar('JFK', 'PTY', '2026-11-01', '2027-05-19')
    assert _payload(calls[0][1])[2] == ['2026-11-01', '2027-05-19']


def test_regression_3048_return_window_starts_at_first_departure(monkeypatch):
    # Production ALB-MIA 2026-10-26: returns from 2027-01-01 for a 2027-01-10
    # departure were rejected with gRPC 3 on every exit. Return dates before the
    # first departure have no fare, so they are omitted like any unpriced date.
    calls = _serve(monkeypatch, (FIX / 'roundtrip_pty_cdg_business.txt').read_text())
    swoop.calendar_prices('ALB', 'MIA', '2027-01-10', '2027-01-10',
                          return_start='2027-01-01', return_end='2027-02-18')
    payload = _payload(calls[0][1])
    assert payload[2:] == [['2027-01-10', '2027-01-10'], ['2027-01-10', '2027-02-18']]
    assert payload[1] == swoop.rpc._build_filters_from_legs([
        swoop.rpc._normalize_rpc_leg('ALB', 'MIA', '2027-01-10'),
        swoop.rpc._normalize_rpc_leg('MIA', 'ALB', '2027-01-10'),
    ])[1]


def test_regression_3048_return_window_before_departure_is_empty_without_rpc(monkeypatch):
    calls = _serve(monkeypatch, '')
    assert swoop.calendar_prices('JFK', 'PTY', '2027-01-10', '2027-01-12',
                                 return_start='2026-12-20', return_end='2027-01-09') == []
    assert calls == []
