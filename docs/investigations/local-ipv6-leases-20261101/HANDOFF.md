# Local IPv6 source leases

Implementation owner: Swoop terminal `term_8cf8119f-f55c-479e-b466-ef8476159773`,
checkout `local-ipv6-leases-20261101`. Fresh source following prepared SDK
`4cd207980e336aa01ba995676326adebe5cfb2a5`; earlier READY history remains intact.
C47 and the unified session policy remain ancestors. Consumer adoption lives in
`ndcterm/swoop-local-ipv6-leases-20261101`; Infra owns its separate address bank.

See [native transport](../../NATIVE_TRANSPORT.md#local-source-address-bank-opt-in)
for configuration, lifecycle, scope of health and bounded fallback/recovery.
This change does not activate production or authorize admission/release/provider
calls. Existing promotion ownership and the user review hold remain in force.

Verification uses the normal full offline/type gate and joint namespace runner.
The final exact SHAs and test counts are in the consumer handoff packet. The
namespace JSON records both source SHAs and local timing samples. It does not
measure Google or establish that changing source addresses avoids throttling.

The native HTTP investigation found an actual pooling defect during joint tests:
calling wreq Response.close after consuming the body discarded a healthy TCP
connection. The adapter now aborts unread error bodies and leaves consumed bodies
eligible for pooling. Actual IPv6 receiver source/port assertions cover reuse.
Lease refs also cover response bodies and background session workers, preventing
address retirement while a cancelled/partial operation is still draining.

Historical production health remains calendar `http_redirect`, shopping `timeout`,
fallback closed/healthy. Direct IPv6 has succeeded and recovered in recorded
runs; intermittent refusal/timeouts and earlier OW calendar 8.14s/shopping 3.18s
outliers do not prove provider throttling or a native speed advantage. No new
provider/financial/benchmark calls were made here. Hotels M0CRd gRPC 3 remains
separate unresolved wire triage, not not-applicable or a transport exception.
