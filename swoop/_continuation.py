"""Validated narrowing of the next unselected journey, without changing its prefix."""
from typing import Any

from .builders import AIRLINE_ALLIANCES
from .decoder import Itinerary
from ._pricing import _validate_routing

_MINIMUMS = {"min_layover_duration", "earliest_departure", "earliest_arrival"}
_MAXIMUMS = {"max_stops", "max_duration", "max_layover_duration", "latest_departure", "latest_arrival"}
_LISTS = {"airlines", "connecting_airports", "exclude_connecting_airports"}


def narrow_next_leg(original: dict[str, Any], filters: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(filters, dict) or set(filters) - (_MINIMUMS | _MAXIMUMS | _LISTS):
        raise ValueError("unsupported next-leg filters")
    supplied = {key: value for key, value in filters.items() if value is not None}
    for key, value in supplied.items():
        if key in _LISTS:
            if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
                raise ValueError("next-leg lists must be nonempty code lists")
            if key == "airlines":
                if any(not (len(v) == 2 and v.isascii() and v.isalnum() or v in AIRLINE_ALLIANCES) for v in value):
                    raise ValueError("invalid next-leg airline")
            elif any(len(v) != 3 or not v.isascii() or not v.isalpha() for v in value):
                raise ValueError("invalid next-leg airport")
            supplied[key] = list(dict.fromkeys(v.upper() for v in value))
        elif type(value) is not int or value < (1 if key in {"max_duration", "latest_departure", "latest_arrival"} else 0):
            raise ValueError("next-leg limits must be valid integers")
        elif ((key in {"earliest_departure", "earliest_arrival"} and value > 23)
              or (key in {"latest_departure", "latest_arrival"} and value > 24)):
            raise ValueError("invalid next-leg hour")
    result = dict(original)
    for key in _MINIMUMS | _MAXIMUMS:
        if key not in supplied:
            continue
        previous = original.get(key)
        result[key] = supplied[key] if previous is None else (
            max(previous, supplied[key]) if key in _MINIMUMS else min(previous, supplied[key]))
    for key in ("airlines", "connecting_airports"):
        if key not in supplied:
            continue
        previous = original.get(key)
        narrowed = [v for v in previous if v in supplied[key]] if previous else supplied[key]
        if not narrowed:
            raise ValueError("next-leg filter has no allowed intersection")
        result[key] = narrowed
    excluded = list(dict.fromkeys([*(original.get("exclude_connecting_airports") or []),
                                  *(supplied.get("exclude_connecting_airports") or [])]))
    if result.get("connecting_airports"):
        result["connecting_airports"] = [v for v in result["connecting_airports"] if v not in excluded]
        if not result["connecting_airports"]:
            raise ValueError("next-leg routing excludes every allowed connection")
        result.pop("exclude_connecting_airports", None)
    elif excluded:
        result["exclude_connecting_airports"] = excluded
    for start, end in (("earliest_departure", "latest_departure"), ("earliest_arrival", "latest_arrival")):
        earliest, latest = result.get(start), result.get(end)
        if (earliest if earliest is not None else 0) >= (latest if latest is not None else 24):
            raise ValueError("next-leg time window has no allowed intersection")
    _validate_routing(result.get("max_duration"), result.get("connecting_airports"),
                      result.get("exclude_connecting_airports"), result.get("min_layover_duration"),
                      result.get("max_layover_duration"))
    return result


def routing_matches(itinerary: Itinerary, leg: dict[str, Any]) -> bool:
    """Check both airports of a transfer; a single decoded layover name is insufficient."""
    connections = {airport for left, right in zip(itinerary.segments, itinerary.segments[1:])
                   for airport in (left.arrival_airport_code, right.departure_airport_code)}
    allowed = leg.get("connecting_airports")
    excluded = leg.get("exclude_connecting_airports")
    return (not allowed or connections.issubset(allowed)) and not connections.intersection(excluded or [])
