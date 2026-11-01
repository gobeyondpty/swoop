"""Source-lease protocol, operation lifetime and per-source native routing."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import socketserver
import threading
import time
from types import SimpleNamespace

import pytest
import swoop
from swoop import _adaptive, _local_ipv6, _native, _session, rpc
from swoop.exceptions import SwoopTransportError
from tests.factories import encode_rpc_outer, make_full_response
from tests.test_native_continuation_policy import itinerary

PREFIX='fd42:5357:1::/80'
MIA='socks5h://mia-fixture:1082'
EMPTY=json.dumps([['wrb.fr',None,'null']])


class Broker:
    def __init__(self,tmp_path,reply=None,events=None):
        self.path=str(tmp_path/'pool.sock')
        self.requests=[]
        self.releases=[]
        self.events=events if events is not None else []
        lock=threading.Lock()
        broker=self
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.request.settimeout(3)
                try:
                    request=json.loads(self.rfile.readline(4097))
                    with lock:
                        index=len(broker.requests)
                        broker.requests.append(request)
                    message=dict(v=1,ok=True,address=f'fd42:5357:1::{index+10:x}',lease=f'lease-{index}',mode=request['mode'])
                    if reply is not None:
                        message=reply(message) if callable(reply) else reply
                    data=message if isinstance(message,bytes) else json.dumps(message).encode()+b'\n'
                    self.wfile.write(data)
                    self.wfile.flush()
                    released=self.rfile.readline(4097)
                    with lock:
                        broker.releases.append(json.loads(released) if released else None)
                        broker.events.append('broker-release')
                except (OSError,ValueError):
                    pass
        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads=True
        self.server=Server(self.path,Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
    def config(self,**kwargs):
        return swoop.TransportConfig(address_family='ipv6',local_ipv6_socket=self.path,local_ipv6_prefix=PREFIX,
            fallback_proxies=(MIA,),timeout=10,request_timeout=2,country='PA',currency='EUR',**kwargs)
    def wait_releases(self,count):
        until=time.monotonic()+2
        while len(self.releases)<count and time.monotonic()<until:
            threading.Event().wait(.005)
        assert len(self.releases)==count


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    _adaptive._routes.clear()
    rpc._clients.clear()
    monkeypatch.setattr(_local_ipv6,'_BROKER_UID',os.getuid())
    monkeypatch.setattr(_adaptive.random,'uniform',lambda *_:1.0)
    yield
    _adaptive._routes.clear()
    rpc._clients.clear()


@pytest.fixture
def broker(tmp_path):
    server=Broker(tmp_path)
    yield server
    server.close()


class Edge:
    def __init__(self,monkeypatch,responder=None,events=None):
        self.clients=[]
        self.calls=[]
        self.events=events if events is not None else []
        edge=self
        class HTTP:
            def __init__(self,family='auto',profile='chrome',proxy=None,local_address=None,**kwargs):
                self.source=local_address
                self.route=proxy or family
                self.closed=False
                edge.clients.append(self)
            def get(self,url,**kwargs):
                assert not self.closed
                edge.calls.append((self,'GET',url,kwargs))
                return SimpleNamespace(status_code=200,headers={},text='"cfb2h":"BL","FdrFJe":"SID"')
            def post(self,url,**kwargs):
                assert not self.closed
                edge.calls.append((self,'POST',url,kwargs))
                return responder(self,url,kwargs) if responder else SimpleNamespace(status_code=200,text=EMPTY,headers={})
            def close(self):
                self.closed=True
                edge.events.append('client-close')
        monkeypatch.setattr(_native,'NativeClient',HTTP)
        monkeypatch.setattr('primp.Client',HTTP)


def test_fresh_operation_uses_new_source_and_closes_before_broker_release(monkeypatch,broker):
    edge=Edge(monkeypatch,events=broker.events)
    for _ in range(2):
        assert swoop.search_raw('JFK','LAX','2026-11-30',transport=broker.config()) is None
    broker.wait_releases(2)
    assert [client.source for client in edge.clients]==['fd42:5357:1::a','fd42:5357:1::b']
    assert all(client.closed for client in edge.clients)
    assert broker.events[0]=='client-close'
    assert len(rpc._clients)==0  # fresh clients never enter the process-wide pool
    assert all(request['op']=='acquire' and 0<request['hold_seconds']<=10 for request in broker.requests)
    assert all(message['op']=='release' and message['reason'] is None for message in broker.releases)
    assert all('gl=PA' in c[2] and 'EUR' in c[3]['headers']['x-goog-ext-259736195-jspb'] for c in edge.calls)


def test_nested_public_operation_acquires_once_and_reuses_connections(monkeypatch,broker):
    edge=Edge(monkeypatch)
    with _local_ipv6.operation(broker.config()) as config:
        swoop.search_raw('JFK','LAX','2026-11-30',transport=config)
        swoop.search_raw('JFK','LAX','2026-11-30',transport=config)
        assert not edge.clients[0].closed and not broker.releases
    broker.wait_releases(1)
    assert len(broker.requests)==len(edge.clients)==1 and len(edge.calls)==2


def test_concurrent_searches_never_share_source_or_request_state(monkeypatch,broker):
    barrier=threading.Barrier(2)
    def reply(client,url,kwargs):
        barrier.wait(timeout=2)
        return SimpleNamespace(status_code=200,text=EMPTY,headers={})
    edge=Edge(monkeypatch,reply)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(swoop.search_raw,'JFK','LAX','2026-11-30',transport=broker.config()) for _ in range(2)]
        assert [f.result(timeout=3) for f in futures]==[None,None]
    broker.wait_releases(2)
    assert len({client.source for client in edge.clients})==2


def test_native_failure_replays_exact_wire_to_mia_without_local_binding(monkeypatch,broker):
    def reply(client,url,kwargs):
        return SimpleNamespace(status_code=403 if client.source else 200,text=EMPTY,headers={})
    edge=Edge(monkeypatch,reply)
    assert swoop.search_raw('JFK','LAX','2026-11-30',transport=broker.config()) is None
    broker.wait_releases(1)
    assert [c[0].source for c in edge.calls]==['fd42:5357:1::a',None]
    assert edge.calls[0][2:]==edge.calls[1][2:] or edge.calls[0][3]['content']==edge.calls[1][3]['content']
    assert edge.calls[0][2]==edge.calls[1][2]
    assert edge.calls[0][3]['headers']==edge.calls[1][3]['headers']
    assert broker.releases[0]['reason']=='http_403'
    assert next(h for h in _adaptive.health() if h.get('scope')=='address')['state']=='open'
    assert next(h for h in _adaptive.health() if h.get('scope')=='prefix')['state']=='closed'


def test_proven_hidden_inventory_releases_failed_source_and_correlates_prefix(monkeypatch,broker):
    inventory=encode_rpc_outer(make_full_response(other_itins=[itinerary('JFK','MIA','PTY','2026-11-16','117')]))
    def reply(client,url,kwargs):
        return SimpleNamespace(status_code=200,headers={},text='[]' if client.source else inventory)
    Edge(monkeypatch,reply)
    for _ in range(3):
        result=swoop.search_raw('JFK','PTY','2026-11-16',transport=broker.config())
        assert result and result.other
    broker.wait_releases(3)
    assert all(message['reason']=='hidden_inventory' for message in broker.releases)
    assert next(h for h in _adaptive.health() if h.get('scope')=='prefix')['reason']=='hidden_inventory'


@pytest.mark.parametrize('reply',[dict(v=1,ok=False,error='busy'),
    lambda x:{**x,'address':'fd43::1'},lambda x:{**x,'mode':'reuse'},
    lambda x:{**x,'lease':'bad\nlease'},lambda x:{**x,'v':True},b'['+b'x'*4096])
def test_invalid_or_busy_broker_never_sends_unbound_direct(monkeypatch,tmp_path,reply):
    broker=Broker(tmp_path,reply)
    try:
        edge=Edge(monkeypatch)
        assert swoop.search_raw('JFK','LAX','2026-11-30',transport=broker.config()) is None
        assert [client.route for client in edge.clients]==[MIA]
        assert all(h['reason'] is None for h in _adaptive.health())
    finally:
        broker.close()


def test_wrong_peer_owner_and_missing_broker_fail_locally(monkeypatch,broker,tmp_path):
    edge=Edge(monkeypatch)
    monkeypatch.setattr(_local_ipv6,'_BROKER_UID',os.getuid()+1)
    swoop.search_raw('JFK','LAX','2026-11-30',transport=broker.config())
    assert [c.route for c in edge.clients]==[MIA]
    config=replace(broker.config(),local_ipv6_socket=str(tmp_path/'missing.sock'),fallback_proxies=())
    with pytest.raises(_local_ipv6.LocalSourceUnavailable):
        swoop.search_raw('JFK','LAX','2026-11-30',transport=config)


def test_cancellation_before_acquisition_or_fallback_sends_nothing(monkeypatch,broker):
    edge=Edge(monkeypatch)
    def cancelled():
        raise RuntimeError('cancelled')
    with pytest.raises(RuntimeError,match='cancelled'):
        swoop.search_raw('JFK','LAX','2026-11-30',transport=broker.config(_cancel_check=cancelled))
    assert not broker.requests and not edge.clients


def test_lease_stays_until_deferred_native_body_finishes(monkeypatch,broker):
    reading=threading.Event()
    finish=threading.Event()
    events=broker.events
    class Response:
        status_code=200
        headers={}
        @property
        def text(self):
            reading.set()
            assert finish.wait(2)
            return 'complete'
    raw=SimpleNamespace(post=lambda *a,**kw:Response(),close=lambda:events.append('client-close'))
    with ThreadPoolExecutor(max_workers=1) as workers:
        with _local_ipv6.operation(broker.config()) as selected:
            selected._source_lease.keep(raw)
            response=_session.BudgetedClient(raw,selected).post('http://unused',timeout=1)
            future=workers.submit(lambda:response.text)
            assert reading.wait(1)
        assert not broker.releases and not events
        finish.set()
        with pytest.raises(_local_ipv6.LocalSourceUnavailable):
            future.result(timeout=2)
    broker.wait_releases(1)
    assert events==['client-close','broker-release']


def test_worker_activity_retains_address_after_outer_operation_returns(broker):
    started=threading.Event()
    finish=threading.Event()
    with ThreadPoolExecutor(max_workers=1) as workers:
        with _local_ipv6.operation(broker.config()) as selected:
            def worker():
                with _local_ipv6.operation(selected):
                    started.set()
                    assert finish.wait(2)
            future=workers.submit(worker)
            assert started.wait(1)
        assert not broker.releases
        with pytest.raises(_local_ipv6.LocalSourceUnavailable):
            selected._source_lease.check()
        finish.set()
        future.result(timeout=2)
    broker.wait_releases(1)


def test_correlated_sources_open_prefix_without_hammering_new_addresses(monkeypatch,broker):
    def reply(client,url,kwargs):
        return SimpleNamespace(status_code=403 if client.source else 200,text=EMPTY,headers={})
    edge=Edge(monkeypatch,reply)
    for _ in range(4):
        swoop.search_raw('JFK','LAX','2026-11-30',transport=broker.config())
    broker.wait_releases(3)
    assert len(broker.requests)==3
    assert sum(c[0].source is not None for c in edge.calls)==3
    assert next(h for h in _adaptive.health() if h.get('scope')=='prefix')['state']=='open'
    assert all('fd42' not in str(item) for item in _adaptive.health())


def test_valid_empty_does_not_open_source_or_prefix(monkeypatch,broker):
    Edge(monkeypatch)
    swoop.search_raw('JFK','LAX','2026-11-30',transport=broker.config())
    broker.wait_releases(1)
    assert all(h['state']=='closed' for h in _adaptive.health())


def test_prefix_retry_after_and_single_recovery_probe(monkeypatch,broker):
    now=[time.monotonic()]
    clock=time.monotonic
    failed=[True]
    def reply(client,url,kwargs):
        return SimpleNamespace(status_code=429 if client.source and failed[0] else 200,text=EMPTY,headers={'Retry-After':'120'})
    edge=Edge(monkeypatch,reply)
    swoop.search_raw('JFK','LAX','2026-11-30',transport=broker.config())
    broker.wait_releases(1)
    prefix=next(route for key,route in _adaptive._routes.items() if key[0].startswith('ipv6-prefix:'))
    assert prefix.until-clock()>119
    prefix.until=clock()-1
    key=_adaptive._prefix_key(broker.config())
    probe=_adaptive._acquire(key)
    assert probe and probe[2]
    swoop.search_raw('JFK','LAX','2026-11-30',transport=broker.config())
    assert len(broker.requests)==1
    _adaptive._finish(probe,None,healthy=True)
    failed[0]=False
    swoop.search_raw('JFK','LAX','2026-11-30',transport=broker.config())
    broker.wait_releases(2)
    assert prefix.reason is None


@pytest.mark.parametrize('kwargs',[
    {'local_address':'0.0.0.0'}, {'local_address':'::'}, {'local_address':'ff02::1'},
    {'local_address':'fe80::1%lo'}, {'local_ipv6_mode':'other'},
    {'local_ipv6_socket':'relative','local_ipv6_prefix':PREFIX},
    {'local_ipv6_socket':'/tmp/x'}, {'local_ipv6_prefix':PREFIX},
    {'local_ipv6_socket':'/tmp/x','local_ipv6_prefix':'fd42::/48'},
])
def test_source_configuration_is_explicit_and_validated(kwargs):
    with pytest.raises(ValueError):
        swoop.TransportConfig(address_family='ipv6',**kwargs)


def test_reuse_mode_closes_clients_before_returning_reusable_address(monkeypatch,tmp_path):
    broker=Broker(tmp_path,lambda r:{**r,'address':'fd42:5357:1::a'})
    try:
        edge=Edge(monkeypatch)
        for _ in range(2):
            swoop.search_raw('JFK','LAX','2026-11-30',transport=broker.config(local_ipv6_mode='reuse'))
        broker.wait_releases(2)
        assert len(edge.clients)==2 and all(c.closed for c in edge.clients)
        assert {c.source for c in edge.clients}=={'fd42:5357:1::a'}
        assert not rpc._clients
        assert all(request['mode']=='reuse' for request in broker.requests)
    finally:
        broker.close()


@pytest.mark.parametrize('name',['deals','explore','hotels','hotel_prices','hotel_reviews'])
def test_all_real_session_producers_use_the_lease_and_close_their_jars(monkeypatch,broker,name):
    from urllib.parse import parse_qs,urlsplit
    from tests.test_hotels import _batchexecute
    def reply(client,url,kwargs):
        rpcid=parse_qs(urlsplit(url).query).get('rpcids',[None])[0]
        return SimpleNamespace(status_code=200,headers={},text=_batchexecute(rpcid,[]) if rpcid else encode_rpc_outer([]))
    edge=Edge(monkeypatch,reply)
    kwargs=dict(transport=broker.config())
    if name in ('hotels','hotel_prices'):
        kwargs.update(check_in='2026-11-30',check_out='2026-12-03',currency='EUR')
    getattr(swoop,name)('JFK' if name in ('deals','explore') else 'token',**kwargs)
    broker.wait_releases(1)
    assert len(edge.clients)==1 and edge.clients[0].source=='fd42:5357:1::a' and edge.clients[0].closed
    assert [c[1] for c in edge.calls]==['GET','POST']


def test_real_c47_next_and_price_keep_wire_intent_with_fresh_source_leases(monkeypatch,broker):
    from urllib.parse import parse_qs
    from tests.factories import decode_f_req
    from swoop import _selection
    outbound=itinerary('JFK','MIA','PTY','2026-11-16','117')
    inbound=itinerary('PTY','ORD','JFK','2026-11-23','119')
    out_text=encode_rpc_outer(make_full_response(other_itins=[outbound]))
    in_text=encode_rpc_outer(make_full_response(other_itins=[inbound]))
    decoded=rpc._parse_rpc_response(out_text)
    original_legs=[rpc._normalize_rpc_leg('JFK','PTY','2026-11-16'),
        rpc._normalize_rpc_leg('PTY','JFK','2026-11-23',airlines=['AA','UA'],max_stops=1)]
    selector=_selection.encode_trip_selector(request_legs=original_legs,itineraries=[decoded.other[0]],
        cabin='economy',include_basic_economy=True,exclude_separate_tickets=True,
        transport=swoop.TransportConfig(country='PA',currency='EUR'))
    requests=[]
    def reply(client,url,kwargs):
        body=decode_f_req(parse_qs(kwargs['content'].decode())['f.req'][0])
        segments=body[1][13]
        requests.append((client.source,segments))
        return SimpleNamespace(status_code=200,text=in_text if segments[0][8] else out_text,headers={})
    edge=Edge(monkeypatch,reply)
    result=swoop.search_next_leg(selector,next_leg_filters={'airlines':['AA'],'exclude_connecting_airports':['MIA']},transport=broker.config())
    assert len(result.results)==1
    retained=_selection.decode_trip_selector(result.results[0].selector)
    assert retained['query_legs'][1]['airlines']==['AA']
    assert retained['query_legs'][1]['exclude_connecting_airports']==['MIA']
    assert retained['selected_legs'][0]==_selection.decode_trip_selector(selector)['selected_legs'][0]
    assert all(source=='fd42:5357:1::a' for source,_ in requests)
    assert requests[0][1][1][10] is None and requests[1][1][1][10]==['MIA']
    start=len(requests)
    assert swoop.price_selector(result.results[0].selector,transport=broker.config()).price==350
    assert all(source=='fd42:5357:1::b' and segments[1][10]==['MIA'] for source,segments in requests[start:])
    broker.wait_releases(2)
    assert len(edge.clients)==2 and all(c.closed for c in edge.clients)
