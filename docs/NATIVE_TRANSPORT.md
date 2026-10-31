# Family-controlled transport and bounded RPC failover

On Python 3.11+, install `swoop-flights[native]` to enable `TransportConfig(address_family="ipv6")`.
The optional, pinned `wreq==0.13.0` Rust client supplies IPv6-only DNS and an IPv6
local bind. URLs keep their hostnames; SNI, certificate-chain and hostname checks
stay enabled. It uses Chrome137 for the generic `chrome` profile. Python 3.10 retains the default primp transport. Existing
`address_family="auto"` callers continue to use primp. This is an explicit native
backend, not the diagnostic curl adapter and not a claim that primp gained IPv6
configuration.

```python
transport = TransportConfig(
    address_family="ipv6", country="US", currency="USD", timeout=8,
    fallback_proxies=("socks5h://trusted-pool:1082", "socks5h://trusted-exit:1080"),
)
```

Only trusted application configuration should choose exits. Family-controlled
direct ignores global/environment proxies. Nonempty `fallback_proxies` enables
adaptive routing for shopping, booking and calendar RPCs through `_http_post`.
Deals/explore session acquisition and hotels retain their existing transport
policy; adaptive RPC routing is not enabled on those paths.

The policy makes at most three dispatches: direct, then up to two distinct
configured primp exits. Each replays the original URL, encoded bytes and headers;
no search is reconstructed and no selector, passenger, fare, bag, routing or POS
filter changes. It divides the remaining RPC budget among remaining routes and
never extends an enclosing monotonic operation deadline. Adaptive requests do not
nest 429 retries or shopping-page fallbacks. `_operation_deadline` and
`_cancel_check` are internal integration hooks for enclosing operations and
cooperative disconnect handling. A running native operation retains its timeout;
cancellation prevents subsequent dispatches.

HTTP 403, 429 (including Retry-After), redirects, 5xx, compact gRPC INTERNAL,
network failures, timeouts and malformed responses have separate health reasons.
Compact INTERNAL is an upstream refusal, not evidence of an IP ban. Detailed
gRPC errors and invalid arguments propagate immediately. Protocol-valid empty
inventory is healthy and does not trigger failover. An ambiguous body permits
one identical-query comparison on a non-cooling fallback; only parsed inventory
on that comparison marks the primary as hiding inventory. Unresolved ambiguous
bodies remain errors, rather than becoming a no-fare result.

Health is shared by transport identity and RPC operation, never by tenant or
query. A failure opens that route for at least 30 seconds (longer for Retry-After).
One caller may probe it after cooldown; other callers use fallback. Late responses
cannot erase a newer failure. Active calls are capped at eight per route and state
at 64 routes; unexpired cooldowns are not evicted to admit more state. Health
snapshots from `swoop._adaptive.health()` contain no proxies, credentials or query
payloads. All cooling/capacity-limited routes produce a typed availability error.

Clients are cached in the existing bounded pool. Native connections have a
90-second idle timeout, four idle connections per host and a 16-connection pool
cap; HTTP/2 is negotiated normally. Initializing the native client at service
startup moves library initialization off the first request without provider
traffic. A sticky fallback identity retains its existing primp connection pool.
Native per-phase DNS/connect/TLS/TTFB measurements are unavailable and remain
unknown; the existing diagnostics measure complete send/read time, decoding and
actual HTTP dispatches. Do not infer a SOCKS handshake failure from a generic
transport exception's wording.

Validate live traffic under certification call-mode. `native_probe.py` in the
vin1 investigation directory is an isolated, read-only actual-client benchmark
and controlled failure/recovery probe; it never replaces production files.
AF_INET6 must be checked with the actual client's syscalls, not curl's. Google
can refuse a route after an earlier successful call: report failures as well as
successful latency samples, and do not treat a small sample as a latency SLA.
