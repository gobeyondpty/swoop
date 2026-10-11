"""HTTP-only fallback for exits rejecting unsigned shopping RPCs.

The public page embeds the shopping payload in ds:1. Reuse the existing TFS
builder and decoder; do not launch a browser or borrow another exit's session.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any, Optional
from urllib.parse import urlencode

from .builders import CABIN_CLASS_MAP, CabinClass, TFSData, _PBPassengers, _apply_tfs_leg
from .decoder import RawSearchResult, decode_result
from .exceptions import SwoopError, SwoopHTTPError, SwoopParseError, SwoopRateLimitError, SwoopTransportError, SwoopUpstreamError
from ._transport_budget import remaining_timeout, transport_deadline
from .models import Passengers, TransportConfig
from .response_observer import read_response_body
from .diagnostics import measure_decode, measure_http

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
    exclude_separate_tickets: bool = False,
    _deadline: Optional[float] = None,
) -> RawSearchResult:
    """Make one page request, preserving supported search constraints.

Selected itineraries and multi-city need the RPC. Time windows have no known
TFS encoding here; reject them rather than return flights ignoring the intent.
"""
    deadline = transport_deadline(transport.timeout, _deadline)
    if (not 1 <= len(legs) <= 2
            or any(leg.get("selected_legs") for leg in legs)
            or any(leg.get(key) is not None for leg in legs for key in _WINDOWS)
            or (len(legs) == 2 and (
                legs[0]["origin"] != legs[1]["destination"]
                or legs[0]["destination"] != legs[1]["origin"]))):
        raise SwoopUpstreamError(13)
    tfs = TFSData(
        flight_data=[],
        seat=CABIN_CLASS_MAP[cabin], trip=2 if len(legs) == 1 else 1,
        passengers=_PBPassengers(
            adults=passengers.adults, children=passengers.children,
            infants_in_seat=passengers.infants_in_seat, infants_on_lap=passengers.infants_on_lap,
        ),
        exclude_basic_economy=exclude_basic_economy,
        exclude_separate_tickets=exclude_separate_tickets,
    )
    info = tfs.pb()
    for leg in legs:
        _apply_tfs_leg(
            info, date=leg["date"], origin=leg["origin"], destination=leg["destination"],
            max_stops=leg.get("max_stops"), airlines=leg.get("airlines"),
        )
    params = {"tfs": base64.b64encode(info.SerializeToString()).decode(), "hl": "en"}
    if transport.country:
        params["gl"] = transport.country.upper()
    timeout = remaining_timeout(deadline)
    try:
        with measure_http(transport, "shopping", "page", "GET") as measurement:
            response = client.get(
                "https://www.google.com/travel/flights?" + urlencode(params),
                headers={"accept": "text/html", "accept-language": "en-US,en;q=0.9"},
                timeout=timeout,
            )
            measurement.status_code = response.status_code
            text = read_response_body(response, transport, operation="shopping", phase="page",
                method="GET") if response.status_code == 200 or transport.response_observer else ""
            if response.status_code == 200:
                measurement.body(text)
                remaining_timeout(deadline)
    except SwoopError:
        raise
    except Exception as exc:
        raise SwoopTransportError("Google Flights page transport failed while sending or reading a response") from exc
    if response.status_code == 429:
        raise SwoopRateLimitError()
    if response.status_code != 200:
        raise SwoopHTTPError(response.status_code)
    with measure_decode(transport):
        result = decode_result(_page_payload(text))
    remaining_timeout(deadline)
    result._result_scope = "limited"
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
