"""Spaced, bounded Google RPC diagnosis; never changes production transport.

Compare identical fresh curl requests across source route/protocol. Same Google
IPv6 destination is pinned for direct and SOCKS paths, preserving hostname/SNI.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

root=Path('/srv/ndcterm/current/ndcterm').resolve()
sys.path.insert(0,str(root/'apps/swoop-service/.venv/lib/python3.13/site-packages'))
import swoop
from swoop import rpc

capture: dict[str,Any]={}
class Capture:
    def post(self,url: str,*,content: bytes,headers: dict[str,str],**kwargs: Any) -> Any:
        capture.update(url=url,body=content,headers=headers.copy())
        raise swoop.SwoopTransportError('capture only')
original=rpc._get_transport_client
rpc._get_transport_client=lambda transport:Capture()
try:
    try:
        swoop.search_raw('JFK','LAX','2026-11-30',sort=swoop.SORT_CHEAPEST,exclude_separate_tickets=True,
            transport=swoop.TransportConfig(country='US',currency='USD',timeout=6,retries=0))
    except swoop.SwoopTransportError:
        if not capture:
            raise
finally:
    rpc._get_transport_client=original
ip6='2001:4860:4828:7700::'
ip4=socket.getaddrinfo('www.google.com',443,socket.AF_INET,socket.SOCK_STREAM)[0][4][0]
identity='throttle-probe-'+uuid4().hex[:12]
rows=[]
paused=set()
with tempfile.TemporaryDirectory(prefix='swoop-throttle-probe-') as tmp:
    directory=Path(tmp)
    request=directory/'request'
    request.write_bytes(capture['body'])
    def run(label: str,*,url: str,post: bool=True,proxy: bool=False,ipv4: bool=False,http1: bool=False) -> dict[str,Any]:
        response=directory/'response'
        response.unlink(missing_ok=True)
        headers_file=directory/'headers'
        header_args=[]
        for key,value in capture['headers'].items():
            header_args+=['--header',key+': '+value]
        command=['curl','--silent','--show-error','--verbose','--max-time','6','--connect-timeout','3',
            '--http1.1' if http1 else '--http2','--ipv4' if ipv4 else '--ipv6',
            '--resolve',f'www.google.com:443:{ip4}' if ipv4 else f'www.google.com:443:[{ip6}]',
            '--output',str(response),'--dump-header',str(headers_file),'--write-out','%{json}']
        if proxy:
            # Local resolution passes the exact pinned IPv6 address in SOCKS,
            # while the original URL keeps hostname/SNI/certificate checks.
            command+=['--proxy',f'socks5://{identity}:x@10.0.0.10:1082','--noproxy','']
            # The proxy itself is on IPv4 mesh; only the Google target is IPv6.
            command.remove('--ipv6')
        else:
            command+=['--noproxy','*']
        if post:
            command+=['--request','POST','--data-binary','@'+str(request),*header_args]
        command.append(url)
        started=time.perf_counter()
        process=subprocess.run(command,capture_output=True,text=True,timeout=9)
        metrics=json.loads(process.stdout)
        row: dict[str,Any]=dict(label=label,timestamp=datetime.now(timezone.utc).isoformat(),
            exit_code=process.returncode,status=metrics.get('http_code'),http_version=metrics.get('http_version'),
            remote_ip=metrics.get('remote_ip'),local_ip=metrics.get('local_ip'),
            pinned_google_ip=ip4 if ipv4 else ip6,elapsed_ms=(time.perf_counter()-started)*1000,
            time_connect_ms=metrics.get('time_connect',0)*1000,tls_complete_ms=metrics.get('time_appconnect',0)*1000,
            pretransfer_ms=metrics.get('time_pretransfer',0)*1000,starttransfer_ms=metrics.get('time_starttransfer',0)*1000,
            curl_total_ms=metrics.get('time_total',0)*1000,
            socks_fixed_destination_confirmed=any('SOCKS5 connect to' in line and ip6 in line for line in process.stderr.splitlines()) if proxy else None)
        if headers_file.exists():
            for line in headers_file.read_text(errors='replace').splitlines():
                key,sep,value=line.partition(':')
                if sep and key.lower() in ('retry-after','content-type','server'):
                    row[key.lower().replace('-','_')]=value.strip()
                if sep and key.lower()=='location':
                    target=urlsplit(value.strip())
                    row['redirect_host']=target.netloc
                    row['redirect_path']=target.path
        text=response.read_text(errors='replace') if response.exists() else ''
        row['unusual_traffic_marker']='unusual traffic' in text.lower()
        row['sorry_redirect']=row.get('redirect_path','').startswith('/sorry')
        row['decoded_utf8_bytes']=len(text.encode())
        if row['status']==429:
            paused.add(label)
        if post and row['status']==200:
            try:
                result=rpc._parse_rpc_response(text)
                row['count']=len(result.best)+len(result.other) if result else 0
                row['usable_rpc']=row['count']>0
            except Exception as error:
                row.update(usable_rpc=False,error_type=type(error).__name__,grpc_code=getattr(error,'grpc_code',None),grpc_type_url=getattr(error,'type_url',None))
        rows.append(row)
        print(json.dumps(row),flush=True)
        return row
    run('direct_v6_connectivity_control',url='https://www.google.com/generate_204',post=False)
    run('direct_v6_flights_page',url='https://www.google.com/travel/flights?hl=en&gl=US',post=False)
    routes=[('direct_v6_h2',{}),('mia1_v6_h2',dict(proxy=True)),('direct_v4_h2',dict(ipv4=True)),('direct_v6_h1',dict(http1=True))]
    for sample in range(3):
        if sample:
            time.sleep(3)
        order=routes if sample%2==0 else list(reversed(routes))
        for label,options in order:
            if label in paused:
                continue
            run(label,url=capture['url'],**options)['sample']=sample
output=dict(release=str(root),query=dict(origin='JFK',destination='LAX',date='2026-11-30',country='US',currency='USD',adults=1),
    request_sha256=hashlib.sha256(capture['body']).hexdigest(),rows=rows,
    constraints='No redirects/retries, max 6s each, fresh connections, 3s between rounds, stop a route on HTTP429. Curl-only diagnostic; native production client unchanged.')
Path('/tmp/swoop-throttle-probe.json').write_text(json.dumps(output,indent=2)+'\n')
