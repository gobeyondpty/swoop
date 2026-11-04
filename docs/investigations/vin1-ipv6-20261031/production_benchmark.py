"""Read-only vin1 acceptance + matched real-client benchmark; no release changes.

Run with native python3 -I -S on vin1, guarded by ndc-cert-call on the owner host.
Outputs contain measurements/query bounds, never selectors or raw provider bodies.
"""
from __future__ import annotations

import argparse
import dataclasses
from datetime import datetime, timezone
import hashlib
import importlib
import http.client
import json
import math
from pathlib import Path
import statistics
import sys
import time
from typing import Any
from uuid import uuid4


def main() -> None:
    parser=argparse.ArgumentParser()
    parser.add_argument('--samples',type=int,default=5)
    parser.add_argument('--api-samples',type=int,default=3)
    parser.add_argument('--phase',choices=('all','matched','api'),default='all')
    parser.add_argument('--output',default='/tmp/swoop-production-benchmark.json')
    args=parser.parse_args()
    if not 1 <= args.samples <= 8 or not 1 <= args.api_samples <= 5:
        raise ValueError('bounded sample counts required')
    root=Path('/srv/ndcterm/current/ndcterm').resolve()
    app=root/'apps/swoop-service'
    site=app/'.venv/lib'/f'python{sys.version_info.major}.{sys.version_info.minor}'/'site-packages'
    sys.path[:0]=[str(site),str(app)]
    import swoop
    from swoop import rpc, _adaptive
    from swoop._native import NativeClient
    from swoop._calendar import _parse_calendar_days
    from swoop._selection import decode_trip_selector
    loaded_swoop_commit=importlib.import_module("runtime").loaded_swoop_commit

    output: dict[str,Any]=dict(started_at=datetime.now(timezone.utc).isoformat(),release=str(root),
        sdk_commit=loaded_swoop_commit(swoop),phase=args.phase,samples=args.samples,api_samples=args.api_samples,
        query=dict(origin='JFK',destination='LAX',date='2026-11-30',window=['2026-11-01','2026-11-30'],
                   country='US',currency='USD',adults=1,cabin='economy',include_basic_economy=True,exclude_separate_tickets=True),
        raw_rows=[],api_rows=[],functional=[],phase_timings=dict(dns_ms=None,connect_ms=None,tls_ms=None,ttfb_ms=None))
    path=Path(args.output)
    def save() -> None:
        path.write_text(json.dumps(output,indent=2)+'\n')
    def emit(row: dict[str,Any]) -> None:
        print(json.dumps(row),flush=True)
        save()
    conn=http.client.HTTPConnection('127.0.0.1',3847,timeout=30)
    def health() -> dict[str,Any]:
        conn.request('GET','/health')
        res=conn.getresponse()
        return json.loads(res.read())
    output['health_before']=health()
    if output['health_before']['swoop_commit']!=output['sdk_commit'] or output['health_before']['transport_primary']!='native_ipv6':
        raise RuntimeError('Live worker and sealed SDK identity do not match native deployment')
    emit({'phase':'identity','release':str(root),'health':output['health_before']})

    common=dict(origin='JFK',destination='LAX',date='2026-11-01',date_end='2026-11-30',mode='picker',
                cabin='5',adults=1,country='US',currency='USD',include_basic_economy=True)
    search=dict(origin='JFK',destination='LAX',date='2026-11-30',cabin='5',adults=1,country='US',currency='USD',
                include_basic_economy=True,sort='cheapest',expand_legs=False)
    cases=[('calendar_one_way','/calendar',common),
           ('calendar_roundtrip','/calendar',{**common,'min_stay':5,'max_stay':5}),
           ('shopping','/search',search)]
    captured: dict[str,Any]={}
    class Capture:
        def post(self,url: str,*,content: bytes,headers: dict[str,str],**kwargs: Any) -> Any:
            captured.update(url=url,body=content,headers=headers.copy())
            raise swoop.SwoopTransportError('capture only; no dispatch')
    original=rpc._get_transport_client
    requests: dict[str,Any]={}
    rpc._get_transport_client=lambda transport: Capture()
    try:
        for operation,_,_ in cases:
            captured.clear()
            try:
                tc=swoop.TransportConfig(country='US',currency='USD',timeout=4,retries=0)
                if operation=='shopping':
                    swoop.search_raw('JFK','LAX','2026-11-30',sort=swoop.SORT_CHEAPEST,exclude_separate_tickets=True,transport=tc)
                else:
                    stay: dict[str,Any]={'min_stay':5,'max_stay':5} if operation=='calendar_roundtrip' else {}
                    swoop.get_calendar('JFK','LAX','2026-11-01','2026-11-30',exclude_separate_tickets=True,transport=tc,**stay)
            except swoop.SwoopTransportError:
                if not captured:
                    raise
            requests[operation]=dict(captured)
    finally:
        rpc._get_transport_client=original

    proxy=f'socks5h://benchmark-{uuid4().hex[:12]}:x@10.0.0.10:1082'
    output['client_init_ms']={}
    output['stopped_routes']=[]
    for operation,_,_ in (cases if args.phase!='api' else []):
        clients: dict[str,Any]={}
        for route in ('native_direct','native_mia1','primp_mia1'):
            started=time.perf_counter()
            if route=='primp_mia1':
                from primp import Client
                clients[route]=Client(impersonate='chrome',proxy=proxy)
            else:
                clients[route]=NativeClient('ipv6',proxy=proxy if route=='native_mia1' else None)
            output['client_init_ms'][operation+'/'+route]=(time.perf_counter()-started)*1000
        failures={route:0 for route in clients}
        seen={route:set() for route in clients}
        stopped=set()
        request=requests[operation]
        fingerprint=hashlib.sha256(request['url'].encode()+request['body']+json.dumps(request['headers'],sort_keys=True).encode()).hexdigest()
        for sample in range(args.samples):
            routes=list(clients) if sample%2==0 else list(reversed(clients))
            for route in routes:
                if route in stopped:
                    continue
                recorder=swoop.DiagnosticsRecorder()
                tc=swoop.TransportConfig(timeout=4,retries=0,country='US',currency='USD',diagnostics=recorder)
                meta: dict[str,Any]={}
                class TimedClient:
                    def post(self,*a: Any,**kw: Any) -> Any:
                        response=clients[route].post(*a,**kw)
                        native=getattr(response,'_response',None)
                        if native is not None:
                            local=str(native.local_addr)
                            meta.update(http_version=str(native.version),remote_addr=str(native.remote_addr),local_addr=local,
                                        connection_reused=local in seen[route])
                            seen[route].add(local)
                        return response
                started=time.perf_counter()
                row: dict[str,Any]=dict(operation=operation,route=route,sample=sample,client_state='cold' if sample==0 else 'pooled',request_sha256=fingerprint)
                try:
                    response=rpc._post_with_retry(TimedClient(),request['url'],request['body'],request['headers'],transport=tc)
                    row['http_status']=response.status_code
                    if operation=='shopping':
                        result=rpc._parse_rpc_response(response.text)
                        offers=result.best+result.other if result else []
                        row['count']=len(offers)
                        row['bounds_match']=all(x.departure_airport_code=='JFK' and x.arrival_airport_code=='LAX' and tuple(x.departure_date)==(2026,11,30) for x in offers)
                    else:
                        days=_parse_calendar_days(response.text,endpoint='GetCalendarPicker')
                        row['count']=len(days)
                        row['prices']=[dict(date=d.departure_date,return_date=d.return_date,price=d.price,currency=d.currency) for d in days]
                        row['bounds_match']=all('2026-11-01'<=d.departure_date<='2026-11-30' and d.currency=='USD' for d in days)
                    row['success']=row['count']>0 and row['bounds_match']
                    failures[route]=0 if row['success'] else failures[route]+1
                except Exception as error:
                    row.update(success=False,error_type=type(error).__name__,cause_type=type(error.__cause__).__name__,
                               grpc_code=getattr(error,'grpc_code',None),grpc_type_url=getattr(error,'type_url',None),
                               http_status=getattr(error,'status_code',None) or row.get('http_status'))
                    failures[route]+=1
                    if getattr(error,'status_code',None)==429:
                        stopped.add(route)
                row.update(elapsed_ms=(time.perf_counter()-started)*1000,diagnostics=dataclasses.asdict(recorder.snapshot()),**meta)
                output['raw_rows'].append(row)
                emit({'phase':'matched_rpc',**{k:v for k,v in row.items() if k not in ('diagnostics','prices')}})
                if failures[route]>=2:
                    stopped.add(route)
                if route in stopped:
                    output['stopped_routes'].append(dict(operation=operation,route=route,after_sample=sample,reason='rate_limit_or_two_consecutive_failures'))

    # The raw benchmark may exceed the service's keepalive idle timeout.
    # Open a fresh HTTP connection before measuring actual API calls.
    conn.close()
    conn=http.client.HTTPConnection('127.0.0.1',3847,timeout=30)
    selectors: dict[str,str]={}
    def api(label: str,endpoint: str,body: dict[str,Any],*,functional: bool=False) -> tuple[dict[str,Any],dict[str,Any]]:
        started=time.perf_counter()
        row: dict[str,Any]=dict(operation=label,route='production_api',endpoint=endpoint)
        data: dict[str,Any]={}
        try:
            conn.request('POST',endpoint,json.dumps(body),headers={'Content-Type':'application/json','X-Swoop-Request-ID':str(uuid4())})
            res=conn.getresponse()
            data=json.loads(res.read())
            row.update(http_status=res.status,success=res.status==200,diagnostics=data.get('diagnostics'))
            if res.status!=200:
                row['error_detail']=data.get('detail')
            elif endpoint=='/calendar':
                row['count']=len(data.get('prices',[]))
                row['success']=row['count']>0
            elif endpoint in ('/search','/search-next'):
                trips=data.get('results',[])
                row.update(count=len(trips),is_complete=data.get('is_complete'),result_scope=data.get('result_scope'))
                row['success']=bool(trips)
                if trips:
                    selector=trips[0]['selector']
                    payload=decode_trip_selector(selector)
                    row['selector_context_match']=payload.get('country')=='US' and payload.get('currency')=='USD' and payload.get('exclude_separate_tickets') is True
                    row['success']=row['success'] and row['selector_context_match']
                    selectors[label]=selector
            elif endpoint=='/price':
                row.update(is_estimate=data.get('is_estimate'),currency=data.get('currency'),price=data.get('price'),booking_options=len(data.get('booking_options',[])))
                row['success']=data.get('currency')=='USD' and bool(data.get('price')) and data.get('is_estimate') is False
        except Exception as error:
            row.update(success=False,error_type=type(error).__name__)
            conn.close()
        row['elapsed_ms']=(time.perf_counter()-started)*1000
        output['functional' if functional else 'api_rows'].append(row)
        emit({'phase':'functional' if functional else 'production_api',**{k:v for k,v in row.items() if k!='diagnostics'}})
        return row,data
    if args.phase!='matched':
        for sample in range(args.api_samples):
            for operation,endpoint,body in cases:
                row,_=api(operation,endpoint,body)
                row['sample']=sample
        api('calendar_grid','/calendar',{**common,'mode':'grid','date':'2026-11-30','date_end':'2026-12-02'},functional=True)
        api('calendar_graph','/calendar',{**common,'mode':'graph','date':'2026-11-30','date_end':'2026-12-02','min_stay':5,'max_stay':5},functional=True)
        api('shopping_roundtrip','/search',{**search,'return_date':'2026-12-05'},functional=True)
        if 'shopping_roundtrip' in selectors:
            api('continue_roundtrip','/search-next',{'selector':selectors['shopping_roundtrip']},functional=True)
        if 'shopping' in selectors:
            api('selector_price','/price',{'selector':selectors['shopping']},functional=True)
            api('selector_details','/details',{'selector':selectors['shopping']},functional=True)
        else:
            output['functional'].append(dict(operation='selector_price_and_details',success=False,error_type='NoSuccessfulShoppingSelector'))

    # Health collection does not mutate worker policy. Fault recovery is covered
    # by offline tests and the earlier isolated natural-cooldown evidence.
    output['health_after']=health()
    output['finished_at']=datetime.now(timezone.utc).isoformat()
    output['release_after']=str(Path('/srv/ndcterm/current/ndcterm').resolve())
    output['release_changed']=output['release_after']!=output['release']
    groups=[]
    for source in ('raw_rows','api_rows'):
        rows=output[source]
        for key in sorted({(r['operation'],r['route']) for r in rows}):
            sample=[r for r in rows if (r['operation'],r['route'])==key]
            good=[r['elapsed_ms'] for r in sample if r.get('success')]
            warm=[r['elapsed_ms'] for r in sample if r.get('success') and (r.get('sample',0)>0)]
            groups.append(dict(operation=key[0],route=key[1],attempts=len(sample),successes=len(good),
                success_median_ms=statistics.median(good) if good else None,
                pooled_success_median_ms=statistics.median(warm) if warm else None,
                success_p95_ms=sorted(good)[max(0,math.ceil(.95*len(good))-1)] if good else None,
                all_attempts_mean_ms=statistics.mean(r['elapsed_ms'] for r in sample)))
    output['summary']=groups
    for operation in ('calendar_one_way','calendar_roundtrip'):
        calendars=[row['prices'] for row in output['raw_rows'] if row['operation']==operation and row.get('success')]
        output[operation+'_prices_match']=all(value==calendars[0] for value in calendars) if calendars else None
    save()
    emit({'phase':'complete','summary':groups,'functional':[{k:v for k,v in r.items() if k!='diagnostics'} for r in output['functional']]})
    conn.close()


if __name__=='__main__':
    main()
