"""Bounded, short-lived exact selections for direct booking-price retrieval.

Hints are an optimization, never authentication or a cached price. Current
booking responses must prove the entire ordered flight/local-clock manifest.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, replace
from datetime import datetime
from typing import Any, Callable, Optional

from .builders import ItinerarySummary
from ._validate import parse_flight_number, validate_cabin, validate_date, validate_iata_code, validate_time_range
from .decoder import BookingOption, Codeshare, Itinerary, Segment
from .exceptions import SwoopHTTPError, SwoopTransportError, SwoopUpstreamError
from .models import Passengers, PriceResult, ResolvedLeg, TransportConfig

MAX_HINT_AGE_SECONDS = 120
MAX_HINT_BYTES = 16_384
MAX_HINT_SEGMENTS = 24
_SEGMENT_FIELDS = (
    "airline", "airline_name", "flight_number", "aircraft",
    "departure_airport_code", "arrival_airport_code", "departure_date",
    "arrival_date", "departure_time", "arrival_time", "travel_time",
)
_CONTEXT_FIELDS = (
    "query_legs", "selected_legs", "cabin", "passengers", "include_basic_economy",
    "sort", "exclude_separate_tickets", "show_all_results", "booking_token_hint",
)


def _context_digest(payload: dict[str, Any]) -> str:
    context = {key: payload.get(key) for key in _CONTEXT_FIELDS}
    if isinstance(context["passengers"], Passengers):
        context["passengers"] = asdict(context["passengers"])
    return hashlib.sha256(json.dumps(context, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _clock(segment: Segment, departure: bool) -> str:
    date = segment.departure_date if departure else segment.arrival_date
    clock = segment.departure_time if departure else segment.arrival_time
    if len(date) != 3 or len(clock) != 2 or not all(type(value) is int for value in (*date, *clock)):
        raise ValueError("unknown selected segment clock")
    return datetime(*date, *clock).isoformat()


def _identity(carrier: str, number: str) -> tuple[str, str]:
    if not isinstance(carrier, str) or not re.fullmatch(r"[A-Z0-9]{2,3}", carrier):
        raise ValueError("invalid selected carrier")
    if not isinstance(number, str) or not re.fullmatch(r"[0-9]{1,4}[A-Z]?", number):
        raise ValueError("invalid selected flight number")
    return carrier, number.lstrip("0") or "0"


def _selection(segment: Segment) -> list[Any]:
    departure = _clock(segment, True)
    _clock(segment, False)
    _identity(segment.airline, segment.flight_number)
    parse_flight_number(f"{segment.airline}{segment.flight_number}")
    for airport in (segment.departure_airport_code, segment.arrival_airport_code):
        validate_iata_code(airport, "selected airport")
    return [segment.departure_airport_code, departure[:10], segment.arrival_airport_code,
            None, segment.airline, segment.flight_number]


def _validate_party(passengers: Passengers) -> None:
    values = asdict(passengers)
    if (not all(type(value) is int and value >= 0 for value in values.values())
            or passengers.adults < 1 or sum(values.values()) > 9
            or passengers.infants_on_lap > passengers.adults):
        raise ValueError("invalid selected party")


def _validate_context(payload: dict[str, Any]) -> None:
    """Do not bypass the selector's original validation on the short path."""
    validate_cabin(payload["cabin"])
    if not isinstance(payload["include_basic_economy"], bool):
        raise ValueError("invalid selector fare filter")
    for flag in ("exclude_separate_tickets", "show_all_results"):
        if not isinstance(payload[flag], bool):
            raise ValueError("invalid selector filter")
    if type(payload["sort"]) is not int:
        raise ValueError("invalid selector sort")
    for leg in payload["query_legs"]:
        validate_iata_code(leg["origin"], "origin")
        validate_iata_code(leg["destination"], "destination")
        validate_date(leg["date"], "date")
        stops = leg.get("max_stops")
        if stops is not None and (type(stops) is not int or stops < 0):
            raise ValueError("invalid selected stop filter")
        airlines = leg.get("airlines")
        if airlines is not None and (not isinstance(airlines, list) or not airlines
                or not all(isinstance(code, str) and re.fullmatch(r"[A-Z0-9]{2,3}", code) for code in airlines)):
            raise ValueError("invalid selected airline filter")
        for name, minimum, maximum in (("earliest_departure", 0, 23), ("latest_departure", 1, 24),
                                      ("earliest_arrival", 0, 23), ("latest_arrival", 1, 24)):
            hour = leg.get(name)
            if hour is not None and type(hour) is not int:
                raise ValueError("invalid selected time filter")
            validate_time_range(hour, name, minimum, maximum)


