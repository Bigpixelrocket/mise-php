# mise-php

A mise tool plugin for prebuilt, fat static PHP on Apple Silicon Macs.

The plugin lists releases from
[`bigpixelrocket/php-bin`](https://github.com/bigpixelrocket/php-bin), downloads
the matching CLI archive, verifies its SHA-256 checksum, exposes `bin/php`, and
gives every install its own editable `php.ini`. It never compiles PHP locally.

## Status

The plugin contract, offline end-to-end tests, and installation from published
`php-bin` releases are verified on macOS 26 arm64. Run `mise ls-remote php` for
the versions available right now.

## Requirements

- macOS 26 (Tahoe) or newer on arm64 / aarch64
- a current mise release with vfox tool-plugin support

The plugin supports the maintained PHP branches recorded in
[`support-snapshot.json`](support-snapshot.json), which tracks the accepted
`php-bin` support policy automatically. Branches that have reached end of life
are delisted, so they stop appearing in `mise ls-remote php` and stop resolving
from a branch shorthand. Exact versions published before that point remain
installable, because their `php-bin` releases are immutable.

Other operating systems and Intel Macs receive an explicit unsupported-target
error. Older macOS releases cannot load the published binaries.

## Autorelease

This repository tracks `php-bin` automatically. A daily consumer captures the
accepted public support policy, and deterministic workflows admit, seal, and
merge any required change, then record exact-commit readiness.

See [AUTORELEASE.md](AUTORELEASE.md) for the full contract, the operator
pause control, and maintainer commands.

## Install

If another plugin is already installed under the `php` name, inspect it and
remove it before installing `mise-php`:

```bash
mise plugins ls --urls
mise plugins uninstall --purge php
```

Only run the uninstall command after confirming that `php` is the plugin you
want to replace. The `--purge` option also removes PHP versions, downloads, and
cache managed by that plugin.

```bash
mise plugin install php https://github.com/bigpixelrocket/mise-php
mise ls-remote php
mise use -g php@8.4
php -v
php -m
```

Pin a full patch when repeatability matters:

```toml
[tools]
php = "8.4.5"
```

`php-bin` sometimes rebuilds a published patch with a changed recipe and
publishes it as a revision such as `8.4.5-1`. Listings show only the plain
patch, and installing `8.4.5` picks its newest published revision. To pin one
exact build, name the revision:

```toml
[tools]
php = "8.4.5-1"
```

### GitHub API token

The plugin reads release metadata from the GitHub API. Anonymous API calls
share a small hourly limit per network address, which shared CI runners use up
quickly; the plugin then fails with HTTP 403 and names the variables below.
Set a token to use your account's much larger limit:

```bash
export MISE_PHP_GITHUB_TOKEN="$(gh auth token)"
```

- `MISE_PHP_GITHUB_TOKEN` is used first, then `GITHUB_TOKEN`, so the token a
  GitHub Actions job already provides works unchanged. An empty value counts as
  unset.
- The token needs no scopes: `php-bin` releases are public. A read-only token
  is enough.
- The plugin sends it only as `Authorization: Bearer` to
  `https://api.github.com`: never with a download, including the release assets
  GitHub redirects to other hosts, and never to a custom
  `MISE_PHP_API_BASE_URL`.
- The plugin never prints or logs it.
- With neither variable set, mise adds its own GitHub token, such as
  `MISE_GITHUB_TOKEN` or `GITHUB_API_TOKEN`, to the plugin's `api.github.com`
  calls when it has one. Without any token the calls stay anonymous and share
  the small limit.

Mise also reads the plugin from a repository declaration:

```toml
[plugins]
php = "https://github.com/bigpixelrocket/mise-php"

[tools]
php = "8.4"
```

## php.ini and extensions

Each install has its own `php.ini` at `bin/php.ini` inside the install folder:

```bash
php --ini
```

PHP reads it however that install's `php` starts: through the mise shim, by
absolute path from an IDE, or through a symlink. Nothing is shared between
installs, and no environment variable is needed.

Commonly used extensions ship as shared extensions in the install's
`lib/php/extensions` folder. Each one has a line in `php.ini`; remove the
leading `;` to turn it on, or add one to turn it off:

```ini
extension=redis
;zend_extension=xdebug
;extension=pcov
```

Releases built before shared extensions compile every module into `bin/php`
and have no extension lines; their `php.ini` still holds your settings.
Keep one active line per extension: PHP warns `Module already loaded` when it
loads the same extension twice.

Installing a new patch of a branch you already have copies `php.ini` from your
newest install of that branch, so your settings and extension choices follow
you to `8.4.6`. The `extension_dir` line is rewritten to point at the new
install. Extensions bundled with the new install that the old file never
mentioned get their default lines. `mise install -f` of a version you already
have starts from your newest *other* install of that branch, so edits made only
in the reinstalled version are replaced.

The copy keeps exactly one active line per extension, and marks every line it
changes with a comment directly above it that names the extension:

- An extension the new install lacks is commented out. If the new `bin/php`
  has that module built in, which is the case for releases built before shared
  extensions, the note says so; otherwise it says to rebuild it with PIE.
- A line commented out that way becomes active again in a later install that
  has the extension. Only the line directly below its note, in exactly the
  form mise-php wrote it, counts: remove a note together with its line, and a
  line of yours that ends up below a note is left as you wrote it.
- A second active line for the same extension is commented out.
- A commented line for an extension another line enables is marked
  `keep this one commented`.

Extensions a new install bundles that the old file never mentioned join the
list under `; Bundled shared extensions`. A file carried forward by an earlier
release can hold a second such header over the extensions that copy added. The
copy keeps only the first header and drops the second with the blank line above
it, so where the two lists follow each other they become one. The copy reads
`php.ini` the way PHP does: a value in double or single quotes can span several
lines, and so can an unquoted value that contains an apostrophe, up to the next
apostrophe in the file. Lines inside such a value are left exactly as they are,
even ones that look like `extension=` lines, because PHP does not read them as
settings.

### Building your own extensions

Each install includes `phpize`, `php-config`, and the PHP headers, so
[PIE](https://github.com/php/pie) and `phpize` can build extensions against it.
Install the Xcode Command Line Tools and `autoconf` (for example
`brew install autoconf`) first, then run PIE with the PHP you want to extend:

```bash
php pie.phar install apcu/apcu
```

PIE builds the extension into that install's `lib/php/extensions` and adds its
line to the end of that install's `php.ini`. Extensions you build are never
copied to another install: after a patch upgrade their lines are commented out
with a note, and `pie install` builds them again for the new install.

PIE reads `php.ini` one line at a time, so it stops with a syntax error, after
building the extension but before adding its line, when a value in `php.ini`
spans several lines. Add the `extension=` line yourself in that case.

PIE ignores commented lines, so it adds a line of its own even when `php.ini`
already has a commented line for that extension. Two ways keep this clean:

- For an extension bundled with the install, remove the `;` from its line
  instead of running PIE. PIE would replace the bundled build with its own.
- After `pie install` rebuilds an extension an upgrade commented out, leave the
  old commented line alone: PIE's new line is the one that loads it. The next
  patch install drops the old line mise-php commented out, and marks any other
  commented line for it `keep this one commented`.

## Artifact verification

For a version such as `8.4.5`, the plugin requires both assets on the matching
`php-bin` GitHub Release:

```text
php-8.4.5-cli-macos-aarch64.tar.gz
SHA256SUMS
```

Installation fails if the release, archive, checksum file, or exact checksum
entry is missing. Mise performs the SHA-256 verification before activation.

## Runtime dependencies

Some compiled modules require an operating-system driver or external service
to perform useful work. In particular, SQL Server connections require a
compatible ODBC driver. See the
[`php-bin` runtime dependency guide](https://github.com/bigpixelrocket/php-bin/blob/main/docs/runtime-deps.md).

## Local development

```bash
mise plugin link php "$PWD"
mise ls-remote php
scripts/test.sh
```

The test suite serves local fixture releases and verifies paginated version
listing and ordering, rebuild-revision resolution, checksum-backed
installation, that no token reaches a server other than `api.github.com`,
`php.ini` creation and carry-forward across archive layouts and PIE-added
lines, build-kit relocation, and `PATH` activation through mise.

## Contributing and security

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Report security issues using
[`SECURITY.md`](SECURITY.md), not a public issue.

## License

MIT. Downloaded PHP archives carry their own notices and licenses.
