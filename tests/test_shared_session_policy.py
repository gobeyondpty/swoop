"""Real Deals/Explore/Hotels producers share route health, budgets and jars."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import inspect
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

import swoop
from swoop import _adaptive as policy, _explore, _hotels, _native, rpc
from swoop._session import SessionPool, RouteBusy, get_page, supports_policy
from swoop.exceptions import SwoopHTTPError, SwoopParseError, SwoopTransportError, SwoopUpstreamError
from tests.test_hotels import _batchexecute, _broad_universal_payload, _universal_payload, _price

FIX = Path(__file__).parent / 'fixtures' / 'responses'
COMPACT = json.dumps([["wrb.fr", None, None, None, None, [13]]])
EMPTY = json.dumps([["wrb.fr", None, "[]"]])
MIA = 'socks5h://mia-fixture:1082'


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    policy._routes.clear()
    rpc._clients.clear()
    _explore._browser_params_cache.clear()
    _explore._browser_params_key_locks.clear()
    monkeypatch.setattr(policy.random, 'uniform', lambda *_: 1.0)
    yield
    policy._routes.clear()
    rpc._clients.clear()
    _explore._browser_params_cache.clear()
    _explore._browser_params_key_locks.clear()


def config(**kwargs):
    return swoop.TransportConfig(address_family='ipv6', fallback_proxies=(MIA,),
        timeout=20, request_timeout=8, country='PA', currency='EUR', **kwargs)


class Edge:
    """Only the HTTP edge is replaced; SDK sessions/builders/parsers are real."""
    def __init__(self, monkeypatch, responder):
        self.clients, self.calls = [], []
        edge = self
        class HTTP:
            def __init__(self, family='auto', profile='chrome', proxy=None, **kwargs):
                self.route = proxy or family
                self.jar = len(edge.clients)
                self.cookie = None
                edge.clients.append(self)
            def get(self, url, **kwargs):
                edge.calls.append((self, 'GET', url, kwargs))
                self.cookie = 'cookie-' + str(self.jar)
                value = responder(self, 'GET', url, kwargs)
                if value is None:
                    value = SimpleNamespace(status_code=200, headers={},
                        text=f'"cfb2h":"BL{self.jar}","FdrFJe":"SID{self.jar}"')
                return value
            def post(self, url, **kwargs):
                assert self.cookie == 'cookie-' + str(self.jar), 'RPC dispatched without its route session'
                edge.calls.append((self, 'POST', url, kwargs))
                return responder(self, 'POST', url, kwargs)
        monkeypatch.setattr(_native, 'NativeClient', HTTP)
        monkeypatch.setattr('primp.Client', HTTP)


def response(text=EMPTY, status=200, headers=None):
    return SimpleNamespace(status_code=status, text=text, headers=headers or {})


@pytest.mark.parametrize('failure,reason', [
    (lambda: response(status=403), 'http_403'),
    (lambda: response(status=429, headers={'Retry-After':'120'}), 'http_429'),
    (lambda: response(COMPACT), 'compact_internal'),
    (lambda: response('<html>captcha</html>'), 'malformed'),
    (lambda: TimeoutError('synthetic read deadline'), 'timeout'),
])
def test_deals_recreates_route_session_and_preserves_exact_query(monkeypatch, failure, reason):
    def respond(client, method, url, kw):
        if method == 'GET':
            return None
        if client.route == 'ipv6':
            value = failure()
            if isinstance(value, Exception):
                raise value
            return value
        return response((FIX / 'deals_streaming.txt').read_text())
    edge = Edge(monkeypatch, respond)
    result = swoop.deals('JFK', cabin='business', max_stops=0, airlines=['AA'],
        passengers=swoop.Passengers(adults=2, children=1), include_basic_economy=True,
        exclude_separate_tickets=True, transport=config())
    assert result.deals and result.currency == 'EUR'
    posts = [c for c in edge.calls if c[1] == 'POST']
    assert [c[0].route for c in posts] == ['ipv6', MIA]
    assert posts[0][2] == posts[1][2]
    assert posts[0][3]['content'] == posts[1][3]['content']
    assert posts[0][3]['headers'] == posts[1][3]['headers']
    assert 'gl=PA' in posts[0][2] and 'EUR' in posts[0][3]['headers']['x-goog-ext-259736195-jspb']
    assert [c[1] for c in edge.calls] == ['GET', 'POST', 'GET', 'POST']
    assert posts[0][0] is not posts[1][0]
    assert policy.health()[0]['reason'] == reason
    assert policy.health()[1]['state'] == 'closed'


@pytest.mark.parametrize('operation', ['deals', 'explore', 'hotels', 'hotel_prices', 'hotel_reviews'])
def test_valid_protocol_empty_is_healthy_for_every_session_producer(monkeypatch, operation):
    def respond(client, method, url, kw):
        if method == 'GET':
            return None
        if '/batchexecute' in url:
            return response(_batchexecute(parse_qs(urlsplit(url).query)['rpcids'][0], []))
        return response(EMPTY)
    edge = Edge(monkeypatch, respond)
    if operation in ('deals', 'explore'):
        result = getattr(swoop, operation)('JFK', transport=config())
        assert not getattr(result, 'deals' if operation == 'deals' else 'destinations')
    elif operation == 'hotel_reviews':
        assert not swoop.hotel_reviews('token', transport=config()).reviews
    else:
        kwargs = dict(check_in='2026-11-30', check_out='2026-12-03', transport=config())
        result = getattr(swoop, operation)('token' if operation == 'hotel_prices' else 'New York', **kwargs)
        assert not getattr(result, 'providers' if operation == 'hotel_prices' else 'hotels')
    assert len(edge.clients) == 1
    assert policy.health()[0]['reason'] is None


def test_shared_cooldown_crosses_endpoints_and_single_probe_recovers(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(policy.time, 'monotonic', lambda: clock[0])
    fail = [True]
    def respond(client, method, url, kw):
        if method == 'GET':
            return None
        return response(COMPACT if client.route == 'ipv6' and fail[0] else EMPTY)
    edge = Edge(monkeypatch, respond)
    swoop.deals('JFK', transport=config())
    swoop.explore('JFK', transport=config())
    posts = lambda: [c[0].route for c in edge.calls if c[1] == 'POST']
    assert posts() == ['ipv6', MIA, MIA]
    assert len(policy.health()) == 2  # shared Google route, not one breaker per RPC
    clock[0] = 131
    swoop.explore('JFK', transport=config())
    assert policy.health()[0]['retry_in'] == 60  # failed recovery backs off
    fail[0] = False
    clock[0] = 192
    swoop.deals('JFK', transport=config())
    assert posts()[-1] == 'ipv6'
    assert policy.health()[0]['state'] == 'closed'


def test_explore_params_match_cookie_jar_on_failover_and_warm_reuse(monkeypatch):
    fail = [True]
    def respond(client, method, url, kw):
        if method == 'GET':
            return None
        query = parse_qs(urlsplit(url).query)
        assert query['bl'] == [f'BL{client.jar}']
        assert query['f.sid'] == [f'SID{client.jar}']
        return response(COMPACT if client.route == 'ipv6' and fail[0] else (FIX / 'explore/jfk_response.txt').read_text())
    edge = Edge(monkeypatch, respond)
    result = swoop.explore('JFK', one_way=True, cabin='business', max_stops=0,
        passengers=swoop.Passengers(adults=2), transport=config())
    assert result.destinations and all(d.return_date is None for d in result.destinations)
    posts = [c for c in edge.calls if c[1] == 'POST']
    assert posts[0][3]['content'] == posts[1][3]['content']
    assert posts[0][0].cookie != posts[1][0].cookie
    policy._routes.clear()
    fail[0] = False
    swoop.explore('JFK', transport=config())
    count = len([c for c in edge.calls if c[1] == 'GET'])
    swoop.explore('JFK', transport=config())
    assert len([c for c in edge.calls if c[1] == 'GET']) == count
    # Evict the actual jar, leaving cached tokens: a new jar must bootstrap.
    rpc._clients.clear()
    swoop.explore('JFK', transport=config())
    assert len([c for c in edge.calls if c[1] == 'GET']) == count + 1


def test_concurrent_explore_sessions_do_not_overwrite_cookies(monkeypatch):
    barrier = threading.Barrier(2)
    def respond(client, method, url, kw):
        if method == 'GET':
            barrier.wait(timeout=2)
            return None
        assert parse_qs(urlsplit(url).query)['f.sid'] == [f'SID{client.jar}']
        return response(EMPTY)
    edge = Edge(monkeypatch, respond)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda origin: swoop.explore(origin, transport=config()), ['JFK','SFO']))
    assert [r.origin for r in results] == ['JFK','SFO']
    assert len(edge.clients) == 2 and {c.route for c in edge.clients} == {'ipv6'}
    assert len({c.jar for c in edge.clients}) == 2


@pytest.mark.parametrize('operation', ['hotels', 'hotel_prices', 'hotel_reviews'])
def test_hotels_rebuild_bootstrap_and_context_on_failover(monkeypatch, operation):
    def respond(client, method, url, kw):
        if method == 'GET':
            return None
        query = parse_qs(urlsplit(url).query)
        assert query['f.sid'] == [f'SID{client.jar}']
        rpc_id = query['rpcids'][0]
        if client.route == 'ipv6':
            return response(COMPACT)
        if rpc_id == _hotels.UNIVERSAL_SEARCH_RPC:
            return response(_batchexecute(rpc_id, _universal_payload()))
        if rpc_id == _hotels.HOTEL_RESULTS_RPC:
            return response(_batchexecute(rpc_id, []))
        return response(_batchexecute(rpc_id, []))
    edge = Edge(monkeypatch, respond)
    kwargs = dict(transport=config())
    if operation != 'hotel_reviews':
        kwargs.update(check_in='2026-11-30', check_out='2026-12-03', adults=2, rooms=2,
                      child_ages=[7], currency='EUR')
    result = getattr(swoop, operation)('token' if operation != 'hotels' else 'New York', **kwargs)
    assert result is not None
    posts = [c for c in edge.calls if c[1] == 'POST']
    assert [c[0].route for c in posts[:2]] == ['ipv6', MIA]
    assert posts[0][3]['content'] == posts[1][3]['content']
    gets = [c for c in edge.calls if c[1] == 'GET']
    assert [c[0].route for c in gets] == ['ipv6', MIA]
    assert gets[0][2:] == gets[1][2:] or gets[0][2] == gets[1][2]  # timeouts may shrink
    assert policy.health()[0]['reason'] == 'compact_internal'


def test_primary_bootstrap_and_rpc_share_short_proof_budget(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(policy.time, 'monotonic', lambda: clock[0])
    timeouts = []
    def respond(client, method, url, kw):
        timeouts.append((client.route, method, kw['timeout']))
        if method == 'GET':
            if client.route == 'ipv6':
                clock[0] += 3.25
            return None
        if client.route == 'ipv6':
            clock[0] += 1.8
        return response(EMPTY)
    Edge(monkeypatch, respond)
    swoop.deals('JFK', transport=config())
    assert timeouts[:2] == [('ipv6','GET',5), ('ipv6','POST',1.75)]
    assert timeouts[2][0] == MIA and timeouts[2][2] <= 8
    assert policy.health()[0]['reason'] == 'timeout'


def test_cancel_after_bootstrap_never_dispatches_rpc_or_fallback(monkeypatch):
    cancelled = [False]
    def cancel():
        if cancelled[0]:
            raise RuntimeError('caller cancelled')
    def respond(client, method, url, kw):
        cancelled[0] = True
    edge = Edge(monkeypatch, respond)
    with pytest.raises(RuntimeError, match='caller cancelled'):
        swoop.deals('JFK', transport=config(_cancel_check=cancel))
    assert len(edge.calls) == 1
    assert all(h['active'] == 0 and h['reason'] is None for h in policy.health())


def test_public_capabilities_preserve_signatures_and_unknown_is_not_admitted():
    for name in ('search','search_raw','deals','explore','hotels','hotel_prices','hotel_reviews',
                 'price_selector','calendar_prices','get_calendar','get_price_graph'):
        function = getattr(swoop, name)
        assert supports_policy(function)
        assert inspect.signature(function) == inspect.signature(function.__wrapped__)
    assert not supports_policy(lambda: None)


def test_session_capacity_skips_without_poisoning_health():
    pool = SessionPool(lambda: object())
    with pool.borrow(config()), pool.borrow(config()):
        with pytest.raises(RouteBusy):
            with pool.borrow(config()):
                pass
    with pool.borrow(config()):
        pass


def test_explore_cache_and_lock_registry_remain_bounded(monkeypatch):
    Edge(monkeypatch, lambda c,m,u,k: None if m == 'GET' else response(EMPTY))
    for _ in range(80):
        rpc._clients.clear()
        swoop.explore('JFK', transport=config())
    assert len(_explore._browser_params_cache) == 32
    assert len(_explore._browser_params_key_locks) == 64


def test_hotel_followup_preserves_currency_and_children_over_route_context():
    context = _hotels._extract_context_from_universal(_universal_payload())
    for builder, extra in ((_hotels._build_hotels_results_payload, {}),
                           (_hotels._build_hotel_detail_payload, {'hotel_token':'original-token'})):
        payload = builder(context, check_in='2026-11-30', check_out='2026-12-03',
            adults=2, child_ages=[7,11], rooms=2, currency='EUR', **extra)
        assert payload[1][3] == 'EUR'  # context says USD; caller intent wins
        assert payload[1][4] == [[2026,11,30],[2026,12,3],2,2]
        assert payload[1][13] == [3,[[7,11]],2]
        if extra:
            assert payload[3] == 'original-token'


def test_hotel_late_refusal_restarts_original_session_before_more_dispatch(monkeypatch):
    def respond(client, method, url, kw):
        if method == 'GET':
            return None
        rpc_id = parse_qs(urlsplit(url).query)['rpcids'][0]
        if rpc_id == _hotels.UNIVERSAL_SEARCH_RPC:
            return response(_batchexecute(rpc_id, _broad_universal_payload()))
        return response(COMPACT if client.route == 'ipv6' else _batchexecute(rpc_id, []))
    edge = Edge(monkeypatch, respond)
    result = swoop.hotels('New York', check_in='2026-11-30', check_out='2026-12-03', transport=config())
    assert result.hotels
    assert [c[0].route for c in edge.calls] == ['ipv6'] * 3 + [MIA] * 3
    posts = [c for c in edge.calls if c[1] == 'POST']
    assert posts[0][3]['content'] == posts[2][3]['content']
    assert posts[1][3]['content'] == posts[3][3]['content']


def test_malformed_deals_without_frames_is_not_healthy_empty(monkeypatch):
    edge = Edge(monkeypatch, lambda c,m,u,k: None if m == 'GET' else response('garbage' if c.route == 'ipv6' else EMPTY))
    assert swoop.deals('JFK', transport=config()).deals == []
    assert len(edge.clients) == 2 and policy.health()[0]['reason'] == 'malformed'


def test_cache_lock_wait_honors_cancel_without_waiting_for_other_request():
    import time
    from swoop._session import session_lock
    lock = threading.Lock()
    lock.acquire()
    cancelled = threading.Event()
    timer = threading.Timer(.02, cancelled.set)
    def cancel():
        if cancelled.is_set():
            raise RuntimeError('cancel waiting for cache lock')
    started = time.perf_counter()
    timer.start()
    try:
        with pytest.raises(RuntimeError, match='cancel waiting'):
            with session_lock(lock, config(_cancel_check=cancel)):
                pytest.fail('lock must remain owned by other operation')
        assert time.perf_counter() - started < .5
    finally:
        lock.release()
        timer.join()
