"""Zero-provider-call candidate wrapper audit against vin1's sealed SDK."""
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sys

root=Path('/srv/ndcterm/current/ndcterm').resolve()
app=root/'apps/swoop-service'
site=app/'.venv/lib/python3.13/site-packages'
sys.path[:0]=[str(site),str(app)]
import swoop
from swoop import _deals, _explore
loaded_swoop_commit=importlib.import_module("runtime").loaded_swoop_commit
candidate=Path('/tmp/swoop-entrypoints-candidate-main.py')
spec=importlib.util.spec_from_file_location('candidate_main',candidate)
assert spec and spec.loader
module=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=module
spec.loader.exec_module(module)
assert module.DIRECT_IPV6_PRIMARY
class Captured(BaseException):
    pass
rows=[]
expected='socks5h://audit-expected:x@10.0.0.10:1082'
for function,edge in ((swoop.deals,_deals),(swoop.explore,_explore)):
    observed=[]
    original=edge._get_client
    def capture(proxy,impersonate=None):
        observed.append(proxy)
        raise Captured()
    setattr(edge,"_get_client",capture)
    try:
        try:
            module._call_with_proxy_failover(function,expected,'JFK')
        except Captured:
            pass
    finally:
        setattr(edge,"_get_client",original)
    assert observed==[expected], (function.__name__,observed)
    rows.append(dict(operation=function.__name__,explicit_proxy_preserved=True,native_admitted=module._native_rpc_supported(function),http_calls=0))
observed=[]
original=swoop.rpc._get_transport_client
def capture_native(transport):
    observed.append(dict(address_family=transport.address_family,proxy_is_none=transport.proxy is None,
                         operation_deadline=transport._operation_deadline is not None,cancellation_check=transport._cancel_check is not None))
    raise Captured()
swoop.rpc._get_transport_client=capture_native
try:
    try:
        module._call_with_proxy_failover(swoop.search_raw,expected,'JFK','LAX','2026-11-30')
    except Captured:
        pass
finally:
    swoop.rpc._get_transport_client=original
assert len(observed)==1 and observed[0]==dict(address_family='ipv6',proxy_is_none=True,operation_deadline=True,cancellation_check=True)
rows.append(dict(operation='search_raw',native_admitted=True,observed=observed,http_calls=0))
result=dict(release=str(root),sdk_commit=loaded_swoop_commit(swoop),
            candidate_sha='5cb6b48c4bb8d9e1397f189d4448252b7d83ba5a',
            candidate_main_sha256=hashlib.sha256(candidate.read_bytes()).hexdigest(),http_calls=0,passed=True,rows=rows)
Path('/tmp/swoop-entrypoints-candidate-audit.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result))
