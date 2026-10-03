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
