# Native vin1 IPv6 primary, mia1 fallback, and HTTP latency

## Owner request and implementation ownership

User request, 2026-10-31:

> Handoff to swoop project to use ipv6 from vin1 and mia1 as fallback if rpc gets blocked in a smart way while we investigate if there is anything we can do to speedup the http time?

Take implementation ownership in this Swoop task. Implement and test native
IPv6 transport and adaptive fallback, prepare the necessary NDC Terminal
consumer/configuration changes, and coordinate rollout through its existing
promotion owner. This is an implementation handoff, not a request to stop after
writing a proposal. The original user-facing calendar fixes are a separate task.

Repo: `/home/shimon/swoop`, fork `github.com/gobeyondpty/swoop`.
Task checkout: `/home/shimon/orca/workspaces/swoop/vin1-ipv6-primary-20261031`.
Branch: `gobeyondpty/vin1-ipv6-primary-20261031`, based on current `fork/main`
`8da7475cf222c5577e20c5bcb22f90be81d224fb` (same SDK as live vin1).
Parent terminal: `term_a5969eed-e09d-45a7-a227-21d728187823`.
Read this repo's AGENTS.md and the NDC Terminal AGENTS.md before consumer work.

## What is already proved

- Production is physical **vin1**: mesh `10.0.0.17`, public `51.81.56.55`.
  SSH: `ssh -o BatchMode=yes 10.0.0.17` from air.
- Live root: `/srv/ndcterm/current/ndcterm`. Our probe observed release
  `events-402fd6540676`; recheck before any rollout.
- Load its sealed SDK with native `python3 -I -S`, adding the release-local
  `apps/swoop-service/.venv/lib/python3.13/site-packages` to sys.path. The release
  need not contain a `.venv/bin/python` executable.
- Native, unmodified deployed Swoop/primp with `proxy=None` and proxy environment
  variables cleared succeeded directly from vin1 for one-way calendar, round-trip
  calendar, and shopping. A syscall-traced repeat used **IPv4**, not IPv6.
- Forced **IPv6** also succeeds directly from vin1. We forced this with curl's
  HTTP transport while retaining the deployed SDK's request builder, filters,
  headers, error handling and response decoder. This **does not prove primp is
  currently configurable for IPv6**. Its Python Client signature exposed no
  address-family/local-address option in our inspection. Solve native transport
  properly; the curl adapter is a diagnostic, not a shipped replacement.
- IPv6 source observed: `2604:2dc0:100:2437::17`; Google destinations included
  `2001:4860:4828:7700::` and `2001:4860:4826:7700::`. `strace` confirmed AF_INET6.
- Each successful direct IPv6 probe used one RPC and zero HTML fallbacks.
  Calendar returned 30 prices; shopping returned 131 itineraries.
- The managed wrapper currently forces mia1: `apps/swoop-service/proxies_prod.py`
  has an eight-identity IPv6 pool at `10.0.0.10:1082`, with static SOCKS fallbacks
  at 1080/1081. `IdentityPool.next()` round-robins; SDK HTTP clients cache per
  proxy/impersonation identity.

## Matched vin1 benchmark (attached)

See `vin1_benchmark.py` and `vin1-benchmark.json` in this directory. Both paths
use the same curl HTTP/2 client, exact SDK-generated payload and headers, US POS,
USD, one adult, economy including Basic, and separate-ticket exclusion.
Direct is forced IPv6; mia1 uses its IPv6-only per-identity SOCKS exit.
Each path/operation has one fresh connection and four explicitly reused
connections (`num_connects=0`). All 20 responses were parsed as usable results;
all returned calendar prices matched. This is a small sample, not a latency SLA.

| HTTP operation | Direct IPv6, reused mean | mia1, reused mean | Reduction |
| --- | ---: | ---: | ---: |
| JFK-LAX, 30-date five-night calendar | 116.097 ms | 144.638 ms | 28.541 ms / 19.7% |
| JFK-LAX shopping, 2026-11-30 | 261.040 ms | 339.567 ms | 78.527 ms / 23.1% |

Fresh calendar connection: 202.201 ms direct vs 289.335 ms via mia1.
Fresh shopping connection: 241.167 ms direct vs 582.419 ms via mia1.
TLS-complete elapsed time on these fresh requests: about 19-25 ms direct vs
143-147 ms via mia1. TTFB includes Google's processing; do not label it all network.

