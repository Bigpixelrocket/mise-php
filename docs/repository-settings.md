# Repository settings

The plan executor applies this state with the protected
`php-bin/scripts/configure-github-maintenance` command and verifies it with
`php-bin/scripts/snapshot-github-admin-state`. Snapshots are redacted: only
secret names are retained.

Required repository state:

- Require a pull request before merging.
- Require the `Plugin contract` status check.
- Bind the required check to the GitHub Actions app, preventing another app
  from satisfying the same context name.
- Require conversation resolution.
- Require linear history; block force pushes and branch deletion.
- Enforce protection for administrators and require CODEOWNER approval for
  protected control paths.
- Enable squash merge, auto-merge, update branch, and automatic head-branch
  deletion; disable merge commits and rebase merge.
- Allow workflow write permission for deterministic downstream jobs while
  repository-scoped Codex jobs remain `contents: read`.
- Create the protected `php-maintenance-release` environment.
- Enable GitHub immutable releases for future repository releases.
- Set `MAINTENANCE_OWNER=loadinglucian`.
- Keep a distinct repository-scoped `OPENAI_API_KEY` secret.

CODEOWNERS protects agent instructions, workflows, schemas, admission, sealing,
and merge admission. Generated snapshots and deterministic readiness records
remain outside CODEOWNERS so their exact-SHA PRs can merge; runtime sealing
still rejects event/readiness paths as agent-authored changes.

```bash
./php-bin/scripts/snapshot-github-admin-state \
  --repo bigpixelrocket/mise-php \
  --output mise-php/docs/admin-state/mise-php.json

./php-bin/scripts/configure-github-maintenance \
  --repo bigpixelrocket/mise-php \
  --owner loadinglucian \
  --required-check "Plugin contract"
```
