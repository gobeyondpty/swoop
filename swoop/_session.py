"""Route-bound session execution and budgeted HTTP; no endpoint payload builders."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from functools import wraps
import inspect
import threading
from typing import Any, Callable, Iterator, TypeVar, cast
from uuid import uuid4
from urllib.parse import urljoin, urlsplit

from ._transport_budget import request_timeout, transport_deadline
from .diagnostics import measure_http
from .exceptions import SwoopError, SwoopHTTPError, SwoopRateLimitError, SwoopTransportError
from .models import TransportConfig
from .response_observer import ResponseOperation, read_response_body


class SessionClient:
    """Cached route/service jar with an immutable identity for session-param caches."""
    def __init__(self, client: Any):
        self._client = client
        self.session_id = uuid4().hex

    def get(self, *args: Any, **kwargs: Any) -> Any:
        return self._client.get(*args, **kwargs)

    def post(self, *args: Any, **kwargs: Any) -> Any:
        return self._client.post(*args, **kwargs)


class RouteBusy(SwoopTransportError):
    """Local session capacity, not evidence that a remote route is unhealthy."""


class SessionPool:
    """Two independently leased cookie jars per cached route/POS/service.

    The factory captures only immutable client settings, never request state.
    An idle first slot is reused for warm sessions. Concurrent operations cannot
    overwrite one another's cookies or session parameters mid-transaction.
    """
    def __init__(self, factory: Callable[[], Any]):
        self._factory = factory
        self._slots: list[tuple[SessionClient, bool]] = []
        self._lock = threading.Lock()

    @contextmanager
    def borrow(self, transport: TransportConfig) -> Iterator[SessionClient]:
        request_timeout(transport)
        with self._lock:
            index = next((i for i, (_, busy) in enumerate(self._slots) if not busy), None)
            if index is None:
                if len(self._slots) >= 2:
                    raise RouteBusy("Google session capacity is busy")
                index = len(self._slots)
                client = SessionClient(self._factory())
                self._slots.append((client, True))
            else:
                client = self._slots[index][0]
                self._slots[index] = (client, True)
        try:
            request_timeout(transport)
            yield client
        finally:
            with self._lock:
                self._slots[index] = (client, False)


@contextmanager
def session_lock(lock: Any, transport: TransportConfig) -> Iterator[None]:
    """Wait cooperatively for cache single-flight without extending the budget."""
    if transport._route_state is None and transport.address_family == "auto":
        with lock:
            yield
        return
    while not lock.acquire(timeout=min(.05, request_timeout(transport))):
        request_timeout(transport)
    try:
        request_timeout(transport)
        yield
    finally:
        lock.release()


class BudgetedResponse:
    def __init__(self, response: Any, transport: TransportConfig):
        self._response, self._transport = response, transport
        self.status_code = response.status_code
        self.headers = getattr(response, "headers", {})
        self._text: str | None = None

    @property
    def text(self) -> str:
        from ._adaptive import note_failure
        request_timeout(self._transport)
        if self._text is None:
            try:
                self._text = self._response.text
            except SwoopError as error:
                note_failure(self._transport, error)
                raise
            except Exception as error:
                wrapped = SwoopTransportError("Google response body read failed")
                wrapped.__cause__ = error
                note_failure(self._transport, wrapped)
                raise wrapped from error
        request_timeout(self._transport)
        assert self._text is not None
        return self._text


class BudgetedClient:
    """Request-owned facade. Only the anonymous underlying client is pooled."""
    def __init__(self, client: Any, transport: TransportConfig):
        self._client, self._transport = client, transport
        self.session_id = getattr(client, "session_id", "")

    def get(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("get", *args, **kwargs)

    def post(self, *args: Any, **kwargs: Any) -> Any:
        return self._dispatch("post", *args, **kwargs)

    def _dispatch(self, method: str, *args: Any, **kwargs: Any) -> Any:
        from ._adaptive import note_failure
        timeout = request_timeout(self._transport)
        kwargs["timeout"] = min(kwargs.get("timeout", timeout), timeout)
        try:
            response = getattr(self._client, method)(*args, **kwargs)
            return BudgetedResponse(response, self._transport)
        except SwoopError as error:
            note_failure(self._transport, error)
            raise
        except Exception as error:
            wrapped = SwoopTransportError("Google transport failed while sending a request")
            wrapped.__cause__ = error
            note_failure(self._transport, wrapped)
            raise wrapped from error


def client_for_session(transport: TransportConfig, legacy_factory: Callable[..., Any]) -> Any:
    if transport._route_state is not None:
        return BudgetedClient(transport._route_state.pooled, transport)
    if transport.address_family != "auto":
        from .rpc import _get_transport_client
        return _get_transport_client(transport)
    return legacy_factory(transport.proxy, transport.impersonate)


def get_page(client: Any, url: str, *, transport: TransportConfig,
             operation: ResponseOperation, headers: dict[str, str]) -> str:
    """Bounded same-origin Travel redirects and a shared send/read deadline."""
    from .rpc import _retry_after
    from ._adaptive import note_failure
    if transport._route_state is None and transport.address_family == "auto":
        # Preserve the legacy observer contract: Deals needs only cookies;
        # Explore/Hotels require a body and expose the original read exception.
        response = client.get(url, headers=headers, timeout=request_timeout(transport))
        required = operation != "deals"
        return read_response_body(response, transport, operation=operation, phase="page", method="GET",
                                  required=required) if required or transport.response_observer else ""
    for redirect in range(3):
        timeout = request_timeout(transport)
        try:
            with measure_http(transport, operation, "page", "GET", redirect) as measurement:
                response = client.get(url, headers=headers, timeout=timeout)
                status = response.status_code
                measurement.status_code = status
                text = read_response_body(response, transport, operation=operation, phase="page", method="GET",
                                          retry_index=redirect, required=status == 200) if status == 200 or transport.response_observer else ""
                if status == 200:
                    measurement.body(text)
                    request_timeout(transport)
                    return text
            if 300 <= status < 400 and redirect < 2:
                location = getattr(response, "headers", {}).get("location")
                if isinstance(location, (bytes, bytearray, memoryview)):
                    location = bytes(location).decode("utf-8", errors="replace")
                if isinstance(location, str):
                    target = urljoin(url, location)
                    parsed, original = urlsplit(target), urlsplit(url)
                    if parsed.scheme == "https" and parsed.netloc == original.netloc and parsed.path.startswith("/travel/"):
                        url = target
                        continue
            error = SwoopRateLimitError() if status == 429 else SwoopHTTPError(status)
            error.retry_after = _retry_after(getattr(response, "headers", {}))
            raise error
        except SwoopError as error:
            note_failure(transport, error)
            raise
        except Exception as error:
            wrapped = SwoopTransportError("Google session bootstrap failed")
            wrapped.__cause__ = error
            note_failure(transport, wrapped)
            raise wrapped from error
    raise SwoopHTTPError(302)


F = TypeVar("F", bound=Callable[..., Any])


def routed_session(scope: str, *, inventory: Callable[[Any], bool], primary_budget: float = 5.0) -> Callable[[F], F]:
    def decorate(function: F) -> F:
        signature = inspect.signature(function)
        default = signature.parameters["transport"].default
        @wraps(function)
        def routed(*args: Any, **kwargs: Any) -> Any:
            transport = kwargs.get("transport", default)
            if transport.address_family == "auto" and not transport.fallback_proxies or transport._route_state is not None:
                return function(*args, **kwargs)
            from ._adaptive import run
            selected = replace(transport, _session_scope=scope)
            def action(config: TransportConfig) -> Any:
                state = config._route_state
                pool = state.pooled
                if not isinstance(pool, SessionPool):
                    return function(*args, **{**kwargs, "transport": config})
                with pool.borrow(config) as client:
                    state.pooled = client
                    try:
                        return function(*args, **{**kwargs, "transport": config})
                    finally:
                        state.pooled = pool
            return run(scope, transport=selected, inventory=inventory, primary_budget=primary_budget,
                       action=action)
        return cast(F, routed)
    return decorate


def supports_policy(function: Any) -> bool:
    """Only SDK-marked entry points may opt into the unified policy in adapters."""
    return getattr(function, "_swoop_shared_routing", False) is True


def mark_policy(function: F) -> F:
    """Keep one request-owned deadline through staged/fan-out public operations."""
    setattr(function, "_swoop_shared_routing", True)
    signature = inspect.signature(function)
    default = signature.parameters["transport"].default
    @wraps(function)
    def bounded(*args: Any, **kwargs: Any) -> Any:
        transport = kwargs.get("transport", default)
        if transport.address_family == "auto" and not transport.fallback_proxies:
            return function(*args, **kwargs)
        deadline = transport_deadline(transport.timeout, transport._operation_deadline)
        budget = kwargs.get("time_budget")
        if budget is not None:
            deadline = min(deadline, transport_deadline(budget))
        selected = replace(transport, _operation_deadline=deadline)
        return function(*args, **{**kwargs, "transport": selected})
    setattr(bounded, "_swoop_shared_routing", True)
    return cast(F, bounded)
