"""Bounded exact-RPC failover and route health; no query or tenant state is shared."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
import threading
import time
import random
from typing import Any, Callable, TypeVar

from ._transport_budget import remaining_timeout, request_timeout, transport_deadline
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
    failures: int = 0


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
            route.failures += 1
            cooldown = min(300.0, _COOLDOWN * 2 ** min(route.failures - 1, 4))
            route.until = time.monotonic() + max(cooldown * random.uniform(1.0, 1.1), delay)
            route.generation += 1
        elif half_open and not healthy:
            route.until = max(route.until, time.monotonic() + _COOLDOWN)
        elif half_open and healthy:
            route.reason = None
            route.failures = 0
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


class AmbiguousResponse(SwoopParseError):
    """No protocol inventory statement; compare once without poisoning health."""


@dataclass
class RouteAttempt:
    pooled: Any
    deadline: float
    probe_deadline: float
    index: int
    operation: str
    failure: Exception | None = None
    validated: bool = False
    ambiguity: AmbiguousResponse | None = None


def note_failure(transport: TransportConfig, error: Exception) -> None:
    state = transport._route_state
    if state is not None and isinstance(error, AmbiguousResponse):
        state.ambiguity = error
    elif state is not None and state.failure is None and _failure(error) is not None:
        state.failure = error


def validated_response(transport: TransportConfig) -> None:
    """A parsed RPC, including valid no-inventory, permits remaining session work."""
    state = transport._route_state
    if state is not None:
        state.validated = True


T = TypeVar("T")


def run(operation: str, *, transport: TransportConfig, action: Callable[[TransportConfig], T],
        inventory: Callable[[T], bool] = bool, deadline: float | None = None,
        primary_budget: float = 2.0) -> T:
    """Shared route choice/health for RPCs and complete route-bound sessions.

    Session actions rebuild their own bootstrap/params on each selected route.
    The outer deadline never moves; a short initial proof budget is released
    only after an actual RPC parses successfully, never on an HTTP 200 alone.
    """
    from .rpc import _get_transport_client
    from ._session import RouteBusy
    if transport._route_state is not None:
        return action(transport)
    deadline = transport_deadline(transport.timeout, deadline)
    if transport._operation_deadline is not None:
        deadline = min(deadline, transport._operation_deadline)
    proxies = (transport.proxy, *dict.fromkeys(transport.fallback_proxies))
    ambiguous = False
    last_error: Exception | None = None
    primary_key = (transport.address_family, transport.impersonate or "chrome", "google")
    for index, proxy in enumerate(proxies):
        if ambiguous and index > 1:
            break
        request_timeout(transport, deadline)
        key = primary_key if index == 0 else (proxy or "", transport.impersonate or "chrome", "google")
        lease = _acquire(key, probe=not ambiguous) if transport.fallback_proxies else None
        if transport.fallback_proxies and lease is None:
            continue
        reason = None
        healthy = False
        delay = _COOLDOWN
        try:
            remaining = remaining_timeout(deadline)
            attempt_deadline = time.monotonic() + remaining / (len(proxies) - index)
            selected = replace(transport, proxy=proxy, address_family=transport.address_family if index == 0 else "auto",
                               fallback_proxies=(), retries=0, _operation_deadline=deadline)
            pooled = _get_transport_client(selected)
            probe_deadline = attempt_deadline
            if index == 0 and transport.fallback_proxies:
                probe_deadline = min(probe_deadline, time.monotonic() + (transport.primary_timeout or primary_budget))
            state = RouteAttempt(pooled, attempt_deadline, probe_deadline, index, operation)
            selected = replace(selected, _route_state=state)
            result = action(selected)
            if state.failure is not None:
                raise state.failure
            if state.ambiguity is not None:
                raise state.ambiguity
            request_timeout(selected, attempt_deadline)
            healthy = True
            if ambiguous and inventory(result):
                with _lock:
                    primary = _routes.get(primary_key)
                    if primary is not None:
                        primary.reason = "hidden_inventory"
                        primary.until = time.monotonic() + _COOLDOWN
                        primary.generation += 1
            return result
        except RouteBusy:
            continue
        except AmbiguousResponse as error:
            last_error = error
            if index == 0:
                ambiguous = True
            else:
                break
        except Exception as error:
            if transport._cancel_check:
                transport._cancel_check()
            reason = _failure(error)
            if reason is None:
                raise
            delay = getattr(error, "retry_after", None) or _COOLDOWN
            last_error = error
        finally:
            if lease is not None:
                _finish(lease, reason, delay, healthy=healthy)
    if last_error is not None:
        raise last_error
    raise SwoopTransportError("Google routes cooling down or at concurrency limit")


def post(url: str, body: bytes, headers: dict[str, str], *, transport: TransportConfig, deadline: float) -> Any:
    """Exact-RPC replay using the same policy as session-based entry points."""
    from .rpc import _post_with_retry
    inventory = [False]
    operation = url.split("?")[0].rsplit("/", 1)[-1]
    def action(selected: TransportConfig) -> Any:
        from ._session import BudgetedClient
        response = _post_with_retry(BudgetedClient(selected._route_state.pooled, selected), url, body, headers,
                                    transport=selected, _deadline=selected._route_state.deadline)
        ambiguous, inventory[0] = _validate(response.text, operation)
        if ambiguous:
            raise AmbiguousResponse("RPC response contains no inventory statement")
        request_timeout(selected)
        validated_response(selected)
        return response
    return run(operation, transport=transport, action=action, deadline=deadline,
               inventory=lambda response: inventory[0])
