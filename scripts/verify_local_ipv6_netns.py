"""Joint SDK/address-bank integration; all HTTP and mutations stay in a netns.

Usage: .venv/bin/python scripts/verify_local_ipv6_netns.py /path/to/infra-checkout
Requires Linux ip, unshare, setpriv, and the SDK native extra. The Infra runner
supplies its real daemon and namespace guard; no provider endpoints are used.
"""
from dataclasses import replace
import http.server
import importlib.util
import json
import os
from pathlib import Path
import select
import socket
import socketserver
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

SDK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK))


def load_runner(root):
    path = Path(root).resolve() / 'nixos/tests/test_swoop_ipv6_netns.py'
    spec = importlib.util.spec_from_file_location('swoop_bank_netns', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run(root):
    runner = load_runner(root)
    runner.namespace_guard()
    import swoop
    from swoop import _adaptive, _deals, _explore, _local_ipv6, _selection, _session, rpc
    from tests.factories import decode_f_req, encode_rpc_outer, make_full_response
    from tests.test_native_continuation_policy import itinerary
    measurements = []

    class Joint(runner.NamespaceIntegration):
        def setUp(self):
            super().setUp()
            rpc._clients.clear()
            _adaptive._routes.clear()
            self.calls = []
            self.blocked = False
            self.body_started, self.body_finish = threading.Event(), threading.Event()
            case = self
            class Server(http.server.ThreadingHTTPServer):
                address_family = socket.AF_INET6
                daemon_threads = True
            class Handler(http.server.BaseHTTPRequestHandler):
                protocol_version = 'HTTP/1.1'
                def log_message(self, *args):
                    pass
                def do_GET(self):
                    case.calls.append(('GET', self.client_address, self.path, self.headers.get('Cookie'), None))
                    body = b'"cfb2h":"BL","FdrFJe":"SID"'
                    self.send_response(200)
                    self.send_header('Set-Cookie', 'source=' + self.client_address[0] + '; Path=/')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    if self.path == '/delayed':
                        self.wfile.flush()
                        case.body_started.set()
                        case.body_finish.wait(3)
                    self.wfile.write(body)
                def do_POST(self):
                    body = self.rfile.read(int(self.headers['Content-Length']))
                    case.calls.append(('POST', self.client_address, self.path, self.headers.get('Cookie'), body))
                    rejected = case.blocked and self.client_address[0] != '::1'
                    payload = b'' if rejected else case.reply(self.path, body).encode()
                    self.send_response(403 if rejected else 200)
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
            self.http = Server(('::1', 0), Handler)
            self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
            self.http_thread.start()
            self.base = f'http://[::1]:{self.http.server_port}'
            self.patches = [patch.object(_deals, 'DEALS_PAGE_URL', self.base + '/travel/flights/deals'),
                            patch.object(_deals, 'DEALS_RPC_URL', self.base + '/GetFlightDealsStreaming'),
                            patch.object(_explore, 'EXPLORE_PAGE_URL', self.base + '/travel/explore'),
                            patch.object(_explore, 'EXPLORE_RPC_URL', self.base + '/GetExploreDestinations'),
                            patch.object(rpc, 'SHOPPING_RPC_URL', self.base + '/GetShoppingResults')]
            for item in self.patches:
                item.start()
            self.reply = lambda *_: json.dumps([['wrb.fr', None, '[]']])

        def tearDown(self):
            self.body_finish.set()
            self.http.shutdown()
            self.http.server_close()
            self.http_thread.join(2)
            for item in self.patches:
                item.stop()
            rpc._clients.clear()
            _adaptive._routes.clear()
            super().tearDown()

        def config(self):
            return swoop.TransportConfig(address_family='ipv6', local_ipv6_socket=self.socket,
                local_ipv6_prefix=str(self.prefix), country='PA', currency='EUR', timeout=5, request_timeout=2)

        def test_sdk_fresh_session_pooling_cookies_and_retirement(self):
            seen = set()
            for name in ['deals', 'explore'] * 3:
                self.wait(lambda: self.status()['counts'].get('ready') == 2)
                start = len(self.calls)
                began = time.perf_counter()
                getattr(swoop, name)('JFK', transport=self.config())
                measurements.append(dict(endpoint=name, elapsed_ms=round((time.perf_counter() - began) * 1000, 3)))
                calls = self.calls[start:]
                self.assertEqual([c[0] for c in calls], ['GET', 'POST'])
                source = calls[0][1][0]
                self.assertNotIn(source, seen)
                seen.add(source)
                self.assertEqual(calls[0][1], calls[1][1])  # same IPv6 and pooled TCP connection
                self.assertIsNone(calls[0][3])  # a fresh source has a fresh cookie jar
                self.assertEqual(calls[1][3], 'source=' + source)
                self.assertIn('gl=PA', calls[1][2])
                self.wait(lambda: source not in self.addresses())
                self.assertLessEqual(sum(self.status()['counts'].values()), 2)
            self.assertFalse(rpc._clients)

        def test_sdk_c47_next_and_price_preserve_selectors_on_actual_native_wire(self):
            outbound = itinerary('JFK', 'MIA', 'PTY', '2026-11-16', '117')
            inbound = itinerary('PTY', 'ORD', 'JFK', '2026-11-23', '119')
            out_text = encode_rpc_outer(make_full_response(other_itins=[outbound]))
            in_text = encode_rpc_outer(make_full_response(other_itins=[inbound]))
            decoded = rpc._parse_rpc_response(out_text)
            assert decoded is not None
            legs = [rpc._normalize_rpc_leg('JFK', 'PTY', '2026-11-16'),
                    rpc._normalize_rpc_leg('PTY', 'JFK', '2026-11-23', airlines=['AA', 'UA'], exclude_airlines=['B6'], max_stops=1)]
            selector = _selection.encode_trip_selector(request_legs=legs, itineraries=[decoded.other[0]],
                cabin='economy', include_basic_economy=True, exclude_separate_tickets=True,
                transport=self.config())
            segments = []
            def reply(path, body):
                request = decode_f_req(parse_qs(body.decode())['f.req'][0])[1][13]
                segments.append(request)
                return in_text if request[0][8] else out_text
            self.reply = reply
            result = swoop.search_next_leg(selector, next_leg_filters={'airlines': ['AA'], 'exclude_airlines': ['DL'],
                'exclude_connecting_airports': ['MIA']}, transport=self.config())
            self.assertEqual(len(result.results), 1)
            retained = _selection.decode_trip_selector(result.results[0].selector)
            self.assertEqual(retained['query_legs'][1]['airlines'], ['AA'])
            self.assertEqual(retained['query_legs'][1]['exclude_airlines'], ['B6', 'DL'])
            self.assertEqual(retained['selected_legs'][0], _selection.decode_trip_selector(selector)['selected_legs'][0])
            first = {c[1][0] for c in self.calls}
            self.assertEqual(len(first), 1)
            self.wait(lambda: next(iter(first)) not in self.addresses())
            self.wait(lambda: self.status()['counts'].get('ready') == 2)
            start = len(self.calls)
            priced = swoop.price_selector(result.results[0].selector, transport=self.config())
            assert priced is not None
            self.assertEqual(priced.price, 350)
            second = {c[1][0] for c in self.calls[start:]}
            self.assertEqual(len(second), 1)
            self.assertFalse(first & second)
            self.assertTrue(all(s[1][10] == ['MIA'] for s in segments[2:]))
            self.assertTrue(all(s[1][5] == ['B6', 'DL'] for s in segments[1:]))
            self.wait(lambda: next(iter(second)) not in self.addresses())

        def test_actual_body_drain_keeps_address_after_caller_finishes(self):
            errors = []
            def read(response):
                try:
                    response.text
                except Exception as error:
                    errors.append(error)
                finally:
                    response.close()
            with _local_ipv6.operation(self.config()) as chosen:
                source = chosen.local_address
                response = _session.BudgetedClient(rpc._get_transport_client(chosen), chosen).get(self.base + '/delayed', headers={})
                thread = threading.Thread(target=read, args=(response,))
                thread.start()
                self.assertTrue(self.body_started.wait(1))
            self.assertIn(source, self.addresses())
            self.assertEqual(self.status()['counts'].get('leased'), 1)
            self.body_finish.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertTrue(errors)  # no completed result after the operation ended
            self.wait(lambda: source not in self.addresses())

        def test_real_source_failures_socks_rebootstrap_prefix_gate_and_recovery(self):
            # Real SOCKS transport remaps only our local fixture destination.
            case = self
            class Socks(socketserver.BaseRequestHandler):
                def handle(self):
                    target = None
                    def read(size):
                        data = b''
                        while len(data) < size:
                            part = self.request.recv(size - len(data))
                            if not part:
                                raise EOFError
                            data += part
                        return data
                    try:
                        version, count = read(2)
                        assert version == 5
                        read(count)
                        self.request.sendall(b'\x05\x00')
                        version, command, _, family = read(4)
                        assert version == 5 and command == 1
                        read(16 if family == 4 else read(1)[0] if family == 3 else 4)
                        read(2)
                        target = socket.create_connection(('::1', case.http.server_port), timeout=2)
                        self.request.sendall(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00')
                        while True:
                            ready, _, _ = select.select([self.request, target], [], [], 2)
                            if not ready:
                                return
                            for source in ready:
                                data = source.recv(65536)
                                if not data:
                                    return
                                (target if source is self.request else self.request).sendall(data)
                    except (EOFError, OSError):
                        pass
                    finally:
                        if target:
                            target.close()
            class Proxy(socketserver.ThreadingTCPServer):
                daemon_threads = True
            proxy = Proxy(('127.0.0.1', 0), Socks)
            thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            thread.start()
            tc = replace(self.config(), fallback_proxies=(f'socks5h://127.0.0.1:{proxy.server_address[1]}',))
            self.blocked = True
            try:
                for _ in range(3):
                    self.wait(lambda: self.status()['counts'].get('ready') == 2)
                    start = len(self.calls)
                    self.assertEqual(swoop.deals('JFK', transport=tc).deals, [])
                    calls = self.calls[start:]
                    self.assertEqual([c[0] for c in calls], ['GET', 'POST', 'GET', 'POST'])
                    source = calls[0][1][0]
                    self.assertNotEqual(source, '::1')
                    self.assertEqual(calls[1][3], 'source=' + source)
                    self.assertEqual(calls[3][3], 'source=::1')
                    self.assertEqual(calls[1][4], calls[3][4])
                    self.wait(lambda: source not in self.addresses())
                prefix = next(route for key, route in _adaptive._routes.items() if key[0].startswith('ipv6-prefix:'))
                self.assertEqual(prefix.reason, 'http_403')
                start = len(self.calls)
                self.assertEqual(swoop.deals('JFK', transport=tc).deals, [])
                self.assertTrue(all(c[1][0] == '::1' for c in self.calls[start:]))
                self.blocked = False
                prefix.until = 0  # controlled expiry, no 30s provider probe/sleep
                start = len(self.calls)
                self.assertEqual(swoop.deals('JFK', transport=tc).deals, [])
                self.assertTrue(all(c[1][0] != '::1' for c in self.calls[start:]))
                self.assertIsNone(prefix.reason)  # valid empty is a healthy recovery
            finally:
                proxy.shutdown()
                proxy.server_close()
                thread.join(2)

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Joint)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if '--report' in sys.argv:
        path = Path(sys.argv[sys.argv.index('--report') + 1])
        def head(directory):
            return subprocess.check_output(['git', '-C', str(directory), 'rev-parse', 'HEAD'], text=True).strip()
        path.write_text(json.dumps(dict(scope='disposable user+network namespace; local HTTP only',
            provider_calls=0, sdk_commit=head(SDK), infra_commit=head(root),
            tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
            fresh_session_samples=measurements), indent=2) + '\n')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    infra = sys.argv[1]
    if '--inside' not in sys.argv:
        env = dict(os.environ, SWOOP_TEST_PARENT_NETNS=os.readlink('/proc/self/ns/net'))
        raise SystemExit(subprocess.call(['unshare', '--user', '--map-root-user', '--net',
            sys.executable, str(Path(__file__).resolve()), infra, '--inside', *sys.argv[2:]], env=env))
    raise SystemExit(run(infra))
