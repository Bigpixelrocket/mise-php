# Repository settings

The plan executor applies this state with the protected
`php-bin/scripts/configure-github-autorelease` command and verifies it with
`php-bin/scripts/snapshot-github-admin-state`. Snapshots are redacted: only
secret names are retained.

Required repository state:

- Protect `main` with the `protect_main` repository ruleset: require a pull
  request (squash merges only, CODEOWNER review, review-thread resolution),
  require the status checks below on an up-to-date branch, require linear
  history, and block force pushes and branch deletion. Repository
  administrators are bypass actors in `pull_request` mode only: the solo
  owner can merge a pull request past a failing rule but can never push,
  force-push, or delete `main` directly. Classic branch protection (and its
  `enforce_admins` toggle) is retired; the configure script removes it.
- Require the `Plugin contract` status check.
- Require the base-controlled `Protected controls` status check. It passes
  automatically for unprotected generated paths and for owner-authored pull
  requests (a solo owner cannot approve their own PR, so an owner review
  requirement was unsatisfiable there); any other author touching a path in
  `autorelease/protected-paths.json` requires an exact-head `loadinglucian`
  approval.
- Bind the required checks to the GitHub Actions app, preventing another app
  from satisfying the same context name.
- Enable squash merge, auto-merge, update branch, and automatic head-branch
  deletion; disable merge commits and rebase merge.
- Keep the default workflow token read-only; deterministic downstream jobs
  explicitly request write scopes while repository-scoped Codex jobs remain
  `contents: read`.
- Do not allow the workflow token to approve pull requests.
- Because GitHub suppresses ordinary PR events created by `GITHUB_TOKEN`, each
  deterministic PR coordinator explicitly dispatches `ci.yml` and
  `protected-controls.yml` at the exact PR branch, accepts only newly created
  successful validator runs for that head SHA, and only then publishes the
  Actions-owned check evidence plus PR-visible commit statuses with the exact
  validator URLs.
- Allow GitHub-owned Actions plus only `openai/codex-action` and
  `jdx/mise-action`, and require every Action reference to use a full commit
  SHA.
- Create the protected `php-autorelease-publish` environment.
- Enable GitHub immutable releases for future repository releases.
- Set `AUTORELEASE_OWNER=loadinglucian`.
- Keep a distinct repository-scoped `OPENAI_API_KEY` secret.

CODEOWNERS protects agent instructions, workflows, schemas, admission, sealing,
and merge admission. Generated snapshots and deterministic readiness records
remain outside CODEOWNERS so their exact-SHA PRs can merge; runtime sealing
still rejects event/readiness paths as agent-authored changes.

```bash
./php-bin/scripts/snapshot-github-admin-state \
  --repo bigpixelrocket/mise-php \
  --output mise-php/docs/admin-state/mise-php-after.json

./php-bin/scripts/configure-github-autorelease \
  --repo bigpixelrocket/mise-php \
  --owner loadinglucian \
  --required-check "Plugin contract" \
  --environment php-autorelease-publish
```