def _snapshot_itineraries(payload: dict[str, Any], hint: dict[str, Any]) -> list[Itinerary]:
    legs, selected = payload.get("query_legs"), payload.get("selected_legs")
    snapshots = hint.get("itineraries")
    if (not isinstance(legs, list) or not 1 <= len(legs) <= 6
            or not isinstance(selected, list) or len(selected) != len(legs)
            or not isinstance(snapshots, list) or len(snapshots) != len(legs)):
        raise ValueError("incomplete selected itinerary")
    itineraries: list[Itinerary] = []
    count = 0
    for leg, flights, snapshot in zip(legs, selected, snapshots):
        if not isinstance(leg, dict) or not isinstance(snapshot, dict):
            raise ValueError("invalid selected itinerary")
        rows, currency, duration = snapshot.get("segments"), snapshot.get("currency"), snapshot.get("travel_time")
        if (not isinstance(rows, list) or not rows or not isinstance(flights, list)
                or len(rows) != len(flights) or not isinstance(currency, str)
                or not re.fullmatch(r"[A-Z]{3}", currency)
                or type(duration) is not int or not 0 <= duration <= 100_000):
            raise ValueError("invalid selected snapshot")
        count += len(rows)
        if count > MAX_HINT_SEGMENTS:
            raise ValueError("selected snapshot exceeds segment budget")
        segments: list[Segment] = []
        for row, flight in zip(rows, flights):
            if not isinstance(row, dict) or set(row) != {*_SEGMENT_FIELDS, "codeshares"}:
                raise ValueError("invalid selected segment snapshot")
            values = dict(row)
            aliases = values.pop("codeshares")
            if not isinstance(aliases, list) or len(aliases) > 8:
                raise ValueError("invalid selected aliases")
            codeshares = []
            for alias in aliases:
                if not isinstance(alias, list) or len(alias) != 2:
                    raise ValueError("invalid selected alias")
                _identity(*alias)
                codeshares.append(Codeshare(airline_code=alias[0], flight_number=alias[1]))
            if (not isinstance(values["airline_name"], str) or len(values["airline_name"]) > 64
                    or not isinstance(values["aircraft"], str) or len(values["aircraft"]) > 64
                    or type(values["travel_time"]) is not int or not 0 <= values["travel_time"] <= 10_000):
                raise ValueError("invalid selected display facts")
            for key in ("departure_date", "arrival_date", "departure_time", "arrival_time"):
                if not isinstance(values[key], (list, tuple)):
                    raise ValueError("invalid selected clock")
                values[key] = tuple(values[key])
            segment = Segment(**values, codeshares=codeshares)
            if flight != _selection(segment):
                raise ValueError("selected snapshot changed flight lineage")
            if segments and segments[-1].arrival_airport_code != segment.departure_airport_code:
                raise ValueError("disconnected selected itinerary")
            segments.append(segment)
        first, last = segments[0], segments[-1]
        if (first.departure_airport_code != leg.get("origin")
                or last.arrival_airport_code != leg.get("destination")
                or _clock(first, True)[:10] != leg.get("date")):
            raise ValueError("selected snapshot changed requested bounds")
        itineraries.append(Itinerary(
            airline_code=first.airline, airline_names=list(dict.fromkeys(segment.airline_name for segment in segments)),
            segments=segments, departure_airport_code=first.departure_airport_code,
            arrival_airport_code=last.arrival_airport_code,
            departure_date=first.departure_date, arrival_date=last.arrival_date,
            departure_time=first.departure_time, arrival_time=last.arrival_time,
            travel_time=duration, stop_count=len(segments) - 1,
            price_info=ItinerarySummary(flights="", price=0, currency=currency),
        ))
    return itineraries


