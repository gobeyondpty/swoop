"""Slow shopping, retry backoff, and page fallback share one total timeout."""
from types import SimpleNamespace
import time

import pytest

import swoop
import swoop.diagnostics as diagnostics
from swoop import rpc
from swoop.exceptions import SwoopHTTPError, SwoopRateLimitError, SwoopTransportError
from tests.test_search_page import rejected_exit


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = 0.0
        waits = []

        def sleep(self, duration):
            self.waits.append(duration)
            self.now += duration

    clock = Clock()
    monkeypatch.setattr(time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    monkeypatch.setattr(diagnostics, "perf_counter", lambda: clock.now)
    import random
    monkeypatch.setattr(random, "uniform", lambda *_: 0)
    return clock


def _slow_exit(monkeypatch, client, clock, *, post_seconds, get_seconds):
    post, get = client.post, client.get

    def slow_post(*args, **kwargs):
        clock.now += post_seconds
        return post(*args, **kwargs)

    def slow_get(*args, **kwargs):
        clock.now += get_seconds
        return get(*args, **kwargs)

    monkeypatch.setattr(client, "post", slow_post)
    monkeypatch.setattr(client, "get", slow_get)


def test_slow_compact_rejection_passes_only_remaining_budget_to_page(rejected_exit, monkeypatch, clock):
    _slow_exit(monkeypatch, rejected_exit, clock, post_seconds=1.5, get_seconds=0.25)
    recorder = swoop.DiagnosticsRecorder()
    result = swoop.search("JFK", "LAX", "2026-11-15", time_budget=2,
        transport=swoop.TransportConfig(retries=0, diagnostics=recorder))
    assert result.results and clock.now == 1.75
    assert rejected_exit.posts[0][1]["timeout"] == 2
    assert rejected_exit.gets[0][1]["timeout"] == 0.5
    snapshot = recorder.snapshot()
    assert snapshot.http_attempts == 2 and snapshot.page_attempts == snapshot.rpc_attempts == 1
    assert snapshot.network_ms == snapshot.elapsed_ms == 1750


@pytest.mark.parametrize("post_seconds", [2, 3])
def test_exhausted_post_never_dispatches_a_page(rejected_exit, monkeypatch, clock, post_seconds):
    _slow_exit(monkeypatch, rejected_exit, clock, post_seconds=post_seconds, get_seconds=0)
    recorder = swoop.DiagnosticsRecorder()
    with pytest.raises(SwoopTransportError):
        swoop.search("JFK", "LAX", "2026-11-15", time_budget=2,
            transport=swoop.TransportConfig(retries=0, diagnostics=recorder))
    assert len(rejected_exit.posts) == 1 and not rejected_exit.gets
    snapshot = recorder.snapshot()
    assert snapshot.http_attempts == 1 and snapshot.page_attempts == 0
    assert snapshot.queries[0].outcome == "error"


def test_late_page_success_is_a_timeout_not_results(rejected_exit, monkeypatch, clock):
    _slow_exit(monkeypatch, rejected_exit, clock, post_seconds=1.5, get_seconds=1.5)
    recorder = swoop.DiagnosticsRecorder()
    with pytest.raises(SwoopTransportError):
        swoop.search("JFK", "LAX", "2026-11-15", time_budget=2,
            transport=swoop.TransportConfig(retries=0, diagnostics=recorder))
    assert rejected_exit.gets[0][1]["timeout"] == 0.5 and clock.now == 3
    snapshot = recorder.snapshot()
    assert snapshot.http_attempts == 2 and snapshot.page_attempts == 1
    assert snapshot.attempts[-1].status_code == 200
    assert snapshot.attempts[-1].error_type == "SwoopTransportError"


def test_decode_time_is_not_added_back_before_fallback(rejected_exit, monkeypatch, clock):
    _slow_exit(monkeypatch, rejected_exit, clock, post_seconds=1.5, get_seconds=0)
    original = rpc._parse_rpc_response

    def decode(text):
        clock.now += 0.5
        return original(text)

    monkeypatch.setattr(rpc, "_parse_rpc_response", decode)
    recorder = swoop.DiagnosticsRecorder()
    with pytest.raises(SwoopTransportError):
        swoop.search("JFK", "LAX", "2026-11-15", time_budget=2,
            transport=swoop.TransportConfig(retries=0, diagnostics=recorder))
    assert len(rejected_exit.posts) == 1 and not rejected_exit.gets
    assert recorder.snapshot().decode_ms == 500


def _retry_client(clock, replies):
    class Client:
        calls = []

        def post(self, *args, **kwargs):
            self.calls.append(kwargs["timeout"])
            seconds, status = replies[len(self.calls) - 1]
            clock.now += seconds
            return SimpleNamespace(status_code=status, text="ok")

    return Client()


def test_retry_receives_remaining_timeout_after_network_and_backoff(clock):
    client = _retry_client(clock, [(2, 429), (1, 200)])
    recorder = swoop.DiagnosticsRecorder()
    response = rpc._post_with_retry(client, rpc.BOOKING_RPC_URL, b"", {},
        transport=swoop.TransportConfig(timeout=8, retries=2, diagnostics=recorder))
    assert response.text == "ok" and client.calls == [8, 5] and clock.waits == [1]
    snapshot = recorder.snapshot()
    assert snapshot.http_attempts == 2 and snapshot.retry_attempts == 1
    assert snapshot.retry_wait_ms == 1000 and snapshot.network_ms == 3000
    assert snapshot.elapsed_ms == 4000


def test_insufficient_retry_budget_preserves_429_without_wait_or_extra_dispatch(clock):
    client = _retry_client(clock, [(0.75, 429)])
    recorder = swoop.DiagnosticsRecorder()
    with pytest.raises(SwoopRateLimitError):
        rpc._post_with_retry(client, rpc.BOOKING_RPC_URL, b"", {},
            transport=swoop.TransportConfig(timeout=1, retries=2, diagnostics=recorder))
    assert client.calls == [1] and not clock.waits and clock.now == 0.75
    snapshot = recorder.snapshot()
    assert snapshot.http_attempts == 1 and snapshot.retry_attempts == 0
    assert snapshot.retry_wait_ms == 0 and snapshot.elapsed_ms == 750


def test_repeated_429s_cannot_reset_the_budget(clock):
    client = _retry_client(clock, [(0.2, 429), (0.2, 429)])
    recorder = swoop.DiagnosticsRecorder()
    with pytest.raises(SwoopRateLimitError):
        rpc._post_with_retry(client, rpc.BOOKING_RPC_URL, b"", {},
            transport=swoop.TransportConfig(timeout=3, retries=5, diagnostics=recorder))
    assert client.calls == pytest.approx([3, 1.8])
    assert clock.waits == [1] and clock.now == pytest.approx(1.4)
    assert recorder.snapshot().http_attempts == 2


def test_actual_wait_overrun_is_measured_and_cannot_dispatch_a_retry(clock, monkeypatch):
    client = _retry_client(clock, [(0.5, 429)])
    recorder = swoop.DiagnosticsRecorder()

    def slow_sleep(seconds):
        clock.waits.append(seconds)
        clock.now += seconds + 1  # Scheduling delay after the requested wait.

    monkeypatch.setattr(time, "sleep", slow_sleep)
    with pytest.raises(SwoopRateLimitError):
        rpc._post_with_retry(client, rpc.BOOKING_RPC_URL, b"", {},
            transport=swoop.TransportConfig(timeout=2.5, retries=2, diagnostics=recorder))
    assert client.calls == [2.5] and clock.waits == [1] and clock.now == 2.5
    snapshot = recorder.snapshot()
    assert snapshot.http_attempts == 1 and snapshot.retry_attempts == 0
    assert snapshot.retry_wait_ms == 2000 and snapshot.elapsed_ms == 2500


@pytest.mark.parametrize("timeout", [0, -1])
def test_expired_budget_does_not_invent_an_http_attempt(clock, timeout):
    client = _retry_client(clock, [])
    recorder = swoop.DiagnosticsRecorder()
    with pytest.raises(SwoopTransportError):
        rpc._post_with_retry(client, rpc.BOOKING_RPC_URL, b"", {},
            transport=swoop.TransportConfig(timeout=timeout, diagnostics=recorder))
    assert not client.calls and recorder.snapshot().http_attempts == 0


def test_client_setup_cannot_reset_the_http_budget(monkeypatch, clock):
    client = _retry_client(clock, [])

    def get_client(*_args):
        clock.now += 2
        return client

    monkeypatch.setattr(rpc, "_get_client", get_client)
    recorder = swoop.DiagnosticsRecorder()
    with pytest.raises(SwoopTransportError):
        rpc._http_post(rpc.BOOKING_RPC_URL, b"", transport=swoop.TransportConfig(timeout=1, diagnostics=recorder))
    assert not client.calls and recorder.snapshot().http_attempts == 0


@pytest.mark.parametrize("phase", ["send", "body"])
def test_page_transport_failure_stays_typed_with_original_cause(rejected_exit, monkeypatch, clock, phase):
    error = TimeoutError("private timeout")

    class Body:
        status_code = 200

        @property
        def text(self):
            raise error

    def get(*args, **kwargs):
        rejected_exit.gets.append((args, kwargs))
        if phase == "send":
            raise error
        return Body()

    monkeypatch.setattr(rejected_exit, "get", get)
    recorder = swoop.DiagnosticsRecorder()
    with pytest.raises(SwoopTransportError) as caught:
        swoop.search_raw("JFK", "LAX", "2026-11-15", transport=swoop.TransportConfig(timeout=2, diagnostics=recorder))
    assert caught.value.__cause__ is error
    assert recorder.snapshot().http_attempts == 2
    assert recorder.snapshot().attempts[-1].error_type == "TimeoutError"


def test_non_429_http_failure_is_not_reclassified_or_retried(clock):
    client = _retry_client(clock, [(0.1, 503)])
    with pytest.raises(SwoopHTTPError) as caught:
        rpc._post_with_retry(client, rpc.BOOKING_RPC_URL, b"", {}, transport=swoop.TransportConfig(timeout=1, retries=5))
    assert caught.value.status_code == 503 and client.calls == [1] and not clock.waits
