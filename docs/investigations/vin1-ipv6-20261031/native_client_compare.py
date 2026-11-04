"""Eight bounded, fresh-connection diagnostic calls to one IPv6 destination.

Profiles/clients differ in implicit HTTP/TLS settings. This does not isolate TLS
or establish an IP ban. The curl path remains diagnostic and is never shipped.
"""
from datetime import datetime, timedelta, timezone
from ipaddress import IPv6Address
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any

root=Path('/srv/ndcterm/current/ndcterm').resolve()
site=root/'apps/swoop-service/.venv/lib/python3.13/site-packages'
sys.path.insert(0,str(site))
import swoop
from swoop import rpc
import wreq
from wreq.blocking import Client

captured: dict[str,Any]={}
class Capture:
    def post(self,url: str,*,content: bytes,headers: dict[str,str],**kwargs: Any) -> Any:
        captured.update(url=url,body=content,headers=headers)
        raise swoop.SwoopTransportError('capture only')
original=rpc._get_transport_client
rpc._get_transport_client=lambda tc:Capture()
try:
    try:
        swoop.search_raw('JFK','LAX','2026-11-30',sort=swoop.SORT_CHEAPEST,exclude_separate_tickets=True,
            transport=swoop.TransportConfig(country='US',currency='USD',timeout=4,retries=0))
    except swoop.SwoopTransportError:
        if not captured:
            raise
finally:
    rpc._get_transport_client=original

address='2001:4860:4828:7700::'
rows=[]
with tempfile.TemporaryDirectory(prefix='native-client-compare-') as folder:
    body=Path(folder)/'request'
    body.write_bytes(captured['body'])
    for sample in range(2):
        profiles=['Chrome137','Chrome146','default','curl']
        if sample:
            profiles.reverse()
        for profile in profiles:
            row: dict[str,Any]=dict(sample=sample,profile=profile,client_state='fresh',ipv6_destination=address,
                request_sha256=hashlib.sha256(captured['body']).hexdigest())
            phase='post_return'
            started=time.perf_counter()
            try:
                if profile=='curl':
                    response_path=Path(folder)/'response'
                    command=['curl','--ipv6','--http2','--noproxy','*','--max-time','4','--silent','--show-error',
                             '--resolve',f'www.google.com:443:[{address}]','--request','POST','--data-binary','@'+str(body),
                             '--output',str(response_path),'--write-out','%{json}']
                    for key,value in captured['headers'].items():
                        command+=['--header',key+': '+value]
                    command.append(captured['url'])
                    process=subprocess.run(command,capture_output=True,text=True,timeout=6)
                    metrics=json.loads(process.stdout)
                    row.update(status=metrics['http_code'],http_version=metrics['http_version'],
                               remote_addr=metrics.get('remote_ip'),connect_ms=metrics.get('time_connect',0)*1000,
                               tls_complete_ms=metrics.get('time_appconnect',0)*1000,curl_total_ms=metrics.get('time_total',0)*1000)
                    if process.returncode:
                        raise TimeoutError('diagnostic curl request failed')
                    text=response_path.read_text()
                else:
                    dns=wreq.DnsOptions(lookup_ip_strategy=wreq.LookupIpStrategy.IPV6_ONLY)
                    dns.add_resolve('www.google.com',[IPv6Address(address)])
                    options: dict[str,Any]=dict(no_proxy=True,local_address='::',dns_options=dns,
                        tls_verify=True,tls_verify_hostname=True,cookie_store=True,redirect=wreq.Policy.none())
                    if profile!='default':
                        options['emulation']=getattr(wreq.Emulation,profile)
                    client=Client(**options)
                    row['client_init_ms']=(time.perf_counter()-started)*1000
                    dispatch_started=time.perf_counter()
                    response=client.post(captured['url'],body=captured['body'],headers=captured['headers'],timeout=timedelta(seconds=4))
                    row.update(status=response.status.as_int(),http_version=str(response.version),remote_addr=str(response.remote_addr),
                               post_return_ms=(time.perf_counter()-dispatch_started)*1000)
                    phase='text_access'
                    body_started=time.perf_counter()
                    text=response.text()
                    row['text_access_ms']=(time.perf_counter()-body_started)*1000
                phase='decode'
                result=rpc._parse_rpc_response(text) if row['status']==200 else None
                row['count']=len(result.best)+len(result.other) if result else 0
                row['success']=row['status']==200 and row['count']>0
            except Exception as error:
                row.update(success=False,error_type=type(error).__name__,failed_stage=phase,grpc_code=getattr(error,'grpc_code',None))
            row['elapsed_ms']=(time.perf_counter()-started)*1000
            rows.append(row)
            print(json.dumps(row),flush=True)
output=dict(timestamp=datetime.now(timezone.utc).isoformat(),release=str(root),rows=rows,
    notes='All calls fresh, one fixed IPv6 destination, identical SDK payload/explicit headers. Implicit headers and TLS/HTTP profiles differ. Native DNS/connect/TLS/TTFB timings remain unknown; post_return is a program boundary, not a handshake phase.')
Path('/tmp/swoop-native-client-compare.json').write_text(json.dumps(output,indent=2)+'\n')
