"""One monotonic timeout across attempts and alternate transports."""
from __future__ import annotations

import math
import time
from typing import Optional

from .exceptions import SwoopTransportError


def transport_deadline(timeout: float, deadline: Optional[float] = None) -> float:
    """Start a budget or tighten an existing one; never extend its deadline."""
    if not math.isfinite(timeout):
        raise SwoopTransportError("Google Flights transport timeout must be finite")
    own_deadline = time.monotonic() + max(0, timeout)
    return min(own_deadline, deadline) if deadline is not None else own_deadline


def remaining_timeout(deadline: float) -> float:
    """Check before dispatch, outside attempt-measurement scopes."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SwoopTransportError("Google Flights transport budget exhausted")
    return remaining


def request_timeout(transport: object, deadline: Optional[float] = None) -> float:
    """Budget/cancellation check for every HTTP dispatch and deferred body read."""
    from typing import Any, cast
    config = cast(Any, transport)
    if config._cancel_check:
        config._cancel_check()
    if config._source_lease is not None:
        config._source_lease.check()
    state = config._route_state
    if state is not None:
        if state.failure is not None:
            raise state.failure
        if state.ambiguity is not None:
            raise state.ambiguity
        bound = state.deadline
        if not state.validated:
            bound = min(bound, state.probe_deadline)
    else:
        bound = config._operation_deadline
    if deadline is not None:
        bound = min(bound, deadline) if bound is not None else deadline
    timeout = config.request_timeout or config.timeout
    if bound is not None:
        timeout = min(timeout, remaining_timeout(bound))
    return timeout
