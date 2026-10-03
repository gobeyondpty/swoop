"""Google Flights calendar price grid (``GetCalendarGrid``).

One RPC returns the cheapest fare for every departure date in a window, or for
every departure x return combination of two windows — the data behind the
Google Flights "Date grid". A flexible-date question that would otherwise need
one full search per date pair is a single request.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from .builders import CabinClass
from .decoder import _decode_price_info, _safe_get, raise_if_error_envelope
from .exceptions import SwoopParseError
from .models import Passengers, TransportConfig
from .rpc import _build_filters_from_legs, _encode_f_req_payload, _http_post, _normalize_rpc_leg

logger = logging.getLogger(__name__)

CALENDAR_GRID_RPC_URL = (
    "https://www.google.com/_/FlightsFrontendUi/data/"
    "travel.frontend.flights.FlightsFrontendService/GetCalendarGrid"
)


@dataclass(frozen=True)
class CalendarPrice:
    """Cheapest fare Google lists for one departure (and return) date."""

    departure_date: str
    return_date: Optional[str]
    price: int
    currency: Optional[str]


def _parse_calendar_prices(text: str) -> list[CalendarPrice]:
    """Collect the price cells from every ``wrb.fr`` entry.

    Google streams a grid as many entries, often on one line, each carrying a
    few cells. Reading only the first entry silently drops most of the grid.
    """
    prices: list[CalendarPrice] = []
    frames: list[Any] = []
    payloads = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("[["):
            continue
        try:
            outer = json.loads(line)
        except ValueError:
            continue
        for entry in outer if isinstance(outer, list) else []:
            if not (isinstance(entry, list) and entry and entry[0] == "wrb.fr"):
                continue
            if not (len(entry) > 2 and isinstance(entry[2], str)):
                frames.append(entry)
                continue
            try:
                inner = json.loads(entry[2])
            except ValueError:
                continue
            payloads += 1
            for cell in _safe_get(inner, [1]) or []:
                departure_date = _safe_get(cell, [0])
                price = _safe_get(cell, [2, 0, 1])
                if not isinstance(departure_date, str) or not isinstance(price, (int, float)):
                    continue  # no fare published for this date
                return_date = _safe_get(cell, [1])
                summary = _decode_price_info(_safe_get(cell, [2]) or [])
                prices.append(CalendarPrice(
                    departure_date=departure_date,
                    return_date=return_date if isinstance(return_date, str) else None,
                    price=int(price),
                    currency=summary.currency if summary and summary.currency else None,
                ))
    if not payloads:
        raise_if_error_envelope(frames, endpoint="GetCalendarGrid")
        raise SwoopParseError("Calendar response missing inner payload")
    return prices


def calendar_prices(
    origin: str,
    destination: str,
    departure_start: str,
    departure_end: str,
    *,
    return_start: Optional[str] = None,
    return_end: Optional[str] = None,
    cabin: CabinClass = "economy",
    passengers: Passengers = Passengers(),
    max_stops: Optional[int] = None,
    airlines: Optional[list[str]] = None,
    exclude_basic_economy: bool = False,
    exclude_separate_tickets: bool = False,
    transport: TransportConfig = TransportConfig(),
) -> list[CalendarPrice]:
    """Cheapest fare for every date in a flexible window, in one request.

    Pass only the departure window for a one-way scan. Pass both return bounds
    for a roundtrip grid: one :class:`CalendarPrice` per departure x return
    combination. Dates are inclusive ``YYYY-MM-DD``; dates with no published
    fare are omitted. The filters are the ones :func:`search` sends, so a cell
    is comparable with the cheapest result of the matching search.

    Raises:
        ValueError: If only one return bound is given.
        SwoopUpstreamError: If Google answers with an ErrorResponse envelope.
        SwoopParseError: If the response carries no calendar payload.
    """
    legs = [_normalize_rpc_leg(origin, destination, departure_start, max_stops=max_stops, airlines=airlines)]
    windows = [[departure_start, departure_end]]
    if return_start is not None and return_end is not None:
        legs.append(_normalize_rpc_leg(destination, origin, return_start, max_stops=max_stops, airlines=airlines))
        windows.append([return_start, return_end])
    elif return_start is not None or return_end is not None:
        raise ValueError("return_start and return_end must be given together")
    filters = _build_filters_from_legs(
        legs, cabin=cabin, passengers=passengers,
        exclude_basic_economy=exclude_basic_economy, exclude_separate_tickets=exclude_separate_tickets,
    )[1]

    res = _http_post(
        CALENDAR_GRID_RPC_URL,
        content=f"f.req={_encode_f_req_payload([None, filters, *windows])}".encode(),
        transport=transport,
    )
    prices = _parse_calendar_prices(res.text)
    logger.info("calendar_prices %s->%s returned %d dates", origin, destination, len(prices))
    return prices