def make_pricing_hint(payload: dict[str, Any], itineraries: list[Itinerary]) -> Optional[dict[str, Any]]:
    """Keep only the selected flights, never raw responses, fares or candidates."""
    if payload.get("exclude_separate_tickets") is not True or not payload.get("booking_token_hint"):
        return None
    try:
        _validate_context(payload)
        party = payload["passengers"]
        _validate_party(party if isinstance(party, Passengers) else Passengers(**party))
        hint = {"created_at": int(time.time()), "context": _context_digest(payload),
                "itineraries": [{"currency": itinerary.currency, "travel_time": itinerary.travel_time,
                    "segments": [{**{name: getattr(segment, name) for name in _SEGMENT_FIELDS},
                        "codeshares": [[alias.airline_code, alias.flight_number] for alias in segment.codeshares]}
                        for segment in itinerary.segments]} for itinerary in itineraries]}
        if len(json.dumps(hint, separators=(",", ":")).encode()) > MAX_HINT_BYTES:
            return None
        _snapshot_itineraries(payload, hint)
        return hint
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def matching_booking_options(options: list[BookingOption], itineraries: list[Itinerary]) -> list[BookingOption]:
    """Require every returned segment to match both exact local clocks in order."""
    manifest = [segment for itinerary in itineraries for segment in itinerary.segments]
    matched = []
    for option in options:
        details = option.segment_details
        if len(details) != len(manifest) or not details:
            continue
        try:
            for detail, segment in zip(details, manifest):
                aliases = {_identity(segment.airline, segment.flight_number),
                           *(_identity(alias.airline_code, alias.flight_number) for alias in segment.codeshares)}
                departure = datetime.fromisoformat(detail.departure_local_iso)
                arrival = datetime.fromisoformat(detail.arrival_local_iso)
                if (departure.utcoffset() is None or arrival.utcoffset() is None
                        or departure.replace(tzinfo=None).isoformat() != _clock(segment, True)
                        or arrival.replace(tzinfo=None).isoformat() != _clock(segment, False)
                        or detail.origin_iata != segment.departure_airport_code
                        or detail.destination_iata != segment.arrival_airport_code
                        or _identity(detail.carrier_code, detail.flight_number) not in aliases
                        or type(detail.cabin_class) is not int or detail.cabin_class not in (1, 2, 3, 4)):
                    raise ValueError("booking response changed selected identity")
        except (ValueError, TypeError, AttributeError):
            continue
        matched.append(option)
    return matched


def try_hint_price(payload: dict[str, Any], *, transport: TransportConfig,
                   deadline: Optional[float], load: Callable[..., list[BookingOption]],
                   eligible: Callable[..., list[BookingOption]]) -> tuple[Optional[PriceResult], int]:
    """Try one authoritative booking RPC; zero attempts for ineligible hints."""
    hint = payload.get("pricing_hint")
    try:
        if (payload.get("exclude_separate_tickets") is not True or not isinstance(hint, dict)
                or type(hint.get("created_at")) is not int
                or not 0 <= time.time() - hint["created_at"] <= MAX_HINT_AGE_SECONDS
                or len(json.dumps(hint, separators=(",", ":")).encode()) > MAX_HINT_BYTES
                or hint.get("context") != _context_digest(payload)):
            return None, 0
        _validate_party(payload["passengers"])
        token = payload.get("booking_token_hint")
        if not isinstance(token, str) or not token.strip():
            return None, 0
        itineraries = _snapshot_itineraries(payload, hint)
        _validate_context(payload)
    except (ValueError, TypeError, KeyError, AttributeError):
        return None, 0
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SwoopTransportError("Google Flights pricing budget exhausted")
        transport = replace(transport, timeout=min(transport.timeout, remaining))
    legs = [{**leg, "selected_legs": flights}
            for leg, flights in zip(payload["query_legs"], payload["selected_legs"])]
    try:
        options = load(token, legs, cabin=payload["cabin"], passengers=payload["passengers"],
                       exclude_separate_tickets=True, transport=transport)
    except SwoopUpstreamError as exc:
        if exc.grpc_code != 5:  # NOT_FOUND: the retained selection no longer exists
            raise
        return None, 1
    except SwoopHTTPError as exc:
        if exc.status_code != 410:  # GONE, rather than an outage or rate limit
            raise
        return None, 1
    options = matching_booking_options(options, itineraries)
    choices = eligible(options, payload["include_basic_economy"], cabin=payload["cabin"])
    if not choices:
        return None, 1
    best = min(choices, key=lambda option: option.price)
    final = itineraries[-1]
    final.direct_price, final.booking_token = best.price, token
    return PriceResult(
        price=best.price, currency=final.currency,
        fare_brand=best.brand_label or best.brand_code or None, is_basic_economy=best.is_basic,
        is_estimate=False, booking_options=options, itinerary=final, rpc_calls=1,
        resolved_legs=[ResolvedLeg(origin=leg["origin"], destination=leg["destination"], date=leg["date"],
            flight_summary=f"{itinerary.segments[0].airline} {itinerary.segments[0].flight_number}",
            itinerary=itinerary, selection="explicit")
            for leg, itinerary in zip(payload["query_legs"], itineraries)],
    ), 1
