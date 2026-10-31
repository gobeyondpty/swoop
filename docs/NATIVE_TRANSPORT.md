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
