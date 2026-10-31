"""Regression #2149: next-bound constraints belong in shopping and retained selectors."""
from copy import deepcopy

import pytest

import swoop
import swoop._selection as selection
from swoop._continuation import narrow_next_leg
from swoop.decoder import RawSearchResult, Layover
from swoop.models import Passengers
from swoop.rpc import _build_segment_from_leg, _normalize_rpc_leg
from tests.factories import make_simple_itinerary


LEGS = [_normalize_rpc_leg("JFK", "PTY", "2026-11-16"),
        _normalize_rpc_leg("PTY", "JFK", "2026-11-23")]


def flight(origin="JFK", destination="PTY", number="117", date="2026-11-16"):
    return make_simple_itinerary(origin=origin, destination=destination, date=date,
                                 airline="AA", flight_number=number, booking_token="")


def connecting(origin, arrival, departure, destination, date="2026-11-16"):
    first = flight(origin, arrival, date=date)
    first.segments += flight(departure, destination, "118", date).segments
    first.arrival_airport_code = destination
    # Reproduce the observed NRT/HND shape: decoded layover names only the latter.
    first.layovers = [Layover(minutes=120, arrival_airport_code=departure)]
    return first


def selector(legs, outbound):
    return selection.encode_trip_selector(request_legs=legs, itineraries=[outbound],
        cabin="premium-economy", passengers=Passengers(adults=2, children=1),
        include_basic_economy=True, exclude_separate_tickets=True,
        carry_on_bags=1, checked_bags=2, max_price=900, country="PA")


def test_regression_2149_filters_reach_next_bound_and_pricing_without_changing_outbound(monkeypatch):
    outbound = connecting("JFK", "MIA", "MIA", "PTY")
    inbound = flight("PTY", "JFK", "119", "2026-11-23")
    legs = deepcopy(LEGS)
    legs[1].update(max_stops=1, max_duration=900, min_layover_duration=60,
                   max_layover_duration=600, earliest_departure=6, latest_departure=22,
                   airlines=["AA", "UA"], exclude_connecting_airports=["ATL"])
    original = selector(legs, outbound)
    original_legs = selection.decode_trip_selector(original)["query_legs"]
    calls = []

    def provider(query, **kwargs):
        calls.append((deepcopy(query), kwargs))
        return RawSearchResult(other=[inbound if query[0].get("selected_legs") else outbound])

    monkeypatch.setattr(selection, "_search_from_legs", provider)
    result = swoop.search_next_leg(original, next_leg_filters={
        "airlines": ["AA"], "max_stops": 2, "max_duration": 700,
        "min_layover_duration": 180, "max_layover_duration": 720,
        "exclude_connecting_airports": ["MIA"], "earliest_departure": 10,
        "latest_departure": 24, "earliest_arrival": 1, "latest_arrival": 23})
    assert len(result.results) == 1  # MIA on the already chosen outbound is allowed.
    payload = selection.decode_trip_selector(result.results[0].selector)
    assert selection.decode_trip_selector(original)["query_legs"] == original_legs
    assert payload["query_legs"][0] == original_legs[0]
    expected = dict(original_legs[1], airlines=["AA"], max_stops=1, max_duration=700,
                    min_layover_duration=180, max_layover_duration=600,
                    exclude_connecting_airports=["ATL", "MIA"], earliest_departure=10,
                    latest_departure=22, earliest_arrival=1, latest_arrival=23)
    assert payload["query_legs"][1] == expected
    assert calls[1][0][1] == expected
    wire = _build_segment_from_leg(calls[1][0][1])
    assert wire[3:5] == [2, ["AA"]]
    assert wire[7] == [700]
    assert wire[10:13] == [["ATL", "MIA"], 180, 600]
    assert swoop.price_selector(result.results[0].selector) is not None
    for query, kwargs in calls:
        assert query[1] == expected
        assert kwargs["passengers"] == Passengers(adults=2, children=1)
        assert kwargs["cabin"] == "premium-economy"
        assert kwargs["max_price"] == 900 and kwargs["checked_bags"] == 2
        assert kwargs["exclude_separate_tickets"] is True
        assert kwargs["transport"].country == "PA"


