"""Offline calendar-picker integration checks alongside the calendar grid."""

import json

import pytest

import swoop
from swoop import rpc
from tests.factories import FakeHTTPResponse, decode_f_req, encode_rpc_outer, make_error_response


def _serve(monkeypatch, response):
    calls = []

    def post(url, *, content, transport):
        calls.append((url, content, transport))
        return FakeHTTPResponse(text=response)

    monkeypatch.setattr(rpc, "_http_post", post)
    return calls


def test_oneway_picker_sends_bytes_and_preserves_fares_and_transport(monkeypatch):
    response = encode_rpc_outer([None, [
        ["2027-01-10", None, [[None, 333], "selector-a"]],
        ["2027-01-11", None, [[None, 271], "selector-b"]],
    ]])
    calls = _serve(monkeypatch, response)
    transport = swoop.TransportConfig(timeout=12, country="PA")
    result = swoop.calendar(
        "PTY", "CDG", "2027-01-10", "2027-01-11", cabin="business",
        passengers=swoop.Passengers(adults=2, children=1, infants_in_seat=1, infants_on_lap=1),
        transport=transport,
    )

    assert result.days == [
        swoop.CalendarDay("2027-01-10", None, 333, selector="selector-a"),
        swoop.CalendarDay("2027-01-11", None, 271, selector="selector-b"),
    ]
    assert (result.min_price, result.max_price, result.currency) == (271, 333, None)
    url, content, used_transport = calls[0]
    assert url.endswith("FlightsFrontendService/GetCalendarPicker")
    assert isinstance(content, bytes)
    assert used_transport is transport
    payload = decode_f_req(content.decode().removeprefix("f.req="))
    assert payload[1][5:7] == [3, [2, 1, 1, 1]]
    assert payload[1][13] == [[[[["PTY", 0]]], [[["CDG", 0]]], None, 0]]
    assert payload[1][17] == 2
    assert payload[2:] == [["2027-01-10", "2027-01-11"], None, None]


@pytest.mark.parametrize("max_stay, expected", [(None, [7, 7]), (10, [7, 10])])
def test_roundtrip_picker_keeps_stay_range_and_return_dates(monkeypatch, max_stay, expected):
    calls = _serve(monkeypatch, encode_rpc_outer([None, [
        ["2027-01-10", "2027-01-17", [[None, 540], "roundtrip-selector"]],
    ]]))
    result = swoop.calendar("PTY", "CDG", "2027-01-10", "2027-01-11", min_stay=7, max_stay=max_stay)
    payload = decode_f_req(calls[0][1].decode().removeprefix("f.req="))
    assert payload[1][17] == 1
    assert payload[1][13][1][:2] == [[[["CDG", 0]]], [[["PTY", 0]]]]
    assert payload[4] == expected
    assert result.days[0].return_date == "2027-01-17"
    assert result.days[0].price == 540


@pytest.mark.parametrize("framed", [False, True])
def test_picker_parses_rpc_framing_and_skips_unpriced_dates(framed):
    response = encode_rpc_outer([None, [
        ["2027-01-10", None, [[None, 300], "selector"]],
        ["2027-01-11", None, None],
        None,
    ]])
    if framed:
        outer = response.removeprefix(")]}'")
        response = ")]}'\n\n" + str(len(outer)) + "\n" + outer
    result = rpc._parse_calendar_response(response, currency="USD")
    assert result.days == [swoop.CalendarDay("2027-01-10", None, 300, "USD", "selector")]


def test_empty_picker_result_has_no_price_range():
    result = rpc._parse_calendar_response(encode_rpc_outer([None, []]))
    assert result.days == []
    assert (result.min_price, result.max_price, result.currency) == (None, None, None)


@pytest.mark.parametrize("framed", [False, True])
def test_picker_reports_upstream_rejection(monkeypatch, framed):
    response = make_error_response(13)
    if framed:
        outer = response.removeprefix(")]}'")
        response = ")]}'\n\n" + str(len(outer)) + "\n" + outer
    _serve(monkeypatch, response)
    with pytest.raises(swoop.SwoopUpstreamError) as raised:
        swoop.get_calendar("PTY", "CDG", "2027-01-10", "2027-01-11")
    assert raised.value.grpc_code == 13


@pytest.mark.parametrize("response", ["<html>consent</html>", json.dumps([["wrb.fr", None, "invalid-json"]])])
def test_picker_rejects_unparseable_responses(response):
    with pytest.raises(swoop.SwoopParseError):
        rpc._parse_calendar_response(response)


@pytest.mark.parametrize("args, kwargs", [
    (("bad", "CDG", "2027-01-10", "2027-01-11"), {}),
    (("PTY", "CDG", "invalid", "2027-01-11"), {}),
    (("PTY", "CDG", "2027-01-11", "2027-01-10"), {}),
    (("PTY", "CDG", "2027-01-10", "2027-01-11"), {"min_stay": -1}),
    (("PTY", "CDG", "2027-01-10", "2027-01-11"), {"max_stay": -1}),
    (("PTY", "CDG", "2027-01-10", "2027-01-11"), {"min_stay": 7, "max_stay": 3}),
    (("PTY", "CDG", "2027-01-10", "2027-01-11"), {"cabin": "invalid"}),
    (("PTY", "CDG", "2027-01-10", "2027-01-11"), {"passengers": swoop.Passengers(adults=0)}),
])
def test_invalid_picker_inputs_do_not_send_requests(monkeypatch, args, kwargs):
    calls = _serve(monkeypatch, "")
    with pytest.raises(ValueError):
        swoop.calendar(*args, **kwargs)
    assert calls == []
