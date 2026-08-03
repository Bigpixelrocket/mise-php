# Investigation phase

Observable goal: compare the captured accepted php-bin policy and event records
with the exact local support snapshot, then produce one evidence-bound plan
without modifying the repository.

Identify whether local parsing, filtering, fixtures, documentation, temporary
artifact installation, or readiness state must change. A `support-snapshot.json`
edit also regenerates `lib/policy.lua`, so admit both paths in the same plan.
Cite exact public policy commit and digests. Do not independently fetch or
classify upstream PHP data.
Return GO only when every criterion passes and unresolved is empty.

Treat `requiredChecks` as downstream exact-head gates, not investigation-phase
advisory checks. Declare them in the plan, but do not run them in this read-only
phase or treat their not-yet-run status as unresolved; writable deterministic
jobs execute them before merge.

The plan must cite each of the four records in `policy-capture.json` exactly
once. Each evidence item has `captureId`, the captured `digest`, and a
`locator` with `kind: json_pointer` and a resolving JSON Pointer `value`.
