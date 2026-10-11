"""A logical shopping call can include rejected RPC and public-page HTTP work."""
import json

import pytest

import swoop
import swoop.diagnostics as diagnostics
import swoop._search_page as page
from swoop import rpc
from swoop.exceptions import SwoopHTTPError, SwoopParseError, SwoopTransportError, SwoopUpstreamError
from tests.factories import make_full_response
from tests.test_search_page import rejected_exit


def test_one_logical_rpc_tracks_rejected_post_plus_fallback_get_and_both_decodes(rejected_exit, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(diagnostics, "perf_counter", lambda: clock[0])
    original_post, original_get = rejected_exit.post, rejected_exit.get
    original_rpc_decode, original_page_decode = rpc._parse_rpc_response, page.decode_result

    def post(*args, **kwargs):
        clock[0] += 0.1
        return original_post(*args, **kwargs)

    def get(*args, **kwargs):
        clock[0] += 0.2
        return original_get(*args, **kwargs)

    def rpc_decode(text):
        try:
            return original_rpc_decode(text)
        finally:
            clock[0] += 0.005

    def page_decode(data):
        result = original_page_decode(data)
        clock[0] += 0.025
        return result

    monkeypatch.setattr(rejected_exit, "post", post)
    monkeypatch.setattr(rejected_exit, "get", get)
    monkeypatch.setattr(rpc, "_parse_rpc_response", rpc_decode)
    monkeypatch.setattr(page, "decode_result", page_decode)
    recorder = swoop.DiagnosticsRecorder()
    result = swoop.search("JFK", "LAX", "2026-11-15", transport=swoop.TransportConfig(diagnostics=recorder, retries=0))
    snapshot = recorder.snapshot()
    assert result.rpc_calls == 1 and result.is_complete is False
    assert snapshot.http_attempts == len(rejected_exit.posts) + len(rejected_exit.gets) == 2
    assert snapshot.rpc_attempts == snapshot.page_attempts == 1
    assert [attempt.transport for attempt in snapshot.attempts] == ["rpc", "page"]
    assert snapshot.network_ms == pytest.approx(300)
    assert snapshot.decode_ms == pytest.approx(30)
    assert snapshot.elapsed_ms == pytest.approx(330)
    assert snapshot.queries[0].result_scope == "limited"
    assert snapshot.queries[0].outcome == "success"
    assert snapshot.queries[0].raw_result_count == snapshot.queries[0].decoded_result_count


@pytest.mark.parametrize("status,error", [(503, SwoopHTTPError), (200, SwoopParseError)])
def test_failed_fallback_remains_a_failure_with_all_attempts(rejected_exit, status, error):
    rejected_exit.status = status
    rejected_exit.html = "broken page"
    recorder = swoop.DiagnosticsRecorder()
    with pytest.raises(error) as raised:
        swoop.search("JFK", "LAX", "2026-11-15", transport=swoop.TransportConfig(diagnostics=recorder, retries=0))
    snapshot = recorder.snapshot()
    assert snapshot.http_attempts == 2 and snapshot.page_attempts == 1
    assert snapshot.attempts[-1].status_code == status
    assert snapshot.queries[0].outcome == "error"
    assert raised.value.diagnostics == snapshot


def test_failed_page_body_is_unknown_bytes_and_never_empty_inventory(rejected_exit, monkeypatch):
    class FailedBody:
        status_code = 200

        @property
        def text(self):
            raise TimeoutError("private body-read failure")

    def get(*args, **kwargs):
        rejected_exit.gets.append((args, kwargs))
        return FailedBody()

    monkeypatch.setattr(rejected_exit, "get", get)
    recorder = swoop.DiagnosticsRecorder()
    with pytest.raises(SwoopTransportError) as raised:
        swoop.search("JFK", "LAX", "2026-11-15", transport=swoop.TransportConfig(diagnostics=recorder, retries=0))
    assert isinstance(raised.value.__cause__, TimeoutError)
    snapshot = recorder.snapshot()
    assert snapshot.http_attempts == 2
    assert snapshot.attempts[-1].transport == "page"
    assert snapshot.attempts[-1].response_bytes is None
    assert snapshot.attempts[-1].error_type == "TimeoutError"
    assert snapshot.queries[0].outcome == "error"


def test_empty_page_fallback_preserves_limited_coverage(rejected_exit):
    rejected_exit.html = "AF_initDataCallback({key:'ds:1',data:" + json.dumps(make_full_response()) + "});"
    recorder = swoop.DiagnosticsRecorder()
    result = swoop.search("JFK", "LAX", "2026-11-15", transport=swoop.TransportConfig(diagnostics=recorder, retries=0))
    query = recorder.snapshot().queries[0]
    assert result.results == [] and result.is_complete is False
    assert query.outcome == "empty" and query.result_scope == "limited"
    assert query.raw_result_count == query.decoded_result_count == 0


def test_unsupported_page_intent_does_not_invent_a_get_attempt(rejected_exit):
    recorder = swoop.DiagnosticsRecorder()
    leg = rpc._normalize_rpc_leg("JFK", "LAX", "2026-11-15", earliest_departure=6)
    with pytest.raises(SwoopUpstreamError) as raised:
        rpc._search_from_legs([leg], transport=swoop.TransportConfig(diagnostics=recorder, retries=0))
    snapshot = recorder.snapshot()
    assert snapshot.http_attempts == 1 and snapshot.page_attempts == 0
    assert not rejected_exit.gets
    assert raised.value.grpc_code == 13
    assert snapshot.queries[0].outcome == "error"
