# Production acceptance, latency and refusal diagnosis — 2026-10-31

Production tested: `events-1f1e4b0be589`, sealed SDK
`82bd0e1b9e9d0e41556d34f333e36d09a76ee712`, native primary `wreq==0.13.0`.
All provider work ran under `ndc-cert-call run` following a successful check.
No production configuration, deadlines or SDK source changed during these probes.
Queries used JFK–LAX, 2026-11-30 shopping, 2026-11-01..30 calendars, one adult,
economy including Basic, US POS, USD and single-ticket exclusion.

## Functional acceptance and latency

[The corrected production API run](production-api-benchmark.json) passed all
21 checks: five each of one-way calendar, five-night round-trip calendar and
shopping; then calendar grid, calendar graph, round-trip shopping, continuation,
confirmed selector pricing and selector details. Selectors retained POS/currency
and single-ticket authority. Pricing confirmed USD 283 with three booking options
and `is_estimate=false`. All six additional functional calls returned HTTP 200.
These are read-only shopping/pricing calls; no booking/payment mutation occurred.

| Production operation | Success | Median | Worst observed |
| --- | ---: | ---: | ---: |
| One-way calendar, 30 dates | 5/5 | 174.5 ms | 8,141.6 ms |
| Five-night round-trip calendar, 30 dates | 5/5 | 184.6 ms | 271.2 ms |
| One-way shopping | 5/5 | 399.2 ms | 3,182.6 ms |

The sample is small; the worst observation is not an SLA or a reliable tail
percentile. One-off grid/graph calls took 7.29/8.21 seconds, round-trip shopping
371 ms, continuation 1.14 seconds, confirmed pricing 4.93 seconds and details
1.82 seconds. Native fallback/probes can dominate the slower calls.

The slowest one-way picker spent 7,803.6 ms obtaining an HTTP 302 on direct,
then 322.4 ms obtaining a successful fallback. Its total measured network time
was 8,126.0 ms and reported decode time 4.6 ms. The slowest shopping call spent
2,667.7 ms on a direct timeout, then 364.2 ms on successful fallback; decode time
was 16.0 ms. These observations locate the large delays in the HTTP attempts,
rather than Python result decoding.

The [initial combined run](production-benchmark.json) retains one benchmark-
client BrokenPipeError: its localhost keepalive connection had gone idle while
raw-client tests ran. No service response was received for that row. The script
now reconnects before API measurements. The fresh-connection API rerun above
supersedes that row for production availability; the original evidence remains.

## Matched actual-client comparison

The raw part of the initial run used identical SDK-generated URL, body bytes and
explicit headers, with request hashes recorded. Direct and mia1 were tested with
the same native client/profile. Actual deployed fallback primp was also measured
on the same mia1 identity. Each operation/client had one new client and subsequent
pooled calls, with alternating route order. Two consecutive failures stopped a
route, and HTTP 429 would stop that route immediately. No hidden retries or page
fallbacks were added to the raw RPC benchmark.

| Raw operation | Native direct usable | Native mia1 usable | primp mia1 usable | primp mia1 pooled median, successful |
| --- | ---: | ---: | ---: | ---: |
| One-way calendar | 0/2 | 4/5 | 4/5 | 120.6 ms |
| Round-trip calendar | 0/2 | 4/5 | 5/5 | 134.4 ms |
| Shopping | 2/5 | 5/5 | 5/5 | 360.4 ms |

Successful matched calendar date/return/price/currency values agreed. HTTP 200
with compact gRPC 13 was counted as failure, not a usable result. Successful
native direct shopping replies identified HTTP/2 and an IPv6 Google peer; the
second success reused the same local connection. The present sample does not
support a native-direct speedup claim. Native DNS/connect/TLS/TTFB phases remain
unknown. Full measurements and failures are retained in the JSON.

## Cause investigation: egress-dependent RPC refusal

[The route/protocol probe](throttle-probe.json) used identical fresh curl
requests and the same SDK payload, three spaced rounds, no redirects or retries,
a six-second maximum, and one fixed Google IPv6 DNS override for the IPv6 paths.
The SOCKS path used local resolution of that override. This curl version did not
emit the expected SOCKS destination trace marker, so the JSON records that
independent wire confirmation as false; it is not upgraded to a verified peer
observation. A [separate connectivity control](socks-target-control.json) retained
the configured cached IPv6 resolution and successful proxy HTTP 204.

