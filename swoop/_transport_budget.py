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
