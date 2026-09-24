# Search-page fallback on RPC-rejected exits

Verified 2026-09-23 with production Swoop 0.7.0/primp 2.0.0, searching
JFK–LAX on 2026-11-15 from vin1 IPv4 51.81.56.55 and IPv6
2604:2dc0:100:2437::.

An identical unsigned GetShoppingResults request returns 30 flights through
the existing exit 2602:fb54:48d:: but HTTP 200 with this compact rejection on
vin1 (metadata omitted):

```json
[["wrb.fr", null, null, null, null, [13]]]
```

This is an upstream error, not an empty inventory result. Fresh Chromium
profiles on both vin1 addresses returned 31 basic-inclusive flights without
CAPTCHA/login. Standard HTTP headers, Firefox impersonation, a warm-up GET,
and removing `gl` did not make the unsigned RPC succeed. Browser-generated
RPC session/signature fields were necessary in the browser replay experiment.
The evidence shows exit-dependent RPC validation; Google's internal reason
for treating the networks differently is not observable.

Chromium is unnecessary for initial search. A plain HTTP GET of
`https://www.google.com/travel/flights?tfs=<existing TFSData protobuf>` returns
the same shopping data inside `AF_initDataCallback` key `ds:1`. The existing
decoder can read that data. Patched CLI checks on vin1 returned 30 flights
(basic excluded) on both families, 10 AA nonstop flights, and 31 roundtrip
outbound options for a 2026-11-22 return, all in under one second. The existing
proxy still returned 30 flights through RPC.

The fallback is attempted once, on the same configured HTTP client/exit, only
for the observed compact shopping status 13. Structured ErrorResponse,
other statuses, HTTP failures, and genuine empty RPC results retain their
existing behavior. Missing/malformed page data and page-contained errors
raise; they never become empty inventory.

TFS preserves airports, dates, cabin, passenger mix, stops, airline filters,
and basic-economy exclusion. The existing protobuf's field 25 does preserve
basic exclusion in our live check (31 flights/minimum USD 239 when included,
30/minimum USD 294 when excluded). Sort order is applied to returned rows.

This is not full transport parity: selected itineraries, multi-city, and
time-window filters remain explicit errors on a rejected exit. Page searches
may expose fewer rows or passenger/cabin combinations than browser-driven
RPCs. Pricing/deals remain on their existing transports; keep working RPC
exits in a production pool for those operations. Do not represent an initial
roundtrip outbound option as a fully selected return itinerary.

Related upstream work: https://github.com/punitarani/fli#search-transport.
No browser profile, cookies, or generated security tokens are persisted.
