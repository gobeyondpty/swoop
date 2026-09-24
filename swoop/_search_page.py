"""HTTP-only fallback for exits rejecting unsigned shopping RPCs.

The public page embeds the shopping payload in ds:1. Reuse the existing TFS
builder and decoder; do not launch a browser or borrow another exit's session.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlencode

from .builders import CABIN_CLASS_MAP, CabinClass, SearchLeg, TFSData, _PBPassengers
from .decoder import RawSearchResult, decode_result
from .exceptions import SwoopHTTPError, SwoopParseError, SwoopRateLimitError, SwoopUpstreamError
from .models import Passengers, TransportConfig

_DATA_START = re.compile(
    r"AF_initDataCallback\(\{key:\s*['\"]ds:1['\"].*?\bdata\s*:", re.S,
)
_WINDOWS = ("earliest_departure", "latest_departure", "earliest_arrival", "latest_arrival")


def _page_payload(html: str) -> list[Any]:
    match = _DATA_START.search(html)
    if match is None:
        raise SwoopParseError("Search page has no flight payload (ds:1)")
    try:
        payload, _ = json.JSONDecoder().raw_decode(html[match.end():].lstrip())
    except ValueError as exc:
        raise SwoopParseError("Search page flight payload is not valid JSON") from exc
    # A page can itself contain an upstream rejection instead of inventory.
    if isinstance(payload, list) and payload and type(payload[0]) is int and payload[0] != 0:
        raise SwoopUpstreamError(payload[0])
    if not isinstance(payload, list) or len(payload) < 4:
        raise SwoopParseError("Search page flight payload has an unexpected shape")
    return payload


def fetch_search_page(
    client: Any, legs: list[dict[str, Any]], *, cabin: CabinClass,
    passengers: Passengers, sort: int, exclude_basic_economy: bool,
    transport: TransportConfig,
) -> RawSearchResult:
    """Make one page request, preserving supported search constraints.

Selected itineraries and multi-city need the RPC. Time windows have no known
TFS encoding here; reject them rather than return flights ignoring the intent.
"""
    if (not 1 <= len(legs) <= 2
            or any(leg.get("selected_legs") for leg in legs)
            or any(leg.get(key) is not None for leg in legs for key in _WINDOWS)
            or (len(legs) == 2 and (
                legs[0]["origin"] != legs[1]["destination"]
                or legs[0]["destination"] != legs[1]["origin"]))):
        raise SwoopUpstreamError(13)
    tfs = TFSData(
        flight_data=[SearchLeg(
            date=leg["date"], from_airport=leg["origin"], to_airport=leg["destination"],
            max_stops=leg.get("max_stops"), airlines=leg.get("airlines"),
        ) for leg in legs],
        seat=CABIN_CLASS_MAP[cabin], trip=2 if len(legs) == 1 else 1,
        passengers=_PBPassengers(
            adults=passengers.adults, children=passengers.children,
            infants_in_seat=passengers.infants_in_seat, infants_on_lap=passengers.infants_on_lap,
        ),
        exclude_basic_economy=exclude_basic_economy,
    )
    params = {"tfs": tfs.as_b64().decode(), "hl": "en"}
    if transport.country:
        params["gl"] = transport.country.upper()
    response = client.get(
        "https://www.google.com/travel/flights?" + urlencode(params),
        headers={"accept": "text/html", "accept-language": "en-US,en;q=0.9"},
        timeout=transport.timeout,
    )
    if response.status_code == 429:
        raise SwoopRateLimitError()
    if response.status_code != 200:
        raise SwoopHTTPError(response.status_code)
    result = decode_result(_page_payload(response.text))
    # The page ranks best flights by default. Honor the requested ordering over
    # the returned inventory without claiming the page provides every RPC row.
    if sort != 1:
        rows = [*result.best, *result.other]
        if sort == 2:
            rows.sort(key=lambda row: row.price if row.price is not None else float("inf"))
        elif sort == 3:
            rows.sort(key=lambda row: (row.departure_date, row.departure_time))
        elif sort == 4:
            rows.sort(key=lambda row: (row.arrival_date, row.arrival_time))
        elif sort == 5:
            rows.sort(key=lambda row: row.travel_time)
        result.best, result.other = [], rows
    return result
