# Native IPv6 primary and shared route/session policy

On Python 3.11+, install `swoop-flights[native]` to enable `TransportConfig(address_family="ipv6")`.
The optional, pinned `wreq==0.13.0` Rust client supplies IPv6-only DNS and an IPv6
local bind. URLs keep their hostnames; SNI, certificate-chain and hostname checks
stay enabled. It uses Chrome137 for the generic `chrome` profile. Python 3.10 retains the default primp transport. Existing
`address_family="auto"` callers continue to use primp. This is an explicit native
backend, not the diagnostic curl adapter and not a claim that primp gained IPv6
configuration.

```python
transport = TransportConfig(
    address_family="ipv6", country="US", currency="USD", timeout=30, request_timeout=8,
    fallback_proxies=("socks5h://trusted-pool:1082", "socks5h://trusted-exit:1080"),
)
```

Only trusted application configuration should choose exits. Family-controlled
direct ignores global/environment proxies. Nonempty `fallback_proxies` enables
shared routing for shopping, booking, calendars, Deals, Explore and all three
Hotels paths. Public API signatures and result fields are unchanged. Consumer
adapters can check the SDK-owned `_session.supports_policy(fn)` marker; unknown
future entry points keep their legacy proxy path until admitted by the SDK.

The policy permits at most three route attempts: direct, then two distinct
configured primp exits. Flight RPCs replay the original URL, encoded bytes and
headers. Session endpoints rerun their endpoint builder with the original query
on the chosen route, obtaining that route's cookies and `bl`/`f.sid`/hotel context.
They never reuse primary-route session identifiers on a fallback. The selected
hotel/flight token, dates, party, POS, currency and filters remain unchanged.
Hotel follow-up builders retain caller currency and child ages over defaults in
returned context. Builders and parsers remain endpoint-specific.

`timeout` bounds a public operation under explicit family routing;
`request_timeout` optionally caps each HTTP send/read. The first direct proof is
capped at 2s for flight RPCs and 5s for session bootstrap plus first RPC (override
with `primary_timeout`). A parsed first hotel RPC releases that proof cap for
remaining session work. The original operation deadline and the attempt's share
of remaining time never move. Adaptive requests do not nest 429 retries or
shopping-page fallbacks. `_operation_deadline` and `_cancel_check` retain staged
and fan-out deadlines/cancellation. A running native call retains its timeout;
cancellation prevents subsequent dispatches and interrupts cache-lock waits.

HTTP 403, 429 (including Retry-After), redirects, 5xx, compact gRPC INTERNAL,
network failures, timeouts and malformed responses have separate health reasons.
A send/read cut short at our own proof cap, attempt share or operation deadline,
or by caller cancellation, still fails over but records no health and does not
extend a probe's cooldown: it is not evidence about the route.
Compact INTERNAL is an upstream refusal, not evidence of an IP ban. Detailed
gRPC errors and invalid arguments propagate immediately. Protocol-valid empty
inventory is healthy and does not trigger failover. An ambiguous body permits
one identical-query comparison on a non-cooling fallback; only parsed inventory
on that comparison marks the primary as hiding inventory. Unresolved ambiguous
bodies remain errors, rather than becoming a no-fare result.

Health is shared by Google transport identity across endpoints, never by tenant
or query. A failure opens that route for 30 seconds; repeated failed recovery
backs off to 60/120/240/300 seconds, with up to 10% positive jitter and longer
Retry-After honored.
One caller may probe it after cooldown; other callers use fallback. Late responses
cannot erase a newer failure. Active calls are capped at eight per route and state
at 64 routes; unexpired cooldowns are not evicted to admit more state. Health
snapshots from `swoop._adaptive.health()` contain no proxies, credentials or query
payloads. All cooling/capacity-limited routes produce a typed availability error.

Clients are cached in the existing bounded pool (32 identities). Session paths
keep up to two independently leased cookie jars per route/service/POS/currency,
so concurrent queries cannot overwrite cookies mid-operation. Explore params
use the jar identity, with bounded token (32) and lock (64) caches. Per-origin
Deals workers retain isolated jars. Local session capacity never marks an exit
unhealthy. GET redirects are limited to two same-origin HTTPS Travel hops.
Native connections have a
90-second idle timeout, four idle connections per host and a 16-connection pool
cap; HTTP/2 is negotiated normally. Initializing the native client at service
startup moves library initialization off the first request without provider
traffic. A sticky fallback identity retains its existing primp connection pool.
Native per-phase DNS/connect/TLS/TTFB measurements are unavailable and remain
unknown; the existing diagnostics measure complete send/read time, decoding and
actual HTTP dispatches. Do not infer a SOCKS handshake failure from a generic
transport exception's wording.

