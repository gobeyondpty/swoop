"""Bounded native-client/session acceptance; run under ndc-cert-call on owner host.

Only HTTP status, timing, inventory counts and route health are retained. Never
write session tokens, cookies, selectors or provider bodies to the result.
"""
import dataclasses
from datetime import datetime, timezone
import http.client
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time
from typing import Any, Callable

source = Path(sys.argv[1])
root = Path('/srv/ndcterm/current/ndcterm').resolve()
site = root / 'apps/swoop-service/.venv/lib' / f'python{sys.version_info.major}.{sys.version_info.minor}' / 'site-packages'
sys.path[:0] = [str(source), str(site)]
import swoop
from swoop import _adaptive, rpc

proxy = 'socks5h://swoop-unified-probe:x@10.0.0.10:1082'
output: dict[str,Any] = dict(started_at=datetime.now(timezone.utc).isoformat(), production_root=str(root),
    source=str(source), query=dict(origin='JFK',destination='LAX',date='2026-11-30',window=['2026-11-01','2026-11-30'],country='US',currency='USD',adults=1,exclude_separate_tickets=True), rows=[], phase_timings=dict(dns_ms=None,tls_ms=None,connect_ms=None,ttfb_ms=None))
conn = http.client.HTTPConnection('127.0.0.1',3847,timeout=5)
conn.request('GET','/health')
output['production_health'] = json.loads(conn.getresponse().read())
conn.close()
path = Path(sys.argv[2])
results: dict[str,Any] = {}

def save():
    path.write_text(json.dumps(output,indent=2)+'\n')

def run(label: str, function: Callable[[Any],Any], *, config: dict[str,Any] | None = None) -> None:
    diagnostics = swoop.DiagnosticsRecorder()
    tc = swoop.TransportConfig(address_family='ipv6',fallback_proxies=(proxy,),
        timeout=30,request_timeout=8,retries=0,country='US',currency='USD',diagnostics=diagnostics)
    if config:
        tc = dataclasses.replace(tc, **config)
    fingerprints = []
    original_post = rpc._post_with_retry
    def recorded_post(client, url, body, headers, **kwargs):
        fingerprints.append(hashlib.sha256(body).hexdigest())
        return original_post(client,url,body,headers,**kwargs)
    rpc._post_with_retry = recorded_post
    started = time.perf_counter()
    row: dict[str,Any] = dict(operation=label)
    try:
        result = function(tc)
        results[label] = result
        values = next((getattr(result,name) for name in ('hotels','providers','reviews','deals','destinations','best','days')
                       if getattr(result,name,None) is not None), result if isinstance(result,list) else [])
        if label.startswith('shopping'):
            values = (getattr(result,"best",[]) + getattr(result,"other",[])) if result else []
        row.update(success=True,count=len(values))
    except Exception as error:
        row.update(success=False,error_type=type(error).__name__,cause_type=type(error.__cause__).__name__,
            grpc_code=getattr(error,'grpc_code',None),http_status=getattr(error,'status_code',None))
    rpc._post_with_retry = original_post
    row.update(request_body_sha256=fingerprints,elapsed_ms=(time.perf_counter()-started)*1000,
        diagnostics=dataclasses.asdict(diagnostics.snapshot()),health=_adaptive.health())
    output['rows'].append(row)
    save()
    print(json.dumps({k:v for k,v in row.items() if k not in ('diagnostics','health')}),flush=True)

calendar=lambda tc: swoop.get_calendar('JFK','LAX','2026-11-01','2026-11-30',exclude_separate_tickets=True,transport=tc)
shopping=lambda tc: swoop.search_raw('JFK','LAX','2026-11-30',exclude_separate_tickets=True,transport=tc)
# Same queries/bounds for isolated native direct vs pooled mia1. Stop a refused
# route after two failures rather than generating a load/throttling experiment.
for label, function in (() if len(sys.argv)>3 else (('calendar',calendar),('shopping',shopping))):
    for route,config in (('direct',dict(fallback_proxies=(),timeout=2,request_timeout=2)),
                         ('mia1',dict(address_family='auto',fallback_proxies=(),proxy=proxy,timeout=8))):
        for sample in range(2):
            run(f'{label}_{route}_{sample}',function,config=config)
# First call may spend at most 2s proving direct; later endpoints share cooldown.
for sample in range(0 if len(sys.argv)>3 else 3):
    run(f'calendar_policy_{sample}',calendar)
    run(f'shopping_policy_{sample}',shopping)
run('deals_policy',lambda tc: swoop.deals('JFK',transport=tc))
run('explore_policy',lambda tc: swoop.explore('JFK',one_way=True,transport=tc))
run('hotels_policy',lambda tc: swoop.hotels('New York',check_in='2026-11-30',check_out='2026-12-03',transport=tc))
run('hotel_token_policy',lambda tc: swoop.hotels('HI New York City Hostel',check_in='2026-11-30',check_out='2026-12-03',transport=tc))
hotels = results.get('hotel_token_policy')
if hotels:
    token = next((h.booking_token for h in hotels.hotels if h.booking_token),None)
    if isinstance(token,str):
        run('hotel_prices_policy',lambda tc, token=token: swoop.hotel_prices(token,query='New York',check_in='2026-11-30',check_out='2026-12-03',transport=tc))
        run('hotel_reviews_policy',lambda tc, token=token: swoop.hotel_reviews(token,transport=tc))
output['finished_at'] = datetime.now(timezone.utc).isoformat()
save()
