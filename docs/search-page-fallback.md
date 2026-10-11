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

Airport alternatives were verified on 2026-10-10 Panama time (2026-10-11 UTC)
for a one-way, nonstop search on **2026-10-26**, JFK/EWR to LAX/BUR. Adding EWR
and BUR in Google's own airport picker produced a TFS URL with repeated
`Airport` messages at `FlightData` fields **13** (origins) and **14**
(destinations). This is one search leg with endpoint alternatives. No extra
legs or per-airport requests are needed. The sanitized actual URL parameter,
response airport metadata, and representative flight facts are archived in
`tests/fixtures/tfs/shopping_page_airport_alternatives.json`.

A plain HTTP GET using that Google-generated TFS returned 42 rows and echoed
both origin airports and both destination airports in the page's route
metadata. A second GET using Swoop's existing minimal `Airport` message
(field 2 only, leaving the Google UI's field 1 unset) also returned 42 rows
and the same endpoint metadata. Both JFK and EWR appear as actual departure
airports in returned flights. No BUR nonstop row was observed on that date;
preserving an alternative in the request does not promise returned coverage
for that airport. These probes searched metadata only; no booking or pricing
selection was made.

The implemented fallback was also probed with the same date and airport
alternatives, business cabin, two adults, UA, nonstop, basic-economy exclusion,
and separate-ticket exclusion. One page GET with the configured 17-second
timeout returned 10 UA rows with actual EWR/LAX endpoints and the requested
departure date. All encoded filters are covered by offline regressions;
the live result is an acceptance observation, not a completeness guarantee.

The fallback now preserves every explicitly requested airport alternative in
one page GET on the existing client. It keeps the `limited` result scope and
does not expand a caller's route into additional requests. The high-level
`SearchLeg` API continues to take singular airports; alternatives are exposed
by `search_raw()`. Singleton one-way and roundtrip TFS bytes, including all
cabins, mixed passenger types, carrier/stops filters, basic-economy and
separate-ticket flags, are pinned against the unmodified runtime in
`tests/fixtures/tfs/tfs_singleton_baseline.json`.

The upstream RPC proposals [PR #17](https://github.com/saraswatayu/swoop/pull/17)
and [PR #32](https://github.com/saraswatayu/swoop/pull/32) describe RPC airport
lists; PR #32 explicitly rejects alternatives on its protobuf path. The
Google-produced TFS capture above establishes the independent page encoding.

This is not full transport parity: selected itineraries, multi-city, and
time-window filters remain explicit errors on a rejected exit. Page searches
may expose fewer rows or passenger/cabin combinations than browser-driven
RPCs. Pricing/deals remain on their existing transports; keep working RPC
exits in a production pool for those operations. Do not represent an initial
roundtrip outbound option as a fully selected return itinerary.

Related upstream work: https://github.com/punitarani/fli#search-transport.
No browser profile, cookies, or generated security tokens are persisted.
