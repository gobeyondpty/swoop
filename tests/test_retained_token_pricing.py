"""Fresh retained tokens price exact selected flights without shopping replay."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace

import pytest

import swoop._pricing_hint as hints
import swoop._selection as selection
import swoop.rpc as rpc
from swoop.builders import ItinerarySummary
from swoop.decoder import BookingOption, BookingSegmentDetails, Codeshare
from swoop.exceptions import SwoopHTTPError, SwoopRateLimitError, SwoopTransportError, SwoopUpstreamError
from swoop.models import Passengers, TransportConfig
from tests.factories import make_raw_result, make_simple_itinerary


@pytest.fixture(autouse=True)
def known_default_country(monkeypatch):
    monkeypatch.setattr(rpc, "_default_country", "US")


def _source(count=1, *, country="US"):
    routes = [("JFK", "LAX", "2026-11-15"), ("LAX", "JFK", "2026-11-22"),
              ("JFK", "BOS", "2026-11-23")][:count]
    legs = [selection._normalize_rpc_leg(origin, destination, date, airlines=["DL"],
            max_stops=0, earliest_departure=6) for origin, destination, date in routes]
    trips = [make_simple_itinerary(origin=origin, destination=destination, date=date,
             airline="DL", flight_number=str(100 + index), price=415, booking_token="retained-token")
             for index, (origin, destination, date) in enumerate(routes)]
    for trip in trips:
        trip.price_info = ItinerarySummary(flights="", price=41500, currency="USD")
    selector = selection.encode_trip_selector(request_legs=legs, itineraries=trips,
        cabin="economy", passengers=Passengers(adults=2, children=1),
        include_basic_economy=False, exclude_separate_tickets=True, country=country)
    return selector, legs, trips


def _option(trips, price=375, *, basic=False, cabin="economy"):
    return BookingOption(price=price, brand_label="Main Cabin", is_basic=basic, _cabin_bucket=cabin,
        segment_details=[BookingSegmentDetails(
            origin_iata=segment.departure_airport_code, destination_iata=segment.arrival_airport_code,
            departure_local_iso=hints._clock(segment, True) + "-04:00",
            arrival_local_iso=hints._clock(segment, False) + "-07:00",
            carrier_code=segment.airline, flight_number=segment.flight_number, cabin_class=1)
            for trip in trips for segment in trip.segments])


def _encode(payload):
    if isinstance(payload.get("passengers"), Passengers):
        from dataclasses import asdict
        payload = {**payload, "passengers": asdict(payload["passengers"])}
    return selection.SELECTOR_PREFIX + selection._encode_payload(payload)


def _replay(monkeypatch, trips):
    calls = []
    def fetch(legs, **kwargs):
        index = len(calls)
        calls.append((deepcopy(legs), kwargs))
        current = deepcopy(trips[index])
        current.booking_token = "fresh-token"
        return make_raw_result(current)
    monkeypatch.setattr(selection, "_search_from_legs", fetch)
    return calls


@pytest.mark.parametrize("count", [1, 2, 3])
def test_fresh_single_ticket_prices_with_one_rpc_and_no_replay(monkeypatch, count):
    selector, legs, trips = _source(count)
    calls = []
    def book(token, request, **kwargs):
        calls.append((token, request, kwargs))
        return [_option(trips)]
    monkeypatch.setattr(selection, "get_trip_booking_results", book)
    monkeypatch.setattr(selection, "_search_from_legs", lambda *_a, **_k: pytest.fail("fresh selection replayed shopping"))
    result = selection.price_trip_selector(selector, transport=TransportConfig(retries=0), time_budget=8)
    assert result.price == 375 and result.currency == "USD" and result.is_estimate is False
    assert result.rpc_calls == len(calls) == 1
    assert len(result.resolved_legs) == count
    assert all(leg.selection == "explicit" for leg in result.resolved_legs)
    token, request, kwargs = calls[0]
    assert token == "retained-token"
    assert kwargs["exclude_separate_tickets"] is True
    assert kwargs["passengers"] == Passengers(adults=2, children=1)
    assert kwargs["cabin"] == "economy"
    assert [leg["date"] for leg in request] == [leg["date"] for leg in legs]
    assert all(leg["airlines"] == ["DL"] and leg["max_stops"] == 0 and leg["earliest_departure"] == 6 for leg in request)
    assert len(result.booking_options[0].segment_details) == count


def test_minimal_hint_is_bounded_and_contains_no_fare_or_raw_payload():
    selector, _, _ = _source(3)
    hint = selection.decode_trip_selector(selector)["pricing_hint"]
    assert len(json.dumps(hint).encode()) < 3_000
    assert set(hint) == {"created_at", "context", "itineraries"}
    assert "price" not in json.dumps(hint) and "booking_token" not in json.dumps(hint)
    assert all(set(leg) == {"currency", "travel_time", "segments"} for leg in hint["itineraries"])


def test_sanitized_connecting_response_prices_through_real_rpc_decoder(monkeypatch):
    import swoop.rpc as rpc
    from tests.factories import FakeHTTPResponse, encode_rpc_outer
    from tests.test_booking_segment_details import _option as raw_option

    trip = make_simple_itinerary(origin="JFK", destination="PTY", date="2026-10-26",
        airline="AA", flight_number="2161", booking_token="retained-token")
    first = replace(trip.segments[0], arrival_airport_code="MIA", aircraft="738",
        departure_time=(5, 55), arrival_time=(9, 0), travel_time=185)
    second = replace(first, departure_airport_code="MIA", arrival_airport_code="PTY",
        flight_number="959", departure_time=(10, 56), arrival_time=(13, 2), travel_time=186)
    trip.segments = [first, second]
    trip.travel_time = 487  # Includes the connection; do not sum only airborne time.
    trip.price_info = ItinerarySummary(flights="", price=29900, currency="USD")
    selector = selection.encode_trip_selector(
        request_legs=[selection._normalize_rpc_leg("JFK", "PTY", "2026-10-26", airlines=["AA"])],
        itineraries=[trip], cabin="first", include_basic_economy=False, exclude_separate_tickets=True, country="US")
    wire = encode_rpc_outer([None, [[raw_option()]]])
    requests = []
    def post(url, **kwargs):
        requests.append((url, kwargs))
        return FakeHTTPResponse(text=wire)
    monkeypatch.setattr(rpc, "_http_post", post)
    monkeypatch.setattr(selection, "_search_from_legs", lambda *_a, **_k: pytest.fail("connecting selection replayed"))
    result = selection.price_trip_selector(selector)
    assert result.price == 250 and result.rpc_calls == len(requests) == 1
    assert result.itinerary.travel_time == 487
    assert [fact.cabin_class for fact in result.booking_options[0].segment_details] == [4, 3]
    assert [fact.flight_number for fact in result.booking_options[0].segment_details] == ["2161", "959"]


@pytest.mark.parametrize("mutation", ["cabin", "basic", "party", "airport", "filter-time", "filter-stops", "filter-airline"])
def test_invalid_context_cannot_mint_a_direct_pricing_hint(mutation):
    selector, _, trips = _source()
    payload = selection.decode_trip_selector(selector)
    if mutation == "cabin": payload["cabin"] = "supersonic"
    elif mutation == "basic": payload["include_basic_economy"] = "false"
    elif mutation == "party": payload["passengers"] = Passengers(adults=True)
    elif mutation == "airport": payload["query_legs"][0]["origin"] = "ZZZ"
    elif mutation == "filter-time": payload["query_legs"][0]["latest_arrival"] = 25
    elif mutation == "filter-stops": payload["query_legs"][0]["max_stops"] = -1
    elif mutation == "filter-airline": payload["query_legs"][0]["airlines"] = [None]
    assert hints.make_pricing_hint(payload, trips) is None


def test_unknown_currency_or_incomplete_clock_does_not_mint_hint():
    selector, _, trips = _source()
    payload = selection.decode_trip_selector(selector)
    trips[0].price_info = None
    assert hints.make_pricing_hint(payload, trips) is None
    trips[0].price_info = ItinerarySummary(flights="", price=29900, currency="USD")
    trips[0].segments[0].arrival_date = (0, 0, 0)
    assert hints.make_pricing_hint(payload, trips) is None


@pytest.mark.parametrize("mutation", ["intermediate-airport", "primary-carrier"])
def test_direct_snapshot_keeps_existing_selected_segment_validation(mutation):
    selector, _, trips = _source()
    payload = selection.decode_trip_selector(selector)
    if mutation == "intermediate-airport":
        first = replace(trips[0].segments[0], arrival_airport_code="ZZZ")
        second = replace(first, departure_airport_code="ZZZ", arrival_airport_code="LAX", flight_number="101")
        trips[0].segments = [first, second]
    else:
        trips[0].segments[0].airline = "DAL"
    payload["selected_legs"] = [selection._build_selected_legs(trips[0])]
    assert hints.make_pricing_hint(payload, trips) is None


@pytest.mark.parametrize("change", ["no-hint", "expired", "future", "missing-token", "different-party",
                                  "different-brand", "different-single-ticket", "different-filter", "malformed-clock", "oversized"])
def test_ineligible_hints_use_the_existing_replay_path(monkeypatch, change):
    selector, _, trips = _source()
    payload = selection.decode_trip_selector(selector)
    if change == "no-hint": del payload["pricing_hint"]
    elif change == "expired": payload["pricing_hint"]["created_at"] -= hints.MAX_HINT_AGE_SECONDS + 1
    elif change == "future": payload["pricing_hint"]["created_at"] += 10
    elif change == "missing-token": payload["booking_token_hint"] = None
    elif change == "different-party": payload["passengers"] = Passengers(adults=1)
    elif change == "different-brand": payload["include_basic_economy"] = True
    elif change == "different-single-ticket": payload["exclude_separate_tickets"] = False
    elif change == "different-filter": payload["query_legs"][0]["earliest_departure"] = 7
    elif change == "malformed-clock": payload["pricing_hint"]["itineraries"][0]["segments"][0]["arrival_time"] = [None, None]
    elif change == "oversized": payload["pricing_hint"]["padding"] = "x" * hints.MAX_HINT_BYTES
    replay = _replay(monkeypatch, trips)
    tokens = []
    monkeypatch.setattr(selection, "get_trip_booking_results", lambda token, *_a, **_k: tokens.append(token) or [_option(trips)])
    result = selection.price_trip_selector(_encode(payload))
    assert result.price == 375 and result.rpc_calls == 2
    assert len(replay) == 1 and tokens == ["fresh-token"]


def test_unresolved_selector_never_directly_prices_its_partial_token(monkeypatch):
    selector, _, _ = _source(2)
    payload = selection.decode_trip_selector(selector)
    payload["selected_legs"].pop()
    monkeypatch.setattr(selection, "get_trip_booking_results", lambda *_a, **_k: pytest.fail("unresolved token priced"))
    monkeypatch.setattr(selection, "_search_from_legs", lambda *_a, **_k: pytest.fail("unresolved selector should fail closed"))
    assert selection.price_trip_selector(_encode(payload)) is None


@pytest.mark.parametrize("rejection", [None, SwoopUpstreamError(5), SwoopHTTPError(410)])
def test_missing_or_expired_token_falls_back_once(monkeypatch, rejection):
    selector, _, trips = _source(2)
    replay = _replay(monkeypatch, trips)
    tokens = []
    def book(token, *_args, **_kwargs):
        tokens.append(token)
        if token == "retained-token":
            if rejection: raise rejection
            return []
        return [_option(trips)]
    monkeypatch.setattr(selection, "get_trip_booking_results", book)
    result = selection.price_trip_selector(selector)
    assert result.price == 375 and result.is_estimate is False
    assert result.rpc_calls == 4  # one failed hint + two replay legs + one fresh booking
    assert len(replay) == 2 and tokens == ["retained-token", "fresh-token"]


@pytest.mark.parametrize("field,wrong", [
    ("origin_iata", "EWR"), ("destination_iata", "SFO"), ("carrier_code", "AA"),
    ("flight_number", "999"), ("departure_local_iso", "2026-11-15T09:00:00-04:00"),
    ("arrival_local_iso", "2026-11-15T12:15:00-07:00"),
])
def test_response_identity_or_either_clock_mismatch_replays_without_accepting_wrong_price(monkeypatch, field, wrong):
    selector, _, trips = _source()
    wrong_option = _option(trips, price=1)
    setattr(wrong_option.segment_details[0], field, wrong)
    replay = _replay(monkeypatch, trips)
    monkeypatch.setattr(selection, "get_trip_booking_results", lambda token, *_a, **_k:
        [wrong_option] if token == "retained-token" else [_option(trips)])
    result = selection.price_trip_selector(selector)
    assert result.price == 375 and result.rpc_calls == 3 and len(replay) == 1
    assert all(option.price != 1 for option in result.booking_options)


@pytest.mark.parametrize("mismatch", ["count", "order", "no-details", "unknown-cabin"])
def test_full_ordered_manifest_is_required(monkeypatch, mismatch):
    selector, _, trips = _source(2)
    wrong = _option(trips, price=1)
    if mismatch == "count": wrong.segment_details.pop()
    elif mismatch == "order": wrong.segment_details.reverse()
    elif mismatch == "no-details": wrong.segment_details = []
    elif mismatch == "unknown-cabin": wrong.segment_details[0].cabin_class = None
    replay = _replay(monkeypatch, trips)
    monkeypatch.setattr(selection, "get_trip_booking_results", lambda token, *_a, **_k:
        [wrong] if token == "retained-token" else [_option(trips)])
    result = selection.price_trip_selector(selector)
    assert result.price == 375 and len(replay) == 2


def test_second_mismatched_booking_response_never_becomes_a_confirmed_price(monkeypatch):
    selector, _, trips = _source()
    wrong = _option(trips, price=1)
    wrong.segment_details[0].departure_local_iso = "2026-11-15T09:00:00-04:00"
    _replay(monkeypatch, trips)
    monkeypatch.setattr(selection, "get_trip_booking_results", lambda *_a, **_k: [wrong])
    result = selection.price_trip_selector(selector)
    assert result.price == 415 and result.is_estimate is True and result.booking_options == []


@pytest.mark.parametrize("error", [SwoopUpstreamError(13), SwoopUpstreamError(14), SwoopRateLimitError(),
                                  SwoopHTTPError(503), SwoopTransportError("timed out")])
def test_outage_and_rate_limit_stay_typed_without_extra_shopping(monkeypatch, error):
    selector, _, _ = _source()
    def book(*_args, **_kwargs): raise error
    monkeypatch.setattr(selection, "get_trip_booking_results", book)
    monkeypatch.setattr(selection, "_search_from_legs", lambda *_a, **_k: pytest.fail("outage should not replay"))
    with pytest.raises(type(error)) as caught:
        selection.price_trip_selector(selector)
    assert caught.value is error


def test_error_after_expired_token_replay_is_not_hidden_as_estimated_price(monkeypatch):
    selector, _, trips = _source()
    _replay(monkeypatch, trips)
    def book(token, *_args, **_kwargs):
        if token == "retained-token": raise SwoopHTTPError(410)
        raise SwoopHTTPError(503)
    monkeypatch.setattr(selection, "get_trip_booking_results", book)
    with pytest.raises(SwoopHTTPError) as caught:
        selection.price_trip_selector(selector)
    assert caught.value.status_code == 503


def test_held_codeshare_alias_may_match_but_unheld_alias_may_not(monkeypatch):
    _, legs, trips = _source()
    trips[0].segments[0].codeshares = [Codeshare(airline_code="AF", flight_number="0100")]
    selector = selection.encode_trip_selector(request_legs=legs, itineraries=trips, cabin="economy",
        include_basic_economy=False, exclude_separate_tickets=True, country="US")
    option = _option(trips)
    option.segment_details[0].carrier_code = "AF"
    option.segment_details[0].flight_number = "100"
    monkeypatch.setattr(selection, "get_trip_booking_results", lambda *_a, **_k: [option])
    monkeypatch.setattr(selection, "_search_from_legs", lambda *_a, **_k: pytest.fail("held alias unnecessarily replayed"))
    assert selection.price_trip_selector(selector).rpc_calls == 1


def test_fare_filter_keeps_basic_and_other_cabins_out_of_direct_price(monkeypatch):
    selector, _, trips = _source()
    options = [_option(trips, 1, basic=True), _option(trips, 2, cabin="business"), _option(trips, 375)]
    monkeypatch.setattr(selection, "get_trip_booking_results", lambda *_a, **_k: options)
    assert selection.price_trip_selector(selector).price == 375


def test_hint_and_replay_share_one_deadline(monkeypatch):
    selector, _, trips = _source()
    elapsed = [0.0]
    monkeypatch.setattr(selection.time, "monotonic", lambda: elapsed[0])
    budgets = []
    def fetch(_legs, **kwargs):
        assert kwargs["transport"].retries == 0
        budgets.append(kwargs["transport"].timeout)
        elapsed[0] += 1
        trip = deepcopy(trips[0]); trip.booking_token = "fresh-token"
        return make_raw_result(trip)
    def book(token, *_args, **kwargs):
        assert kwargs["transport"].retries == 0
        budgets.append(kwargs["transport"].timeout)
        if token == "retained-token":
            elapsed[0] += 4
            return []
        return [_option(trips)]
    monkeypatch.setattr(selection, "_search_from_legs", fetch)
    monkeypatch.setattr(selection, "get_trip_booking_results", book)
    assert selection.price_trip_selector(selector, time_budget=8).price == 375
    assert budgets == [8, 4, 3]


def test_exhausted_hint_budget_prevents_replay(monkeypatch):
    selector, _, _ = _source()
    elapsed = [0.0]
    monkeypatch.setattr(selection.time, "monotonic", lambda: elapsed[0])
    def book(*_args, **_kwargs): elapsed[0] += 8; return []
    monkeypatch.setattr(selection, "get_trip_booking_results", book)
    monkeypatch.setattr(selection, "_search_from_legs", lambda *_a, **_k: pytest.fail("deadline exhausted"))
    with pytest.raises(SwoopTransportError):
        selection.price_trip_selector(selector, time_budget=8)


@pytest.mark.parametrize("original,current", [("us", "US"), ("US", "us")])
def test_same_country_is_normalized_and_can_use_direct_pricing(monkeypatch, original, current):
    selector, _, trips = _source(country=original)
    assert selection.decode_trip_selector(selector)["country"] == "US"
    monkeypatch.setattr(selection, "get_trip_booking_results", lambda *_a, **_k: [_option(trips)])
    monkeypatch.setattr(selection, "_search_from_legs", lambda *_a, **_k: pytest.fail("normalized country replayed"))
    assert selection.price_trip_selector(selector, transport=TransportConfig(country=current)).rpc_calls == 1


@pytest.mark.parametrize("original,current", [(None, "US"), ("US", "JP"), ("", "US")])
def test_unknown_or_changed_country_replays_and_uses_current_currency(monkeypatch, original, current):
    selector, _, trips = _source(country=original)
    monkeypatch.setattr(rpc, "_default_country", None)
    calls, tokens = [], []
    def fetch(_legs, **kwargs):
        calls.append(kwargs["transport"].country)
        fresh = deepcopy(trips[0])
        fresh.booking_token = "fresh-token"
        fresh.price_info = ItinerarySummary(flights="", price=40000, currency="JPY")
        return make_raw_result(fresh)
    monkeypatch.setattr(selection, "_search_from_legs", fetch)
    monkeypatch.setattr(selection, "get_trip_booking_results", lambda token, *_a, **_k: tokens.append(token) or [_option(trips)])
    result = selection.price_trip_selector(selector, transport=TransportConfig(country=current))
    assert result.currency == "JPY" and result.price == 375 and result.rpc_calls == 2
    assert calls == [current] and tokens == ["fresh-token"]


def test_real_search_mints_country_binding_from_captured_transport(monkeypatch):
    _, legs, trips = _source()
    captured = []
    def fetch(_legs, **kwargs):
        captured.append(kwargs["transport"].country)
        # Global config changes cannot rewrite the country used for this result.
        monkeypatch.setattr(rpc, "_default_country", "JP")
        return make_raw_result(trips[0])
    monkeypatch.setattr(selection, "_search_from_legs", fetch)
    result = selection.search_trip_options(legs, exclude_separate_tickets=True)
    payload = selection.decode_trip_selector(result.results[0].selector)
    assert captured == ["US"] and payload["country"] == "US" and payload["pricing_hint"]


@pytest.mark.parametrize("phase", ["provider", "manifest-validation"])
def test_late_success_is_rejected_after_provider_and_manifest_validation(monkeypatch, phase):
    selector, _, trips = _source()
    elapsed = [0.0]
    monkeypatch.setattr(selection.time, "monotonic", lambda: elapsed[0])
    def book(*_a, **_k):
        if phase == "provider": elapsed[0] += 9
        return [_option(trips)]
    original_match = hints.matching_booking_options
    def match(*args):
        result = original_match(*args)
        if phase == "manifest-validation": elapsed[0] += 9
        return result
    monkeypatch.setattr(selection, "get_trip_booking_results", book)
    monkeypatch.setattr(hints, "matching_booking_options", match)
    monkeypatch.setattr(selection, "_search_from_legs", lambda *_a, **_k: pytest.fail("late success replayed"))
    with pytest.raises(SwoopTransportError):
        selection.price_trip_selector(selector, time_budget=8)


def test_late_success_after_replay_is_also_rejected(monkeypatch):
    selector, _, trips = _source()
    elapsed = [0.0]
    monkeypatch.setattr(selection.time, "monotonic", lambda: elapsed[0])
    def fetch(*_a, **_k):
        elapsed[0] += 1
        trip = deepcopy(trips[0]); trip.booking_token = "fresh-token"
        return make_raw_result(trip)
    def book(token, *_a, **_k):
        elapsed[0] += 1 if token == "retained-token" else 7
        return [] if token == "retained-token" else [_option(trips)]
    monkeypatch.setattr(selection, "get_trip_booking_results", book)
    monkeypatch.setattr(selection, "_search_from_legs", fetch)
    with pytest.raises(SwoopTransportError):
        selection.price_trip_selector(selector, time_budget=8)


def test_default_transport_restores_selector_point_of_sale(monkeypatch):
    selector, _, trips = _source(country="US")
    monkeypatch.setattr(rpc, "_default_country", "JP")
    countries = []
    def book(*_args, **kwargs):
        countries.append(kwargs["transport"].country)
        return [_option(trips)]
    monkeypatch.setattr(selection, "get_trip_booking_results", book)
    monkeypatch.setattr(selection, "_search_from_legs", lambda *_a, **_k: pytest.fail("saved POS replayed"))
    assert selection.price_trip_selector(selector).rpc_calls == 1
    assert countries == ["US"]


def test_retained_booking_preserves_qpx_filters_and_currency(monkeypatch):
    _, legs, trips = _source()
    legs[0].update(max_duration=600, connecting_airports=["ATL"], min_layover_duration=45)
    trips[0].price_info = ItinerarySummary(flights="", price=41500, currency="JPY")
    selector = selection.encode_trip_selector(request_legs=legs, itineraries=trips,
        cabin="economy", include_basic_economy=False, exclude_separate_tickets=True,
        transport=TransportConfig(country="US", currency="JPY"),
        carry_on_bags=1, checked_bags=2, max_price=400)
    requests = []
    def book(_token, query, **kwargs):
        requests.append((query, kwargs))
        return [_option(trips, 375), _option(trips, 420)]
    monkeypatch.setattr(selection, "get_trip_booking_results", book)
    monkeypatch.setattr(selection, "_search_from_legs", lambda *_a, **_k: pytest.fail("fresh QPX selection replayed"))
    result = selection.price_trip_selector(selector)
    assert result.currency == "JPY" and result.price == 375 and result.rpc_calls == 1
    query, kwargs = requests[0]
    assert query[0]["max_duration"] == 600 and query[0]["connecting_airports"] == ["ATL"]
    assert kwargs["carry_on_bags"] == 1 and kwargs["checked_bags"] == 2 and kwargs["max_price"] == 400
    assert (kwargs["transport"].country, kwargs["transport"].currency) == ("US", "JPY")


@pytest.mark.parametrize("field,value", [("carry_on_bags", 1), ("checked_bags", 2), ("max_price", 500), ("currency", "JPY")])
def test_changed_qpx_context_replays_instead_of_reusing_retained_token(monkeypatch, field, value):
    selector, _, trips = _source()
    payload = selection.decode_trip_selector(selector)
    payload[field] = value
    selector = _encode(payload)
    calls = _replay(monkeypatch, trips)
    tokens = []
    monkeypatch.setattr(selection, "get_trip_booking_results", lambda token, *_a, **_k: tokens.append(token) or [_option(trips)])
    assert selection.price_trip_selector(selector) is not None
    assert len(calls) == 1 and tokens == ["fresh-token"]


@pytest.mark.parametrize("field,value", [
    ("carry_on_bags", "one"), ("checked_bags", {}), ("max_price", []),
    ("max_duration", "600"), ("connecting_airports", True),
    ("currency", 123), ("country", {}),
])
def test_invalid_context_cannot_use_a_retained_token_even_with_a_matching_digest(monkeypatch, field, value):
    selector, _, _ = _source()
    payload = selection.decode_trip_selector(selector)
    if field in ("max_duration", "connecting_airports"):
        payload["query_legs"][0][field] = value
    else:
        payload[field] = value
    payload["pricing_hint"]["context"] = hints._context_digest(payload)
    monkeypatch.setattr(selection, "_search_from_legs", lambda *_a, **_k: pytest.fail("invalid context shopped"))
    monkeypatch.setattr(selection, "get_trip_booking_results", lambda *_a, **_k: pytest.fail("invalid context booked"))
    assert selection.price_trip_selector(_encode(payload)) is None
