# Preserve the landed continuation contract in unified transport

Review of consumer `64c13e2a1aa668cbdcbc15e3b17c4f90e091a07a` against current
develop `913239e33d2944eddc003f559ca63971f27785a3` exposed a real integration
blocker: the landed wrapper always sends `next_leg_filters`, including `{}`,
while SDK `0d956e42fd548fb5ce1e52625726e27d546d36f9` lacked the parameter and
its implementation. The old READY packet is withdrawn. This repair preserves
both histories by merging C47 (`c47e9dfb1a051cac15e12ee36ef5e64a11639a9f`)
into the unified SDK; C47 must be an ancestor of the new immutable pin.

C47 is retained in full: validated narrowing/intersection of next-bound filters,
revalidation of selected prefixes under their saved intent, checks of both
transfer airports, and effective constraints in selectors for continuation and
pricing. Accepting and ignoring the parameter is not a compatible repair.

`tests/test_native_continuation_policy.py` exercises the real public Next,
request builder, response decoder, shared route policy and selector-price replay.
Only the HTTP edge is replaced with synthetic frames. It checks empty/default
filters and narrowing, unchanged selected MIA outbound, intersection of return
constraints, exact encoded replay after native refusal, shared fallback health,
POS/currency/party/bags/price retention, pricing under the narrowed selector and
impossible intersections rejected before any dispatch.

The consumer has a real ASGI `/search-next` and `/price` regression using the
same synthetic framing. Against the old canonical SDK it fails with HTTP 500
for both `{}` and structured filters (unexpected `next_leg_filters`) and for an
impossible intersection that should return 422. This is the pre-fix evidence;
new provenance/producer checks must pass against the regenerated immutable pin.

Offline verification after the SDK merge: 50 focused continuation/native/session
checks passed. `make check`: pyright zero errors; 2290 passed, 54 skipped,
18 live deselected. Existing actual native IPv6 / actual local SOCKS session
pool/fallback/recovery regressions run offline and remain passing. No new
provider, financial or benchmark calls were made for this repair. The earlier
vin1 JSON remains historical evidence for its recorded runtime/package hash;
it is not relabeled as live acceptance of this merged source.

Hotels M0CRd gRPC 3 remains **separate unresolved wire triage**. Preserving
partial AtySUc seed results does not resolve the rejected follow-up or make it
not-applicable. This continuation integration repair claims no fix for it.

The consumer merges current develop, retaining the existing 5cb Deals
compatibility history, #3106 and coordinator dependency-link cleanup. Pin,
complete-history bundle, manifest, installer filename, tamper tests and canonical
pip provenance must advance together. The new exact source/preparation receipt
supersedes the old packet. Admission and release remain withheld pending review
by the current promotion owner.