@pytest.mark.parametrize("filters", [
    {"country": "US"}, {"passengers": {"adults": 1}}, {"cabin": "first"},
    {"max_stops": True}, {"max_duration": 0}, {"earliest_departure": 24},
    {"latest_arrival": 25}, {"airlines": []}, {"connecting_airports": ["bad!"]},
    {"airlines": ["UA"]}, {"earliest_departure": 23},
    {"min_layover_duration": 901}, {"connecting_airports": ["MIA"]},
    {"connecting_airports": ["ORD"], "exclude_connecting_airports": ["ORD"]},
])
def test_regression_2149_impossible_or_invalid_filters_fail_before_io(filters, monkeypatch):
    legs = deepcopy(LEGS)
    legs[1].update(airlines=["AA"], latest_departure=22,
                   max_layover_duration=900, exclude_connecting_airports=["MIA"])
    monkeypatch.setattr(selection, "_search_from_legs", lambda *a, **k: pytest.fail("provider called"))
    with pytest.raises(ValueError):
        swoop.search_next_leg(selector(legs, flight()), next_leg_filters=filters)


def test_regression_2149_routing_lists_intersect_and_cannot_clear_constraints():
    old = {"connecting_airports": ["MIA", "ORD"], "max_stops": 1}
    assert narrow_next_leg(old, {"connecting_airports": ["ORD", "ATL"]})["connecting_airports"] == ["ORD"]
    assert narrow_next_leg(old, {"exclude_connecting_airports": ["MIA"]})["connecting_airports"] == ["ORD"]
    assert narrow_next_leg(old, {"max_stops": None}) == old
    assert old == {"connecting_airports": ["MIA", "ORD"], "max_stops": 1}


@pytest.mark.parametrize("filter", [{"exclude_connecting_airports": ["NRT"]},
                                   {"connecting_airports": ["HND"]}])
def test_regression_2149_transfer_checks_both_airports_and_marks_coverage(monkeypatch, filter):
    legs = [_normalize_rpc_leg("ORD", "PVG", "2026-11-18", **filter)]
    transfer = connecting("ORD", "NRT", "HND", "PVG", "2026-11-18")
    nonstop = flight("ORD", "PVG", "120", "2026-11-18")
    monkeypatch.setattr(selection, "_search_from_legs", lambda *a, **k: RawSearchResult(other=[transfer, nonstop]))
    result = selection.search_trip_options(legs)
    assert [trip.legs[0].itinerary.segments[0].flight_number for trip in result.results] == ["120"]
    assert not result.is_complete and "routing_mismatch" in result.truncation_reasons


def test_regression_2149_continuation_rejects_upstream_routing_mismatch(monkeypatch):
    outbound = flight()
    inbound = connecting("PTY", "MIA", "MIA", "JFK", "2026-11-23")
    monkeypatch.setattr(selection, "_search_from_legs", lambda query, **kwargs:
        RawSearchResult(other=[inbound if query[0].get("selected_legs") else outbound]))
    result = swoop.search_next_leg(selector(LEGS, outbound), next_leg_filters={"exclude_connecting_airports": ["MIA"]})
    assert result.results == [] and not result.is_complete
    assert "routing_mismatch" in result.truncation_reasons


def test_regression_2149_narrowed_bound_survives_later_continuation(monkeypatch):
    legs = [*deepcopy(LEGS), _normalize_rpc_leg("JFK", "BOS", "2026-11-25")]
    outbound = flight()
    inbound = flight("PTY", "JFK", "119", "2026-11-23")
    final = flight("JFK", "BOS", "120", "2026-11-25")
    seen = []
    def provider(query, **kwargs):
        seen.append(deepcopy(query))
        return RawSearchResult(other=[final if query[1].get("selected_legs") else
                                      inbound if query[0].get("selected_legs") else outbound])
    monkeypatch.setattr(selection, "_search_from_legs", provider)
    second = swoop.search_next_leg(selector(legs, outbound), next_leg_filters={"exclude_connecting_airports": ["MIA"]})
    third = swoop.search_next_leg(second.results[0].selector, next_leg_filters={"max_stops": 0})
    payload = selection.decode_trip_selector(third.results[0].selector)
    assert third.results[0].is_resolved
    assert payload["query_legs"][1]["exclude_connecting_airports"] == ["MIA"]
    assert payload["query_legs"][2]["max_stops"] == 0
    assert "max_stops" not in payload["query_legs"][0]
    assert all(query[1]["exclude_connecting_airports"] == ["MIA"] for query in seen)