Validate live traffic under certification call-mode. `vin1_probe.py` in the
`docs/investigations/unified-native-20261031` directory is an isolated, read-only actual-client benchmark
and controlled failure/recovery probe; it never replaces production files.
AF_INET6 must be checked with the actual client's syscalls, not curl's. Google
can refuse a route after an earlier successful call: report failures as well as
successful latency samples, and do not treat a small sample as a latency SLA.

Hotels audit: all three paths use the shared policy. No proxy-first exception is
required. The live broad-search M0CRd follow-up returned gRPC 3 while AtySUc
returned valid hotels. This remains separate unresolved wire triage; it is not
not-applicable and does not justify a transport exception. The existing partial
seed-result behavior is preserved while the RPC rejection remains unresolved.
Invalid arguments do not open route health. Compact gRPC 13/availability failures
restart the session on fallback, including when an optional follow-up catches
the exception. See the investigation report and JSON for measured limits.

## Local source-address bank (opt-in)

`local_address` binds a particular already assigned native IPv6 source. For one
fresh address per public operation, configure the root-owned address broker:

```python
transport = TransportConfig(
    address_family="ipv6", timeout=30, request_timeout=8,
    fallback_proxies=("socks5h://trusted-exit:1082",),
    local_ipv6_socket="/run/swoop-ipv6/pool.sock",
    local_ipv6_prefix="2604:2dc0:100:2437:5a00::/80",
    local_ipv6_mode="fresh",
)
```

The SDK authenticates the Unix peer as root, bounds the handshake to one second,
validates the returned address against the configured prefix and validates its
mode/token. The broker returns an already assigned, ready /128 immediately; no
address provisioning runs on the search path. Busy/unavailable brokers select
configured fallback directly. They never cause an unbound native request or
poison Google health. Without fallback a typed local availability error is raised.
Unconfigured callers retain their existing routing behavior.

A public search, continuation, pricing, calendar, Deals, Explore or Hotels call
holds one source through nested calls and fan-out work. Different top-level calls
lease separately. Fresh mode never reissues a consumed source; reuse mode uses the
broker's bounded healthy bank. Both modes pool native connections and isolate
cookies by operation/service/POS/currency. Native clients close before the lease
returns, since even a reusable address can be quarantined or retired. Sources
never enter the process-wide client cache. Existing sticky mia1 clients remain
pooled. On failover, sessions bootstrap again on mia1 with route-specific cookies
and parameters; original query, POS/currency, filters and selectors are retained.

Cancellation/deadlines stop further dispatch. Active body reads and worker
activity retain the source until they finish, even after their caller returns.
An unread rejected response is explicitly closed. A fully consumed response keeps
its connection available for pooling. A native-close failure retains its broker
socket through the immutable operation deadline plus 30 seconds. Broker leases
are bounded to 600 seconds; the Infra journal retains live PID/starttime/boot
identity through that bound plus grace after a daemon crash. The SDK sends only
its lease's release reason/Retry-After, never network-configuration commands.

Per-address health uses opaque source IDs. One address failure permits another
fresh source on the next operation, while mia1 handles the failed operation.
Three distinct source failures in 15 seconds open the shared prefix gate; a 429
opens it immediately and honors Retry-After. This prevents a fresh-IP loop during
prefix-wide refusal. Prefix concurrency is eight, cooldown/recovery uses the
existing bounded single-probe/backoff policy, and protocol-valid empty inventory
remains healthy. No evidence establishes Google's throttling scope or that a new
address fixes it. Addresses in this bank remain in one routed /64.

Infra owns provisioning and the opt-in NixOS module. Its 64-address limit includes
pending, leased, draining and quarantined addresses; failed deletions retain their
slots. Existing base/legacy aliases and proxies remain outside the owned bank.
No global nonlocal-bind or NDP changes are needed. Deployment requires a separate
reviewed activation under the existing promotion owner.

`python scripts/verify_local_ipv6_netns.py /path/to/infra-checkout --report /tmp/report.json`
runs the actual bank, SDK native client, session builders/parsers, C47 selectors,
SOCKS fallback/recovery and drain checks in a disposable user+network namespace.
It refuses the original namespace and uses only local HTTP. Reported latency is
local-fixture latency, not provider performance. Provider timing and throttling
claims require separately authorized, certification-mode traffic.
