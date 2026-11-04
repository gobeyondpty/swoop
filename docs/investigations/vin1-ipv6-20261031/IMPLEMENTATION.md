# Implementation and acceptance evidence

Owner: Codex, terminal `term_8cf8119f-f55c-479e-b466-ef8476159773`.
SDK checkout: `/home/shimon/orca/workspaces/swoop/vin1-ipv6-primary-20261031`.
Consumer checkout: `/home/shimon/orca/workspaces/ndcterm/swoop-vin1-ipv6-20261031`.
SDK immutable runtime pin: `82bd0e1b9e9d0e41556d34f333e36d09a76ee712`.
SDK behavior contract: [NATIVE_TRANSPORT.md](../../NATIVE_TRANSPORT.md).

The native implementation explicitly uses optional `wreq==0.13.0` for direct
family-controlled connections. Existing/default transport and mia1 failover use
primp. No curl adapter is shipped. wreq resolves IPv6 only, binds `::`, preserves
hostnames/SNI and certificate validation, and retains pooled connections. The
consumer initializes its native client at startup, uses a sticky mia1 identity,
and supplies an enclosing monotonic deadline plus a captured disconnect event.

An adaptive RPC attempts direct plus at most two configured fallback exits,
replaying the exact encoded body/headers/URL. Typed validation runs before a
route is considered healthy. Refusals, malformed replies, timeouts and network
failures have separate health reasons. Valid empty inventory and invalid queries
do not trigger failover. One bounded identical-query comparison can establish
hidden inventory for an ambiguous empty body. Circuit state/concurrency,
Retry-After and half-open recovery are bounded and covered offline.

## Actual vin1 client evidence

All provider scripts ran under `ndc-cert-call run -- ...` after a successful
`ndc-cert-call check`. Scripts used a staging directory in `/tmp`; they did not
replace or configure the live release, which was
`/srv/ndcterm/releases/events-402fd6540676/ndcterm` during these probes.

[The native benchmark](native-results.json) used the same native client and SDK
request builders on both direct and mia1 paths. Direct produced 30 round-trip
calendar prices and 128 shopping itineraries. Successful calendar date/return
price/currency values matched across exits. Opaque selectors were excluded from
comparison. The small native sample includes direct refusal/timeout results;
it does **not** establish a reliable native steady-state latency improvement.
The earlier matched curl benchmark remains a diagnostic baseline only. Native
DNS/connect/TLS/TTFB measurements are unavailable and are explicitly null.

[The natural recovery run](native-recovery.json) observed:

| Step | Actual result | Route state |
| --- | --- | --- |
| Inject one direct 403; actual mia1 RPC | 128 itineraries, 625 ms | direct open, fallback healthy |
| Next request during cooldown | 128 itineraries, 383 ms, one RPC | direct skipped; mia1 reused |
| First probe after 31 seconds | direct timeout; mia1 returned 128, 3025 ms total | direct remains open |
| Second probe after another 31 seconds | direct returned 128, 399 ms, one RPC | direct recovered |

[The recovery syscall trace](native-recovery-af-inet6.strace) records the actual
native client's AF_INET6 connection to `2001:4860:482a:7700::` from
`2604:2dc0:100:2437::17`, plus the actual SOCKS connection to mia1
`10.0.0.10:1082`. Only one Google TCP connection was created across the two
natural probes, demonstrating connection reuse after the timeout. Separate
actual-client inspection observed HTTP/2; protocol phase timing remains unknown.
The fault injection replaces only one direct response in an isolated process;
all fallback and subsequent recovery requests use the actual HTTP clients.

The one-way calendar attempt in the small native benchmark was refused with an
HTTP error; it is retained in the evidence rather than described as a successful
acceptance. Calendar one-way/round-trip payload semantics are unchanged and have
offline regressions. Production acceptance should exercise both after promotion.

## Validation

- Python 3.13: `make check`, pyright clean, 2240 passed, 54 skipped,
  18 live tests deselected.
- Python 3.10: all offline tests, 2236 passed, 58 skipped, 18 deselected.
  The optional native dependency is conditional on Python 3.11+; default primp
  remains supported on Python 3.10.
- Actual native IPv6 loopback regression verifies exact POST bytes/headers,
  fractional deadline handling, body-read timeout, and connection reuse.
- Circuit regressions cover classification, empty/malformed/blocked responses,
  half-open concurrency, stale responses, Retry-After preservation, bounded
  state, deadlines, cancellation, exact filtered RPC replay and two fallbacks.
- Consumer: 148 unittest tests pass against a canonical pip VCS install of the
  exact SDK pin, including sealed runtime/provenance verification and concurrent
  POS/selector/cancellation isolation.
- Five immutable supply/tamper/lifecycle tests pass. The new Git bundle contains
  only the pinned reachable history, checked tree/object inventory and license.

## Downstream ownership

The NDC promotion lease belongs to `tab-bound-brands-3090-20261031`, terminal
`term_5d5e5ca2-b922-47a8-86ed-d4481fae2619`. That owner retains integration,
build, release and final live-health acceptance. The consumer change enables
native primary only for vin1 (with operator overrides), preserves production
proxy files, exposes redacted circuit health and immutable identity, and keeps
rollback available through `SWOOP_DIRECT_IPV6_PRIMARY=false`.

No competing production release was started. Staged wire evidence is not proof
that the new consumer wrapper is already live. The READY packet must name the
exact consumer commit and request post-release health identity, AF_INET6,
actual mia1 failover/recovery, and both calendar forms.
