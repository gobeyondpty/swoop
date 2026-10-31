"""Offline wire-preservation, route-health and native-family regressions."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from swoop import TransportConfig
from swoop import rpc
from swoop import _adaptive as adaptive
from swoop._native import NativeClient
from swoop.exceptions import SwoopHTTPError, SwoopParseError, SwoopTransportError, SwoopUpstreamError

EMPTY = json.dumps([["wrb.fr", None, "null"]])
COMPACT = json.dumps([["wrb.fr", None, None, None, None, [13]]])
INVALID = json.dumps([["wrb.fr", None, None, None, None, [3]]])
DETAILED = json.dumps([["wrb.fr", None, None, None, None,
                      [13, None, [["type.googleapis.com/travel.frontend.flights.ErrorResponse"]]]]])
URL = rpc.SHOPPING_RPC_URL


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    adaptive._routes.clear()
    rpc._clients.clear()
    monkeypatch.setattr(adaptive.time, "monotonic", lambda: 100.0)
    # time is a module singleton, so rpc/budget see the same clock.
    yield
    adaptive._routes.clear()
    rpc._clients.clear()


def config(**kwargs):
    return TransportConfig(address_family="ipv6", fallback_proxies=("socks5h://trusted:1082",), timeout=10, **kwargs)


def client(monkeypatch, effects):
    calls = []
    responses = iter(effects)
    def get(transport):
        calls.append(transport)
        def post(*args, **kwargs):
            item = next(responses)
            if isinstance(item, BaseException):
                raise item
            return SimpleNamespace(status_code=item[0], text=item[1], headers=item[2] if len(item) > 2 else {})
        return SimpleNamespace(post=post)
    monkeypatch.setattr(rpc, "_get_transport_client", get)
    return calls


def send(transport=None):
    return adaptive.post(URL, b"exact-party-selector-bags-filters", {"x-pos": "US-USD"}, transport=transport or config(), deadline=110)


@pytest.mark.parametrize("first,reason", [
    ((403, "blocked"), "http_403"), ((429, "", {"Retry-After": "120"}), "http_429"),
    ((503, ""), "http_unavailable"), ((200, COMPACT), "compact_internal"),
    ((200, "<html>captcha</html>"), "malformed"), (OSError("connection refused"), "network"),
    (TimeoutError("read"), "timeout"),
])
def test_fallback_classification_cooldown_and_recovery(monkeypatch, first, reason):
    calls = client(monkeypatch, [first, (200, EMPTY), (200, EMPTY), (200, EMPTY)])
    assert send().text == EMPTY
    assert [c.proxy for c in calls] == [None, "socks5h://trusted:1082"]
    assert adaptive.health()[0]["reason"] == reason
    send()
    assert calls[-1].proxy == "socks5h://trusted:1082"
    monkeypatch.setattr(adaptive.time, "monotonic", lambda: 221.0)
    adaptive.post(URL, b"same", {}, transport=config(), deadline=231)
    assert calls[-1].proxy is None
    assert adaptive.health()[0]["state"] == "closed"


@pytest.mark.parametrize("text,error", [(EMPTY, None), (INVALID, SwoopUpstreamError), (DETAILED, SwoopUpstreamError)])
def test_valid_empty_and_query_or_detailed_errors_do_not_failover(monkeypatch, text, error):
    calls = client(monkeypatch, [(200, text)])
    if error:
        with pytest.raises(error):
            send()
    else:
        assert send().text == EMPTY
    assert len(calls) == 1
    assert adaptive.health()[0]["reason"] is None


def test_identical_bytes_headers_selector_and_one_deadline(monkeypatch):
    attempts = []
    def post(client, url, body, headers, *, transport, _deadline):
        attempts.append((url, body, headers.copy(), transport, _deadline))
        if len(attempts) == 1:
            raise SwoopHTTPError(403)
        return rpc._HTTPResponse(200, EMPTY)
    monkeypatch.setattr(rpc, "_get_transport_client", lambda t: None)
    monkeypatch.setattr(rpc, "_post_with_retry", post)
    send()
    assert attempts[0][:3] == attempts[1][:3]
    assert [a[4] for a in attempts] == [105, 110]
    assert all(a[3]._operation_deadline == 110 and a[3].retries == 0 for a in attempts)


def test_expired_budget_and_cancellation_do_not_dispatch_or_leak(monkeypatch):
    calls = client(monkeypatch, [])
    with pytest.raises(SwoopTransportError):
        adaptive.post(URL, b"x", {}, transport=config(), deadline=99)
    def cancel():
        raise RuntimeError("cancelled")
    with pytest.raises(RuntimeError, match="cancelled"):
        send(config(_cancel_check=cancel))
    assert calls == []
    assert adaptive.health() == []


def test_cancellation_after_failure_stops_fallback(monkeypatch):
    cancelled = False
    def cancel():
        if cancelled:
            raise RuntimeError("cancelled")
    def post(*args, **kwargs):
        nonlocal cancelled
        cancelled = True
        raise SwoopTransportError("network")
    monkeypatch.setattr(rpc, "_get_transport_client", lambda t: None)
    monkeypatch.setattr(rpc, "_post_with_retry", post)
    with pytest.raises(RuntimeError, match="cancelled"):
        send(config(_cancel_check=cancel))
    assert adaptive.health()[0]["active"] == 0


def test_ambiguous_body_compares_once_without_marking_empty_blocked(monkeypatch):
    calls = client(monkeypatch, [(200, ""), (200, EMPTY)])
    assert send().text == EMPTY
    assert len(calls) == 2
    assert adaptive.health()[0]["reason"] is None


def test_ambiguous_body_only_inventory_proof_opens_direct(monkeypatch):
    client(monkeypatch, [(200, ""), (200, "inventory")])
    original = adaptive._validate
    monkeypatch.setattr(adaptive, "_validate", lambda text, op: (False, True) if text == "inventory" else original(text, op))
    assert send().text == "inventory"
    assert adaptive.health()[0]["reason"] == "hidden_inventory"


def test_single_half_open_probe_and_late_success_cannot_close_new_failure():
    key = ("ipv6", "chrome", "GetShoppingResults")
    first = adaptive._acquire(key)
    late = adaptive._acquire(key)
    adaptive._finish(first, "network")
    adaptive._finish(late, None, healthy=True)
    assert adaptive._acquire(key) is None
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(adaptive.time, "monotonic", lambda: 131.0)
        with ThreadPoolExecutor(max_workers=16) as executor:
            leases = list(executor.map(lambda _: adaptive._acquire(key), range(32)))
        leases = [lease for lease in leases if lease]
        assert len(leases) == 1
        adaptive._finish(leases[0], None, healthy=True)
    assert adaptive.health()[0]["state"] == "closed"


def test_state_and_concurrency_are_bounded():
    key = ("ipv6", "chrome", "shopping")
    leases = [adaptive._acquire(key) for _ in range(9)]
    assert leases[-1] is None
    for lease in leases[:-1]:
        adaptive._finish(lease, None)
    for i in range(200):
        lease = adaptive._acquire(("ipv6", str(i), "booking"))
        adaptive._finish(lease, None)
    assert len(adaptive._routes) == adaptive._MAX_ROUTES


@pytest.mark.parametrize("family,strategy,bind", [("ipv6", "IPV6_ONLY", "::"), ("ipv4", "IPV4_ONLY", "0.0.0.0")])
def test_native_family_tls_pool_and_fractional_deadline(monkeypatch, family, strategy, bind):
    wreq = pytest.importorskip("wreq")
    import wreq.blocking
    constructor = Mock()
    monkeypatch.setattr(wreq.blocking, "Client", constructor)
    dns = Mock()
    monkeypatch.setattr(wreq, "DnsOptions", dns)
    native = NativeClient(family)
    options = constructor.call_args.kwargs
    assert options["no_proxy"] is True
    assert options["local_address"] == bind
    assert options["tls_verify"] is True and options["tls_verify_hostname"] is True
    assert options["pool_idle_timeout"] == timedelta(seconds=90)
    dns.assert_called_once_with(lookup_ip_strategy=getattr(wreq.LookupIpStrategy, strategy))
    constructor.return_value.post.return_value.status.as_int.return_value = 200
    native.post(URL, content=b"unchanged", headers={}, timeout=.1234567)
    assert constructor.return_value.post.call_args.kwargs["timeout"] == timedelta(microseconds=123456)
    assert constructor.return_value.post.call_args.kwargs["body"] == b"unchanged"
    assert "content" not in constructor.return_value.post.call_args.kwargs


def test_family_client_cache_and_explicit_direct_ignore_global_proxy(monkeypatch):
    import swoop._native
    factory = Mock()
    monkeypatch.setattr(swoop._native, "NativeClient", factory)
    monkeypatch.setattr(rpc, "_default_proxy", "socks5h://wrong")
    tc = config()
    first = rpc._get_transport_client(tc)
    assert first is rpc._get_transport_client(tc)
    factory.assert_called_once_with("ipv6", "chrome", None)


def test_retry_after_date_and_invalid():
    assert rpc._retry_after({"Retry-After": "120"}) == 120
    assert rpc._retry_after({"retry-after": "nan"}) is None
    assert rpc._retry_after({"retry-after": "bad"}) is None
    assert rpc._retry_after({"retry-after": "Thu, 01 Jan 1970 00:00:00 GMT"}) == 0


def test_native_actual_ipv6_socket_body_headers_response_and_connection_reuse():
    pytest.importorskip("wreq")
    import socket
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6
        daemon_threads = True
    observed = []
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def do_POST(self):
            observed.append((self.client_address, self.rfile.read(int(self.headers['Content-Length'])), self.headers['X-POS']))
            self.send_response(200)
            self.send_header("Content-Length", str(len(EMPTY)))
            self.end_headers()
            self.wfile.write(EMPTY.encode())
        def log_message(self, *args):
            pass
    try:
        server = Server(('::1', 0), Handler)
    except OSError:
        pytest.skip("IPv6 loopback unavailable")
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        native = NativeClient("ipv6")
        for _ in range(3):
            response = native.post(f"http://[::1]:{server.server_port}/rpc", content=b"exact=bytes", headers={"X-POS":"US-USD"}, timeout=.5)
            assert response.status_code == 200 and response.text == EMPTY
        assert all(peer[0] == '::1' and body == b'exact=bytes' and pos == 'US-USD' for peer, body, pos in observed)
        assert len({peer[1] for peer, _, _ in observed}) == 1
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


def test_second_mia1_route_is_bounded_and_uses_original_bytes(monkeypatch):
    calls = client(monkeypatch, [(403, ''), (200, COMPACT), (200, EMPTY)])
    tc = replace(config(), fallback_proxies=('socks5h://trusted:1082', 'socks5h://alternate:1080'))
    assert send(tc).text == EMPTY
    assert [c.proxy for c in calls] == [None, 'socks5h://trusted:1082', 'socks5h://alternate:1080']
    assert [c.address_family for c in calls] == ['ipv6', 'auto', 'auto']
    assert len(adaptive.health()) == 3


def test_ambiguous_response_never_becomes_inventory_when_comparison_unavailable(monkeypatch):
    calls = client(monkeypatch, [(200, ''), (503, '')])
    with pytest.raises(SwoopHTTPError):
        send()
    assert len(calls) == 2
    assert adaptive.health()[0]['reason'] is None


def test_ambiguous_comparison_does_not_fan_out(monkeypatch):
    calls = client(monkeypatch, [(200, ''), (200, '')])
    tc=replace(config(), fallback_proxies=('socks5h://trusted:1082','socks5h://alternate:1080'))
    with pytest.raises(SwoopParseError):
        send(tc)
    assert len(calls) == 2


def test_http_redirect_is_unavailable_not_success(monkeypatch):
    calls = client(monkeypatch, [(302, ''), (200, EMPTY)])
    assert send().text == EMPTY
    assert len(calls) == 2
    assert adaptive.health()[0]['reason'] == 'http_redirect'


def test_original_deadline_caps_nested_rpc_deadline(monkeypatch):
    dispatch = Mock(return_value=rpc._HTTPResponse(200, EMPTY))
    monkeypatch.setattr(rpc, '_get_transport_client', lambda tc: None)
    monkeypatch.setattr(rpc, '_post_with_retry', dispatch)
    rpc._http_post(URL, b'exact', transport=TransportConfig(timeout=90, _operation_deadline=101), _deadline=110)
    assert dispatch.call_args.kwargs['_deadline'] == 101


def test_expired_native_body_read_budget_stops_without_retry():
    pytest.importorskip('wreq')
    import socket
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import time
    # Use the real clock for a native request that returns headers, then stalls.
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200)
            self.send_header('Content-Length', '100')
            self.end_headers()
            time.sleep(.25)
        def log_message(self, *args):
            pass
    server=ThreadingHTTPServer(('127.0.0.1', 0),Handler)
    server.daemon_threads=True
    worker=threading.Thread(target=server.serve_forever,daemon=True)
    worker.start()
    try:
        native=NativeClient('ipv4')
        started=time.perf_counter()
        with pytest.raises(Exception) as error:
            native.post(f'http://127.0.0.1:{server.server_port}/', content=b'exact', headers={}, timeout=.05).text
        assert 'timeout' in type(error.value).__name__.lower()
        assert time.perf_counter()-started < .2
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


@pytest.mark.parametrize("routing", [{"connecting_airports":["DFW"]}, {"exclude_connecting_airports":["ORD"]}])
def test_real_filtered_request_replays_exact_wire_and_pos(monkeypatch, routing):
    from swoop import Passengers
    attempts=[]
    class HTTP:
        def post(self,url,**kwargs):
            attempts.append((url,kwargs['content'],kwargs['headers'].copy()))
            return SimpleNamespace(status_code=403 if len(attempts)==1 else 200,text=EMPTY,headers={})
    monkeypatch.setattr(rpc,'_get_transport_client',lambda tc: HTTP())
    tc=replace(config(),country='PA',currency='EUR')
    rpc._search_from_legs([{'origin':'JFK','destination':'LAX','date':'2026-11-30',
        'airlines':['AA'],'max_stops':1,'max_duration':1000,**routing,
        'earliest_departure':8,'latest_departure':18}],
        transport=tc,passengers=Passengers(adults=2,children=1),cabin='business',
        carry_on_bags=1,checked_bags=2,exclude_separate_tickets=True)
    assert len(attempts)==2 and attempts[0]==attempts[1]
    assert attempts[0][0].endswith('?gl=PA')
    assert 'EUR' in attempts[0][2]['x-goog-ext-259736195-jspb']


def test_unexpired_retry_after_state_is_not_evicted():
    for i in range(adaptive._MAX_ROUTES):
        lease=adaptive._acquire(('ipv6',str(i),'shopping'))
        adaptive._finish(lease,'http_429',3600)
    assert adaptive._acquire(('ipv6','new','shopping')) is None
    assert len(adaptive._routes)==adaptive._MAX_ROUTES
