# Investigation phase

Observable goal: compare the captured accepted php-bin policy and event records
with the exact local support snapshot, then produce one evidence-bound plan
without modifying the repository.

Identify whether local parsing, filtering, fixtures, documentation, temporary
artifact installation, or readiness state must change. Cite exact public policy
commit and digests. Do not independently fetch or classify upstream PHP data.
Return GO only when every criterion passes and unresolved is empty.

The plan must cite each of the four records in `policy-capture.json` exactly
once. Each evidence item has `captureId`, the captured `digest`, and a
`locator` with `kind: json_pointer` and a resolving JSON Pointer `value`.
