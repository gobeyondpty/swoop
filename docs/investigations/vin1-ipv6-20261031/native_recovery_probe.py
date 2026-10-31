"""Bounded vin1 recovery check using real cooldowns; run under ndc-cert-call."""
import dataclasses
import json
import pathlib
import sys
import time
from typing import Any

stage = pathlib.Path('/tmp/swoop-vin1-native-stage')
release = pathlib.Path('/srv/ndcterm/current/ndcterm').resolve()
sys.path[:0] = [str(stage), str(release/'apps/swoop-service/.venv/lib/python3.13/site-packages')]
import swoop
from swoop import rpc, _adaptive

# Two stable, preconfigured mia1 paths; no identities are minted during retry.
transport = swoop.TransportConfig(address_family='ipv6', country='US', currency='USD', timeout=8,
    retries=0, fallback_proxies=('socks5h://swoop-native-recovery-20261031:x@10.0.0.10:1082', 'socks5h://10.0.0.10:1080'))
original = rpc._get_transport_client
class Refused:
    def post(self, *args, **kwargs):
        return type('Response', (), dict(status_code=403, text='', headers={}))()
rows=[]
def sample(label):
    recorder=swoop.DiagnosticsRecorder()
    started=time.monotonic()
    row: dict[str, Any]
    try:
        result=swoop.search_raw('JFK', 'LAX', '2026-11-30', transport=dataclasses.replace(transport,diagnostics=recorder))
        row=dict(label=label,count=len(result.best)+len(result.other) if result else 0)
    except Exception as error:
        row=dict(label=label,error=type(error).__name__)
    row.update(elapsed_ms=(time.monotonic()-started)*1000,health=_adaptive.health(),diagnostics=dataclasses.asdict(recorder.snapshot()))
    rows.append(row)
    print(json.dumps(row),flush=True)

rpc._get_transport_client=lambda tc: Refused() if tc.proxy is None else original(tc)
try:
    sample('injected_primary_403_actual_mia1')
finally:
    rpc._get_transport_client=original
sample('cooldown_actual_mia1')
for index in range(3):
    time.sleep(31)
    sample(f'natural_half_open_{index+1}')
    if any(r['route']=='ipv6' and r['state']=='closed' for r in _adaptive.health()):
        break
summary=dict(release=str(release),native_client='wreq==0.13.0',rows=rows,
             fault='one injected direct HTTP403; all fallback and recovery HTTP calls real; natural 31-second cooldowns',
             direct_recovered=any(r['route']=='ipv6' and r['state']=='closed' for r in _adaptive.health()))
pathlib.Path('/tmp/swoop-native-recovery.json').write_text(json.dumps(summary,indent=2))
print(json.dumps({'complete':True,'direct_recovered':summary['direct_recovered']}),flush=True)
