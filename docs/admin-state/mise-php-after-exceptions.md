# Administrator-state capability exceptions

Captured: 2026-07-27  
Review by: 2026-10-27

The final administrator snapshot records two GitHub Secret Protection controls
as disabled:

- `secret_scanning_validity_checks`
- `secret_scanning_non_provider_patterns`

Both controls were explicitly requested through the repository API on
2026-07-27 and GitHub retained them as disabled. GitHub documents these controls
as requiring an organization-owned repository on GitHub Team or Enterprise with
GitHub Secret Protection enabled. The organization does not currently expose
that capability.

This is a platform/plan limitation, not an accepted permanent security posture.
Re-test no later than 2026-10-27, or immediately after GitHub Secret Protection
is enabled for the organization. Until then, the supported baseline remains
enabled: Dependabot security updates, provider-pattern secret scanning, and
secret-scanning push protection.

References:

- https://docs.github.com/en/code-security/secret-scanning/using-advanced-secret-scanning-and-push-protection-features/non-provider-patterns/enabling-secret-scanning-for-non-provider-patterns
- https://docs.github.com/en/enterprise-cloud@latest/code-security/how-tos/secure-your-secrets/customize-leak-detection/enabling-validity-checks-for-your-repository
