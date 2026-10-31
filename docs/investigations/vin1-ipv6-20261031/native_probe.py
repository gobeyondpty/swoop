"""Run on vin1 under ndc-cert-call, staging only; never edits the release."""
import dataclasses
import json
import pathlib
import sys
import time

stage = pathlib.Path('/tmp/swoop-vin1-native-stage')
release = pathlib.Path('/srv/ndcterm/current/ndcterm').resolve()
site = release / 'apps/swoop-service/.venv/lib/python3.13/site-packages'
sys.path[:0] = [str(stage), str(site)]
import swoop
import swoop.rpc as rpc
from swoop import DiagnosticsRecorder, TransportConfig
from swoop import _adaptive

# One stable identity for the entire matched run: cold once, then reuse.
proxy = 'socks5h://swoop-native-validation-20261031:x@10.0.0.10:1082'
base = TransportConfig(address_family='ipv6', country='US', currency='USD', retries=0, timeout=8)
rows = []
calendar_variants = []
for operation in ('calendar', 'shopping'):
    prices = []
    for route in ('direct', 'mia1'):
        for sample in range(2):
            recorder = DiagnosticsRecorder()
            transport = dataclasses.replace(base, proxy=proxy if route == 'mia1' else None, diagnostics=recorder)
            started = time.perf_counter()
            try:
                if operation == 'calendar':
                    result = swoop.get_calendar('JFK', 'LAX', '2026-11-01', '2026-11-30', min_stay=5, max_stay=5,
                                                exclude_separate_tickets=True, transport=transport)
                    count = len(result.days)
                    prices.append([{key: value for key, value in dataclasses.asdict(day).items() if key != "selector"} for day in result.days])
                    calendar_variants.append(prices[-1])
                else:
                    result = swoop.search_raw('JFK', 'LAX', '2026-11-30', sort=swoop.SORT_CHEAPEST,
                                              exclude_separate_tickets=True, transport=transport)
                    count = len(result.best) + len(result.other) if result else 0
                row = dict(operation=operation, route=route, sample=sample, count=count,
                           elapsed_ms=(time.perf_counter()-started)*1000,
                           diagnostics=dataclasses.asdict(recorder.snapshot()))
                assert count > 0
            except Exception as error:
                row = dict(operation=operation, route=route, sample=sample, error=type(error).__name__, cause=type(error.__cause__).__name__, message=str(error))
            rows.append(row)
            print(json.dumps(row), flush=True)
    # Preserve any fare changes as evidence; live prices are not immutable.

# Controlled real failover/recovery without breaking shared networking: inject one
# direct HTTP refusal at dispatch; fallback and recovery use the actual native client.
original = rpc._get_transport_client
class Refused:
    def post(self, *args, **kwargs):
        return type('Response', (), dict(status_code=403, text='', headers={}))()
rpc._get_transport_client = lambda tc: Refused() if tc.proxy is None else original(tc)
transport = dataclasses.replace(base, fallback_proxies=(proxy, "socks5h://10.0.0.10:1080"))
failover = {}
try:
    first = swoop.search_raw('JFK', 'LAX', '2026-11-30', transport=transport)
    failover['actual_mia1_success'] = bool(first and first.best + first.other)
except Exception as error:
    failover['error'] = type(error).__name__
finally:
    rpc._get_transport_client = original
failover['after_refusal'] = _adaptive.health()
try:
    second = swoop.search_raw('JFK', 'LAX', '2026-11-30', transport=transport)
    failover['cooldown_mia1_success'] = bool(second and second.best + second.other)
except Exception as error:
    failover['cooldown_error'] = type(error).__name__
# Expire only this isolated process's cooldown, then one real native direct probe.
with _adaptive._lock:
    for route in _adaptive._routes.values():
        route.until = 0
try:
    third = swoop.search_raw('JFK', 'LAX', '2026-11-30', transport=transport)
    failover['after_probe_success'] = bool(third and third.best + third.other)
except Exception as error:
    failover['probe_error'] = type(error).__name__
failover['after_probe'] = _adaptive.health()
failover['direct_recovered'] = any(r['route'] == 'ipv6' and r['state'] == 'closed' for r in failover['after_probe'])
# One-way calendar uses the same real native client.
try:
    one_way = swoop.get_calendar('JFK', 'LAX', '2026-11-01', '2026-11-30', transport=base)
    failover['one_way_calendar_prices'] = len(one_way.days)
except Exception as error:
    failover['one_way_calendar_error'] = type(error).__name__
summary = dict(release=str(release), native_client='wreq==0.13.0', profile='Chrome137', results=rows,
               failover=failover,
               fault='injected direct HTTP403 only; fallback and recovery use actual clients; isolated cooldown clock advanced once',
               network_phases={'dns_ms':None, 'connect_ms':None, 'tls_ms':None, 'ttfb_ms':None},
               health=_adaptive.health(),
               successful_calendars_match=all(p == calendar_variants[0] for p in calendar_variants) if calendar_variants else None,
               calendar_price_variants=calendar_variants)
pathlib.Path('/tmp/swoop-native-vin1-results.json').write_text(json.dumps(summary, indent=2))
print(json.dumps({'complete': True, 'failover': summary['failover']}), flush=True)
