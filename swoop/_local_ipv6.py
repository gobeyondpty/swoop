"""Request-owned native source leases; the root broker owns address provisioning."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import ipaddress
import json
import logging
import math
import socket
import struct
import threading
import time
from typing import Any, Callable, Iterator

from ._transport_budget import remaining_timeout, transport_deadline
from .exceptions import SwoopTransportError
from .models import TransportConfig

_PROTOCOL = 1
_BROKER_UID = 0
_MAX_HOLD = 600.0
_HANDSHAKE = .5
_RECOVERY_GRACE = 30.0
_log = logging.getLogger(__name__)


class LocalSourceUnavailable(SwoopTransportError):
    """Local capacity/lifecycle failure, never evidence that Google is unhealthy."""
    _local_only = True


class SourceLease:
    def __init__(self, channel: socket.socket, address: str, token: str, mode: str, deadline: float):
        self.address, self.token, self.mode = address, token, mode
        self._channel, self.deadline = channel, deadline
        self._lock = threading.RLock()
        self._clients: dict[str, Any] = {}
        self._references = 0
        self._closing = False
        self._finalized = False
        self._reason: str | None = None
        self._retry_after = 0.0

    def check(self) -> None:
        with self._lock:
            if self._closing:
                raise LocalSourceUnavailable("The source operation has finished")

    def retain(self) -> Callable[[], None]:
        with self._lock:
            self.check()
            self._references += 1
        released = False
        guard = threading.Lock()
        def release() -> None:
            nonlocal released
            with guard:
                if released:
                    return
                released = True
            with self._lock:
                self._references -= 1
            self._drain()
        return release

    @contextmanager
    def activity(self) -> Iterator[None]:
        release = self.retain()
        try:
            yield
        finally:
            release()

    def client(self, key: str, factory: Callable[[], Any]) -> Any:
        with self._lock:
            self.check()
            if key not in self._clients:
                self._clients[key] = factory()
            return self._clients[key]

    def keep(self, client: Any) -> Any:
        with self._lock:
            self.check()
            self._clients[f"isolated:{id(client)}"] = client
        return client

    def failed(self, reason: str, retry_after: float) -> None:
        with self._lock:
            self._reason = reason
            if math.isfinite(retry_after):
                self._retry_after = max(self._retry_after, retry_after)

    def finish(self) -> None:
        with self._lock:
            self._closing = True
        self._drain()

    def _drain(self) -> None:
        with self._lock:
            if not self._closing or self._references or self._finalized:
                return
            self._finalized = True
            clients = list(self._clients.values())
        failed = False
        for client in clients:
            try:
                client.close()
            except Exception as error:
                failed = True
                _log.warning("Native client close failed (%s); retaining source through its deadline", type(error).__name__)
        if failed:
            # A close failure must not let the broker remove a potentially used
            # address early. All dispatches have the immutable <=600s deadline.
            timer = threading.Timer(max(0, self.deadline + _RECOVERY_GRACE - time.monotonic()), self._release_socket)
            timer.daemon = True
            timer.start()
        else:
            self._release_socket()

    def _release_socket(self) -> None:
        try:
            payload = dict(v=_PROTOCOL, op="release", lease=self.token,
                           reason=self._reason, retry_after=self._retry_after)
            self._channel.settimeout(.05)
            self._channel.sendall(json.dumps(payload, allow_nan=False).encode() + b"\n")
        except OSError:
            pass  # EOF also releases; the broker's journal covers restart.
        finally:
            self._channel.close()
            with self._lock:
                self._clients.clear()


def _acquire(transport: TransportConfig, deadline: float) -> SourceLease:
    if transport._cancel_check:
        transport._cancel_check()
    channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        handshake_deadline = min(deadline, time.monotonic() + 1.0)
        channel.settimeout(min(_HANDSHAKE, remaining_timeout(handshake_deadline)))
        channel.connect(str(transport.local_ipv6_socket))
        _, uid, _ = struct.unpack("3i", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != _BROKER_UID:
            raise LocalSourceUnavailable("Unexpected local IPv6 broker owner")
        hold = min(_MAX_HOLD, remaining_timeout(deadline))
        channel.sendall(json.dumps(dict(v=_PROTOCOL, op="acquire", mode=transport.local_ipv6_mode,
                                       hold_seconds=hold), allow_nan=False).encode() + b"\n")
        data = bytearray()
        while b"\n" not in data:
            channel.settimeout(min(_HANDSHAKE, remaining_timeout(handshake_deadline)))
            part = channel.recv(4097 - len(data))
            if not part:
                raise LocalSourceUnavailable("Local IPv6 broker disconnected")
            data.extend(part)
            if len(data) > 4096:
                raise LocalSourceUnavailable("Local IPv6 broker reply exceeds its limit")
        line, _, trailing = data.partition(b"\n")
        reply = json.loads(line)
        if trailing or not isinstance(reply, dict) or type(reply.get("v")) is not int or reply["v"] != _PROTOCOL or reply.get("ok") is not True:
            raise LocalSourceUnavailable("Local IPv6 broker is unavailable")
        address = ipaddress.IPv6Address(reply["address"])
        prefix = ipaddress.IPv6Network(str(transport.local_ipv6_prefix))
        token = reply["lease"]
        if (address.scope_id is not None or address not in prefix or address.is_unspecified or address.is_multicast
                or reply.get("mode") != transport.local_ipv6_mode or not isinstance(token, str)
                or not 1 <= len(token) <= 128 or any(not (c.isascii() and (c.isalnum() or c in "_-")) for c in token)):
            raise LocalSourceUnavailable("Invalid local IPv6 broker lease")
        if transport._cancel_check:
            transport._cancel_check()
        remaining_timeout(deadline)
        return SourceLease(channel, str(address), token, transport.local_ipv6_mode, deadline)
    except BaseException:
        channel.close()
        raise


@contextmanager
def operation(transport: TransportConfig) -> Iterator[TransportConfig]:
    """One source per public operation, including nested/fan-out session work."""
    if transport._source_lease is not None:
        with transport._source_lease.activity():
            yield transport
        return
    if not transport.local_ipv6_socket:
        yield transport
        return
    deadline = transport_deadline(min(_MAX_HOLD, transport.timeout), transport._operation_deadline)
    selected = replace(transport, _operation_deadline=deadline)
    from . import _adaptive
    if not _adaptive.source_prefix_available(selected):
        yield replace(selected, _skip_primary=True)
        return
    try:
        lease = _acquire(selected, deadline)
    except Exception as error:
        if selected._cancel_check:
            selected._cancel_check()
        remaining_timeout(deadline)
        if not selected.fallback_proxies:
            raise LocalSourceUnavailable("No local IPv6 source is available") from error
        # Never silently send unbound direct when an address was requested.
        yield replace(selected, _skip_primary=True)
        return
    release = lease.retain()
    try:
        yield replace(selected, local_address=lease.address, _source_lease=lease)
    finally:
        lease.finish()
        release()


def source_id(address: str) -> str:
    return hashlib.sha256(address.encode()).hexdigest()[:12]
