"""Read-only matched Google RPC benchmark: native vin1 IPv6 versus mia1 IPv6 exit."""
import dataclasses
import json
import os
import pathlib
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import uuid

root = pathlib.Path('/srv/ndcterm/current/ndcterm').resolve()
site = root / 'apps/swoop-service/.venv/lib' / f'python{sys.version_info.major}.{sys.version_info.minor}' / 'site-packages'
for name in ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy']:
    os.environ.pop(name, None)
sys.path.insert(0, str(site))
import swoop.rpc
from swoop import get_calendar, search_raw, TransportConfig, SORT_CHEAPEST
from swoop._calendar import _parse_calendar_days

captured = {}
class Capture:
    def post(self, url, content, headers, **kwargs):
        captured.update(url=url, content=content, headers=headers)
        raise RuntimeError('Capture only; dispatch through matched curl transports below')

original_client = swoop.rpc._get_client
swoop.rpc._get_client = lambda *args, **kwargs: Capture()
requests = {}
for operation in ['calendar', 'shopping']:
    captured.clear()
    try:
        transport = TransportConfig(proxy=None, country='US', currency='USD', timeout=8, retries=0)
        if operation == 'calendar':
            get_calendar('JFK', 'LAX', '2026-11-01', '2026-11-30', min_stay=5, max_stay=5,
                         exclude_separate_tickets=True, transport=transport)
        else:
            search_raw('JFK', 'LAX', '2026-11-30', sort=SORT_CHEAPEST,
                       exclude_separate_tickets=True, transport=transport)
    except Exception:
        if not captured:
            raise
    requests[operation] = dict(captured)
swoop.rpc._get_client = original_client

results = []
with tempfile.TemporaryDirectory(prefix='swoop-ipv6-bench-') as folder:
    directory = pathlib.Path(folder)
    for operation, request in requests.items():
        body_file = directory / f'{operation}.req'
        body_file.write_bytes(request['content'])
        # Opposite ordering for the second operation reduces simple ordering bias.
        paths = ['vin1-direct-ipv6', 'via-mia1-ipv6'] if operation == 'calendar' else ['via-mia1-ipv6', 'vin1-direct-ipv6']
        for route in paths:
            proxy = f'socks5h://bench-{uuid.uuid4().hex[:12]}:x@10.0.0.10:1082'
            command = ['curl']
            files = []
            for sample in range(5):
                if sample:
                    command += ['--next']
                output = directory / f'{operation}-{route}-{sample}.body'
                files.append(output)
                command += ['--silent', '--show-error', '--connect-timeout', '5', '--max-time', '8',
                            '--request', 'POST', '--data-binary', '@' + str(body_file),
                            '--output', str(output), '--write-out', 'METRICS%{json}\n']
                if route == 'vin1-direct-ipv6':
                    command += ['--ipv6', '--noproxy', '*']
                else:
                    command += ['--proxy', proxy, '--noproxy', '']
                for key, value in request['headers'].items():
                    command += ['--header', f'{key}: {value}']
                command.append(request['url'])
            process = subprocess.run(command, capture_output=True, text=True, timeout=50)
            metrics = [json.loads(line.removeprefix('METRICS')) for line in process.stdout.splitlines() if line.startswith('METRICS')]
            for sample, metric in enumerate(metrics):
                body = files[sample].read_text() if files[sample].exists() else ''
                row = {key: metric.get(key) for key in ['http_code', 'remote_ip', 'local_ip', 'http_version', 'num_connects', 'exitcode']}
                row.update(operation=operation, route=route, sample=sample,
                           **{key + '_ms': round(metric.get(key, 0) * 1000, 3) for key in ['time_namelookup', 'time_connect', 'time_appconnect', 'time_pretransfer', 'time_starttransfer', 'time_total']})
                if route == 'vin1-direct-ipv6' and ':' not in str(metric.get('remote_ip')):
                    raise RuntimeError('Direct request did not use IPv6')
                try:
                    if operation == 'calendar':
                        days = _parse_calendar_days(body)
                        row['pricedCells'] = len(days)
                        row['prices'] = [{'date': day.departure_date, 'returnDate': day.return_date, 'amount': day.price, 'currency': day.currency} for day in days]
                    else:
                        result = swoop.rpc._parse_rpc_response(body)
                        row['itineraries'] = len(result.best) + len(result.other) if result else 0
                    row['validResult'] = row.get('pricedCells', row.get('itineraries', 0)) > 0
                except Exception as error:
                    row.update(validResult=False, errorType=type(error).__name__, grpcCode=getattr(error, 'grpc_code', None))
                results.append(row)
                print(json.dumps({'progress': {key: value for key, value in row.items() if key != 'prices'}}), flush=True)

summary = []
for operation in requests:
    rows = [row for row in results if row['operation'] == operation]
    entry = {'operation': operation}
    for route in ['vin1-direct-ipv6', 'via-mia1-ipv6']:
        selected = [row for row in rows if row['route'] == route]
        warm = [row['time_total_ms'] for row in selected if row['num_connects'] == 0 and row['validResult']]
        entry[route] = {'valid': sum(row['validResult'] for row in selected), 'samples': len(selected),
                        'freshConnectionMs': selected[0]['time_total_ms'] if selected else None,
                        'reusedConnectionSamples': len(warm),
                        'reusedConnectionMeanMs': round(statistics.mean(warm), 3) if warm else None,
                        'reusedConnectionMedianMs': round(statistics.median(warm), 3) if warm else None}
    direct = entry['vin1-direct-ipv6']['reusedConnectionMeanMs']
    proxy = entry['via-mia1-ipv6']['reusedConnectionMeanMs']
    if direct is not None and proxy:
        entry.update(warmSavedMs=round(proxy-direct, 3), warmReductionPercent=round((proxy-direct)/proxy*100, 1))
    if operation == 'calendar':
        signatures = [sorted((day['date'], day['returnDate'], day['amount'], day['currency']) for day in row['prices']) for row in rows if row['validResult']]
        entry['allReturnedPricesMatch'] = bool(signatures) and all(signature == signatures[0] for signature in signatures)
    summary.append(entry)
print(json.dumps({'final': {'host': socket.gethostname(), 'release': root.parent.name, 'client': 'curl, same deployed SDK payloads and decoding on both routes', 'date': '2026-10-31', 'summary': summary, 'results': results}}), flush=True)
