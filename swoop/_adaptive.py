"""Bounded exact-RPC failover and route health; no query or tenant state is shared."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
import threading
import time
from typing import Any

from ._transport_budget import remaining_timeout
from .exceptions import SwoopHTTPError, SwoopParseError, SwoopTransportError, SwoopUpstreamError
from .models import TransportConfig

_COOLDOWN = 30.0
_MAX_ROUTES = 64
_MAX_ACTIVE = 8
_lock = threading.Lock()


@dataclass
class _Route:
    until: float = 0.0
    generation: int = 0
    active: int = 0
    probing: bool = False
    reason: str | None = None


_routes: OrderedDict[tuple[str, str, str], _Route] = OrderedDict()


def _acquire(key: tuple[str, str, str], *, probe: bool = True) -> tuple[_Route, int, bool] | None:
    with _lock:
        route = _routes.get(key)
        if route is None:
            if len(_routes) >= _MAX_ROUTES:
                victim = next((key for key, item in _routes.items() if item.active == 0 and (item.reason is None or item.until <= time.monotonic())), None)
                if victim is None:
                    return None
                del _routes[victim]
            route = _Route()
            _routes[key] = route
        _routes.move_to_end(key)
        half_open = route.reason is not None
        if (route.active >= _MAX_ACTIVE or route.probing or
                (half_open and (not probe or time.monotonic() < route.until))):
            return None
        route.active += 1
        if half_open:
            route.probing = True
        return route, route.generation, half_open


def _finish(lease: tuple[_Route, int, bool], reason: str | None, delay: float = _COOLDOWN, *, healthy: bool = False) -> None:
    route, generation, half_open = lease
    with _lock:
        route.active -= 1
        if half_open:
            route.probing = False
        if generation != route.generation:
            return  # A late in-flight success must not erase a newer failure.
        if reason:
            route.reason = reason
            route.until = time.monotonic() + max(_COOLDOWN, delay)
            route.generation += 1
        elif half_open and healthy:
            route.reason = None
            route.until = 0.0
            route.generation += 1


def health() -> list[dict[str, Any]]:
    """Bounded, credential-free operational state. No selectors or query data."""
    with _lock:
        return [{"operation": key[2], "route": key[0] if key[0] in ("ipv6", "ipv4") else "fallback",
                 "state": "half_open" if route.probing else "open" if route.reason else "closed",
                 "reason": route.reason, "retry_in": max(0.0, route.until - time.monotonic()),
                 "active": route.active} for key, route in _routes.items()]


def _failure(error: Exception) -> str | None:
    if isinstance(error, SwoopHTTPError):
        if error.status_code in (403, 429):
            return f"http_{error.status_code}"
        return "http_redirect" if 300 <= error.status_code < 400 else "http_unavailable" if error.status_code >= 500 else None
    if isinstance(error, SwoopUpstreamError):
        return "compact_internal" if error.grpc_code == 13 and error.type_url is None else None
    if isinstance(error, SwoopTransportError):
        return "timeout" if "timeout" in type(error.__cause__).__name__.lower() or "budget" in str(error).lower() else "network"
    if isinstance(error, SwoopParseError):
        return "malformed"
    return None


def _validate(text: str, operation: str) -> tuple[bool, bool]:
    """Return (ambiguous, inventory). A protocol-valid empty is always healthy."""
    from . import rpc
    from ._calendar import _calendar_frames, _parse_calendar_days
    from .decoder import raise_if_error_envelope
    import json

    if not text.strip() or text.strip() in (")]}'", "[]"):
        return True, False
    frames = _calendar_frames(text)
    raise_if_error_envelope(frames, endpoint=operation)
    payloads = [frame for frame in frames if isinstance(frame, list) and len(frame) > 2 and frame[0] == "wrb.fr" and isinstance(frame[2], str)]
    if not payloads:
        raise SwoopParseError("RPC response missing result frame")
    for frame in payloads:
        try:
            json.loads(frame[2])
        except (ValueError, TypeError) as exc:
            raise SwoopParseError("Malformed inner RPC payload") from exc
    if operation == "GetShoppingResults":
        result = rpc._parse_rpc_response(text)
        return False, bool(result and (result.best or result.other))
    if operation in ("GetCalendarPicker", "GetCalendarGrid", "GetCalendarGraph"):
        return False, bool(_parse_calendar_days(text, endpoint=operation))
    if operation == "GetBookingResults":
        return False, bool(rpc._parse_booking_rpc_response(text))
    return False, True


def post(url: str, body: bytes, headers: dict[str, str], *, transport: TransportConfig, deadline: float) -> Any:
    """At most three dispatches, same bytes/headers/URL and one deadline."""
    from .rpc import _get_transport_client, _post_with_retry
    operation = url.split("?")[0].rsplit("/", 1)[-1]
    primary_key = (transport.address_family, transport.impersonate or "chrome", operation)
    proxies = (None, *dict.fromkeys(transport.fallback_proxies))
    ambiguous_response = None
    last_error: Exception | None = None
    for index, proxy in enumerate(proxies):
        if index > 1 and ambiguous_response is not None:
            break
        if transport._cancel_check:
            transport._cancel_check()
        remaining_timeout(deadline)
        lease = _acquire(primary_key) if index == 0 else _acquire((proxy or "", transport.impersonate or "chrome", operation), probe=ambiguous_response is None)
        if lease is None:
            continue
        failure = None
        healthy = False
        delay = _COOLDOWN
        try:
            remaining = remaining_timeout(deadline)
            # Share the remaining operation budget among the bounded remaining routes.
            attempt_deadline = time.monotonic() + remaining / (len(proxies) - index)
            selected = replace(transport, proxy=proxy, address_family=transport.address_family if index == 0 else "auto",
                               fallback_proxies=(), retries=0, _operation_deadline=deadline)
            response = _post_with_retry(_get_transport_client(selected), url, body, headers,
                                       transport=selected, _deadline=attempt_deadline)
            ambiguous, inventory = _validate(response.text, operation)
            if transport._cancel_check:
                transport._cancel_check()
            remaining_timeout(deadline)
            if index == 0 and ambiguous:
                ambiguous_response = response
                continue  # Only one identical-query comparison on a healthy fallback.
            healthy = not ambiguous
            if index > 0 and ambiguous_response is not None and inventory:
                with _lock:
                    route = _routes.get(primary_key)
                    if route is not None:
                        route.reason = "hidden_inventory"
                        route.until = time.monotonic() + _COOLDOWN
                        route.generation += 1
            if ambiguous:
                raise SwoopParseError("RPC routes returned ambiguous empty bodies")
            return response
        except Exception as error:
            if transport._cancel_check:
                transport._cancel_check()
            failure = _failure(error)
            if failure is None:
                raise
            delay = getattr(error, "retry_after", None) or _COOLDOWN
            last_error = error
        finally:
            _finish(lease, failure, delay, healthy=healthy)
    if last_error is not None:
        raise last_error
    if ambiguous_response is not None:
        raise SwoopParseError("RPC response empty and no healthy comparison route available")
    raise SwoopTransportError("Google Flights routes cooling down or at concurrency limit")
