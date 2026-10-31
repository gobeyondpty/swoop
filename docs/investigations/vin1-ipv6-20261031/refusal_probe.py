"""Two isolated 12-second diagnostics for delayed native RPC refusals.

Production deadlines/configuration are untouched. Redirect query strings,
cookies and bodies are never saved; record only status/path and safe markers.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from typing import Any
from urllib.parse import urlsplit

root=Path('/srv/ndcterm/current/ndcterm').resolve()
sys.path.insert(0,str(root/'apps/swoop-service/.venv/lib/python3.13/site-packages'))
import swoop
from swoop import rpc
from swoop._native import NativeClient
from swoop._calendar import _parse_calendar_days

captured: dict[str,Any]={}
class Capture:
    def post(self,url: str,*,content: bytes,headers: dict[str,str],**kwargs: Any) -> Any:
        captured.update(url=url,body=content,headers=headers.copy())
        raise swoop.SwoopTransportError('capture only')
rows=[]
client=NativeClient('ipv6')
for operation in ('calendar_one_way','shopping'):
    original=rpc._get_transport_client
    rpc._get_transport_client=lambda transport:Capture()
    try:
        try:
            tc=swoop.TransportConfig(country='US',currency='USD',retries=0)
            if operation=='shopping':
                swoop.search_raw('JFK','LAX','2026-11-30',sort=swoop.SORT_CHEAPEST,exclude_separate_tickets=True,transport=tc)
            else:
                swoop.get_calendar('JFK','LAX','2026-11-01','2026-11-30',exclude_separate_tickets=True,transport=tc)
        except swoop.SwoopTransportError:
            if not captured:
                raise
    finally:
        rpc._get_transport_client=original
    started=time.perf_counter()
    row: dict[str,Any]=dict(operation=operation,timestamp=datetime.now(timezone.utc).isoformat(),diagnostic_timeout_seconds=12)
    try:
        response=client.post(captured['url'],content=captured['body'],headers=captured['headers'],timeout=12)
        row.update(status=response.status_code,headers_returned_ms=(time.perf_counter()-started)*1000,
            http_version=str(response._response.version),remote_addr=str(response._response.remote_addr),local_addr=str(response._response.local_addr))
        for name in ('location','retry-after','server','content-type'):
            value=response.headers[name]
            if value is None:
                continue
            value=value if isinstance(value,str) else bytes(value).decode('utf-8',errors='replace')
            if name=='location':
                target=urlsplit(value)
                row.update(redirect_host=target.netloc,redirect_path=target.path)
            else:
                row[name.replace('-','_')]=value
        text=response.text
        row.update(decoded_utf8_bytes=len(text.encode()),unusual_traffic_marker='unusual traffic' in text.lower(),
                   sorry_redirect=row.get('redirect_path','').startswith('/sorry'))
        if response.status_code==200:
            if operation=='shopping':
                result=rpc._parse_rpc_response(text)
                row['count']=len(result.best)+len(result.other) if result else 0
            else:
                row['count']=len(_parse_calendar_days(text,endpoint='GetCalendarPicker'))
    except Exception as error:
        row.update(error_type=type(error).__name__,grpc_code=getattr(error,'grpc_code',None),grpc_type_url=getattr(error,'type_url',None))
    row['elapsed_ms']=(time.perf_counter()-started)*1000
    rows.append(row)
    print(json.dumps(row),flush=True)
Path('/tmp/swoop-refusal-probe.json').write_text(json.dumps(dict(release=str(root),rows=rows),indent=2)+'\n')
