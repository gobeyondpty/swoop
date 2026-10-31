"""Actual native AF_INET6 plus actual SOCKS fallback, pooling and recovery."""
import json
import select
import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import swoop
from swoop import _adaptive, _deals, rpc


def test_real_native_ipv6_deals_session_socks_fallback_and_recovery(monkeypatch):
    pytest.importorskip('wreq')
    calls = []
    blocked = [True]
    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6
        daemon_threads = True
    def handler(route):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def do_GET(self):
                calls.append((route,'GET',self.client_address,self.path,None))
                body = b'session bootstrap'
                self.send_response(200)
                self.send_header('Set-Cookie',f'route={route}; Path=/')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                calls.append((route,'POST',self.client_address,self.path,
                    (body,self.headers.get('Cookie'),self.headers.get('x-goog-ext-259736195-jspb'))))
                status = 403 if route == 'direct' and blocked[0] else 200
                payload = json.dumps([['wrb.fr',None,'[]']]).encode() if status == 200 else b''
                self.send_response(status)
                self.send_header('Content-Length',str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self,*args):
                pass
        return Handler
    try:
        direct = Server(('::1',0),handler('direct'))
        fallback = Server(('::1',0),handler('mia-fixture'))
    except OSError:
        pytest.skip('IPv6 loopback unavailable')
    def read_exact(stream, size):
        data = b''
        while len(data) < size:
            part = stream.recv(size-len(data))
            if not part:
                raise EOFError
            data += part
        return data
    proxy_targets = []
    class Socks(socketserver.BaseRequestHandler):
        def handle(self):
            peer = self.request
            target = None
            try:
                version, count = read_exact(peer,2)
                assert version == 5
                read_exact(peer,count)
                peer.sendall(b'\x05\x00')
                version,command,_,family = read_exact(peer,4)
                assert version == 5 and command == 1
                if family == 4:
                    host = socket.inet_ntop(socket.AF_INET6,read_exact(peer,16))
                elif family == 3:
                    host = read_exact(peer,read_exact(peer,1)[0]).decode()
                else:
                    host = socket.inet_ntop(socket.AF_INET,read_exact(peer,4))
                port = int.from_bytes(read_exact(peer,2),'big')
                proxy_targets.append((host,port))
                target = socket.create_connection(('::1',fallback.server_port),timeout=2)
                peer.sendall(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00')
                while True:
                    ready,_,_ = select.select([peer,target],[],[],2)
                    if not ready:
                        break
                    for source in ready:
                        data = source.recv(65536)
                        if not data:
                            return
                        (target if source is peer else peer).sendall(data)
            except (EOFError,OSError):
                pass
            finally:
                if target:
                    target.close()
    class Proxy(socketserver.ThreadingTCPServer):
        daemon_threads = True
    proxy = Proxy(('127.0.0.1',0),Socks)
    threads=[]
    for server in (direct,fallback,proxy):
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        threads.append(thread)
    base=f'http://[::1]:{direct.server_port}'
    monkeypatch.setattr(_deals,'DEALS_PAGE_URL',base+'/travel/flights/deals')
    monkeypatch.setattr(_deals,'DEALS_RPC_URL',base+'/GetFlightDealsStreaming')
    clock=[100.0]
    monkeypatch.setattr(_adaptive.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(_adaptive.random,'uniform',lambda *_:1.0)
    _adaptive._routes.clear()
    rpc._clients.clear()
    tc=swoop.TransportConfig(address_family='ipv6',fallback_proxies=(f'socks5h://127.0.0.1:{proxy.server_address[1]}',),
        timeout=10,request_timeout=1,country='PA',currency='EUR')
    try:
        assert swoop.deals('JFK',transport=tc).deals == []
        assert _adaptive.health()[0]['reason'] == 'http_403'
        assert swoop.deals('JFK',transport=tc).deals == []
        blocked[0]=False
        clock[0]=131.0
        assert swoop.deals('JFK',transport=tc).deals == []
        assert _adaptive.health()[0]['state'] == 'closed'
        assert [c[0] for c in calls] == ['direct']*2+['mia-fixture']*4+['direct']*2
        posts=[c for c in calls if c[1]=='POST']
        assert all(c[4][0] == posts[0][4][0] and c[4][2] == posts[0][4][2] for c in posts)
        assert all(c[4][1] == 'route='+c[0] for c in posts)
        assert all(c[2][0] == '::1' for c in calls)
        assert len({c[2][1] for c in calls if c[0]=='direct'}) == 1
        assert len({c[2][1] for c in calls if c[0]=='mia-fixture'}) == 1
        assert proxy_targets == [('::1',direct.server_port)]
    finally:
        rpc._clients.clear()
        _adaptive._routes.clear()
        for server in (direct,fallback,proxy):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(2)
