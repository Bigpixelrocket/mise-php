# Implementation phase

Observable goal: satisfy the admitted mise-php edit at the exact base commit,
inside only admitted paths, and leave a diff ready for deterministic sealing
and clean validation.

Use no web or shell network. Run and record all advisory checks. Do not change
protected or unadmitted paths. When the edit changes `support-snapshot.json`,
run `scripts/generate-policy-lua` and include the regenerated `lib/policy.lua`
in the same diff. Return GO only when all criteria pass, the local support
behavior matches the accepted php-bin policy, and unresolved is empty.
Do not commit, push, merge, tag, publish, or record readiness yourself.