Earlier air measurements of 510 ms Swoop vs 177 ms browser were **not a fair
steady-state comparison**: fare data was warm, but service connections were cold.
Eight calls through eight identities averaged 544 ms on the first pass, then
184 ms on the second. Original private diagnostics showed ~501 ms network,
~0.95 ms decoding, ~8 ms wrapper overhead, one RPC and no retries. Connection
reuse matters much more than Python parsing. Do not repeat the earlier 2.9x
figure as an intrinsic library overhead.

## Required behavior

1. Prefer native direct IPv6 on vin1, with explicit, verified address-family
   control, correct hostname/SNI/certificate verification, pooled connections,
   and unchanged request semantics. Verify AF_INET6 live using the actual client.
2. Keep mia1 as a healthy fallback when direct RPC is refused or unavailable.
   Classify explicit HTTP 403/429, compact gRPC 13, network failure and timeout
   separately; compact INTERNAL is a retryable refusal, not proof of an IP ban.
   An HTTP 200 alone does not prove success. A valid no-fare result or gRPC 3
   invalid query must not automatically mark a route blocked or trigger loops.
   If an empty body is ambiguous, only a bounded identical-query comparison on a
   healthy exit can establish that the direct route hid inventory.
3. Use bounded route health / circuit breaking: avoid retrying a known-bad direct
   route for every call; respect Retry-After where available, retain one shared
   monotonic operation deadline, and allow one controlled half-open probe after
   cooldown so direct IPv6 recovers. Bound attempts, concurrency and state; keep
   availability failures distinct from empty inventory and detailed outages.
4. Preserve the exact party, cabin, POS/currency, bags, airline/connection filters,
   single-ticket policy, selector lineage and original deadline on failover.
   No tenant/request state on mutable shared client fields; no route decisions
   based on user-supplied routing authority.
5. Measure/improve cold connection setup, pooling/idle behavior, unnecessary
   identity rotation, HTTP/2 reuse, DNS and response read costs. Keep phase values
   unknown if the actual client cannot expose them. Do not infer a SOCKS-handshake
   failure from primp's generic `socks connect timeout` label alone.
6. Add meaningful offline regressions for family selection, fallback classification,
   valid empty results, malformed/blocked responses, cooldown/recovery, deadlines,
   cancellation and concurrent callers. Respect frozen API surface tests. Run
   `make check` per AGENTS.md and targeted consumer tests for wrapper changes.
7. Prepare a tested immutable SDK commit/pin and the necessary NDC Terminal
   wrapper/config adoption in isolated worktrees. Consult the live coordinator
   lease before NDC integration/release; no parallel production release. Do not
   change production config to an IPv6 option whose deployed client lacks it.
   Verify live health identity, AF_INET6 primary, actual mia1 failover and recovery.

## Grounding and integration references

Swoop: `swoop/rpc.py`, `_transport_budget.py`, `models.py` (TransportConfig),
`diagnostics.py`, `_calendar.py`, `exceptions.py`, `tests/test_api_surface.py`.

NDC Terminal: `apps/swoop-service/main.py`, `proxies_prod.py`,
`TRANSPORT_DIAGNOSTICS.md`, `test_main.py`, `test_request_cancellation_3100.py`,
`docs/SWOOP_TRANSPORT.md`, `references/google-flights/FINDINGS.md`,
`docs/PRODUCTION_DEPLOY.md`, `docs/PROMOTION_COORDINATOR_WORKFLOW.md`.

Infra checkout `/home/shimon/ansible` contains provider configuration source:
`ansible/playbooks/update-ndcterm-egress.yml` targets current sys1/air, while old
PM2/deploy-ndcterm-apps roles are retired and MUST NOT deploy production.
The IPv6 exit implementation is
`ansible/roles/ipv6_exit_pool/files/ipv6-exit-pool.py`.

Before unattended provider traffic run `ndc-cert-call check` and wrap the entire
worker/script in `ndc-cert-call run -- ...`. Searches need explicit dates. These
are read-only shopping probes; no airline booking/payment mutations are needed.

Report ownership acceptance, implementation progress and final exact SHAs to the
parent and, for consumer rollout, the live NDC promotion owner. Do not stop at an
untested config change or silently replace native primp with the benchmark adapter.