| Source route / protocol | Usable shopping | Actual response |
| --- | ---: | --- |
| vin1 IPv6 / HTTP/2 | 0/3 | Three HTTP 200 compact gRPC 13 refusals |
| vin1 IPv4 / HTTP/2 | 0/3 | Three HTTP 200 compact gRPC 13 refusals |
| vin1 IPv6 / HTTP/1.1 | 0/3 | Two compact gRPC 13 refusals and one 6-second timeout |
| mia1 IPv6 / HTTP/2 | 3/3 | 122 itineraries on each call |

Direct IPv6 connectivity itself worked: Google `/generate_204` returned 204 in
22.5 ms with TLS complete at 18.8 ms. The Flights page returned HTTP 200, about
2 MB, in 238.6 ms with TLS complete at 22.6 ms. The RPC refusals followed the
vin1 egress in this sample across address families and HTTP versions. The same
query remained usable through mia1.

[An eight-call client/profile comparison](native-client-compare.json) separately
observed timeouts across fresh native Chrome137, Chrome146, default-client and
curl calls to one fixed Google IPv6 destination. A curl timeout occurred despite
TCP and TLS completing in 3.6/36.1 ms. Native finer-grained handshake timings were
not available and were not inferred from curl.

[Two longer native diagnostics](refusal-probe.json), with an isolated 12-second
budget and unchanged production configuration, then received compact gRPC 13
within 67.1 ms for one-way calendar and 95.0 ms for shopping. Both used an actual
IPv6 peer (`2001:4860:482b:7700::`) and reused the same native HTTP/2 connection.
Increasing the diagnostic timeout did not restore inventory.

The supported conclusion is **Google RPC failure/refusal correlated with the
vin1 egress**. IP/ASN reputation, session trust or rate-based gating are plausible;
a route-dependent Google backend problem is also possible. We observed no HTTP
429, Retry-After, `/sorry` redirect target or explicit unusual-traffic message in
these diagnostic responses, so these results do not prove an IP ban or a
particular rate limit. A compact INTERNAL response remains a refusal with an
unknown upstream cause. Cooldowns and mia1 fallback serve the application while
that direct route is unhealthy; repeatedly increasing timeouts would not address
the observed fast refusals. Probe failures still explain the multi-second tails.

## Focused Deals/Explore adoption correction

The parent audit identified a separate real bug in the live wrapper: native mode
admitted Deals even though its SDK session path ignores native-family/adaptive
settings. Explore has the analogous path. The fresh consumer correction is
`5cb6b48c4bb8d9e1397f189d4448252b7d83ba5a` on
`Shimon/swoop-native-supported-rpc-20261031`. It admits only audited native RPC
entry points; unsupported/new functions keep explicit proxy/session/failover.
No main API caller contract or SDK pin changes are required.

Regression tests first failed on the prior wrapper and then passed. All 151
consumer tests pass, including actual SDK Deals/Explore GET+POST and identical-
request failover with only the HTTP edge replaced. [The candidate audit on vin1](entrypoints-candidate-audit.json)
loaded the exact candidate wrapper with the actual live sealed SDK and verified
nonempty proxy selection for Deals/Explore, plus native family/deadline/cancel
selection for shopping, with zero HTTP calls. Candidate source bytes are hashed
in the audit. This audit does not install the candidate into production.

The promotion ledger accepted `swoop-native-entrypoints-20261031`; prepared
receipt `8e02ac45-6445-4be5-b619-92dfb00e349a`. The active promotion owner retains
CI, release and final live acceptance. The prepared SHA is unchanged; latency
investigation scripts/results are separate SDK documentation artifacts. At this
report's checkpoint the correction was queued for coordinator CI, not yet verified live.

SDK `make check` remains clean: 2240 passed, 54 skipped, 18 live deselected,
pyright zero errors. The original deployed consumer suite passed 148 tests;
the fresh correction passed 151. No additional production defect was established
by the latency probes, and no new runtime fix was mixed into the prepared SHA.
