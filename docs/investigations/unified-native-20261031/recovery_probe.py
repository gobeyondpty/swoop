"""Controlled failure + real mia1 RPC + real native recovery after real cooldown.

The injected failure is local test control, not an observed Google refusal.
Run under ndc-cert-call, isolated from the serving process. No wire/body rewrite.
"""
import dataclasses
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import Any

root=Path('/srv/ndcterm/current/ndcterm').resolve()
site=root/'apps/swoop-service/.venv/lib'/f'python{sys.version_info.major}.{sys.version_info.minor}'/'site-packages'
source=Path(sys.argv[1])
sys.path[:0]=[str(source),str(site)]
import swoop
from swoop import _adaptive, rpc

output: dict[str,Any]=dict(started_at=datetime.now(timezone.utc).isoformat(),rows=[],
    control='first primary dispatch raises synthetic TimeoutError; provider requests unmodified',
    package_sha256=hashlib.sha256(b''.join(p.read_bytes() for p in sorted((source/'swoop').glob('*.py')))).hexdigest())
path=Path(sys.argv[2])
original=rpc._get_transport_client
failed=[False]
dispatch=[]
class Controlled:
    def __init__(self,client):
        self.client=client
    def post(self,*args,**kwargs):
        dispatch.append('primary')
        if not failed[0]:
            failed[0]=True
            raise TimeoutError('controlled local timeout before provider dispatch')
        return self.client.post(*args,**kwargs)
def factory(transport):
    client=original(transport)
    return Controlled(client) if transport.address_family=='ipv6' else client
rpc._get_transport_client=factory

def run(label):
    recorder=swoop.DiagnosticsRecorder()
    tc=swoop.TransportConfig(address_family='ipv6',fallback_proxies=('socks5h://swoop-unified-probe:x@10.0.0.10:1082',),
        timeout=12,request_timeout=6,country='US',currency='USD',diagnostics=recorder)
    started=time.perf_counter()
    row: dict[str,Any]=dict(operation=label)
    try:
        days=swoop.get_calendar('JFK','LAX','2026-11-01','2026-11-30',exclude_separate_tickets=True,transport=tc)
        row.update(success=len(days.days)>0,count=len(days.days))
    except Exception as error:
        row.update(success=False,error_type=type(error).__name__)
    row.update(elapsed_ms=(time.perf_counter()-started)*1000,primary_dispatches=len(dispatch),
        diagnostics=dataclasses.asdict(recorder.snapshot()),health=_adaptive.health())
    output['rows'].append(row)
    path.write_text(json.dumps(output,indent=2)+'\n')
    print(json.dumps(row),flush=True)
run('controlled_failure_real_mia1')
run('cooldown_real_mia1')
wait=max(h['retry_in'] for h in _adaptive.health() if h['route']=='ipv6')+.1
output['actual_cooldown_wait_seconds']=wait
time.sleep(wait)
run('native_recovery')
output['finished_at']=datetime.now(timezone.utc).isoformat()
path.write_text(json.dumps(output,indent=2)+'\n')
