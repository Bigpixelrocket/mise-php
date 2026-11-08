# mise-php

A mise tool plugin for prebuilt, fat static PHP on Apple Silicon Macs and on
Linux x86_64 and aarch64.

The plugin lists releases from
[`bigpixelrocket/php-bin`](https://github.com/bigpixelrocket/php-bin), downloads
the matching CLI archive, verifies its SHA-256 checksum, exposes `bin/php`, and
gives every install its own editable `php.ini`. It never compiles PHP locally.

## Status

The plugin contract, offline end-to-end tests, and installation from published
`php-bin` releases are verified on macOS 26 arm64. The offline tests also pass
on Linux x86_64 (Arch, Fedora, Debian, and Ubuntu) and on Linux aarch64 in CI.

Linux builds exist only in `php-bin` releases published after it started
building for Linux; every earlier release is macOS-only. On Linux,
`mise ls-remote php` lists only the versions with a Linux build. Run it for
the versions available right now.

## Requirements

- macOS 26 (Tahoe) or newer on arm64 / aarch64, or
- Linux on x86_64 or aarch64 with glibc 2.17 or newer: Fedora, RHEL and its
  rebuilds, Ubuntu, Debian, Pop!_OS, Arch, Gentoo with glibc, and other
  distributions that keep the standard dynamic loader
- a current mise release with vfox tool-plugin support

The plugin supports the maintained PHP branches recorded in
[`support-snapshot.json`](support-snapshot.json), which tracks the accepted
`php-bin` support policy automatically. Branches that have reached end of life
are delisted, so they stop appearing in `mise ls-remote php` and stop resolving
from a branch shorthand. Exact versions published before that point remain
installable, because their `php-bin` releases are immutable.

Other operating systems and Intel Macs receive an explicit unsupported-target
error. Older macOS releases cannot load the published binaries. On Linux,
distributions built on musl libc, such as Alpine, and NixOS receive the same
error before anything downloads: the binaries need glibc and its standard
loader path.

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
patch, and installing `8.4.5` picks its newest revision built for your
platform: a revision without a Linux build is skipped on Linux. To pin one
exact build, name the revision:

```toml
[tools]
php = "8.4.5-1"
```

A pinned revision without a build for your platform fails with
`no Linux x86_64 build for 8.4.5-1` (or the platform you are on).

### Lockfiles

`mise lock` records the right archive for each platform: on a Mac it can lock
`linux-x64` and `linux-arm64` entries, and on Linux a `macos-arm64` entry, each
from the newest revision built for that platform:

```bash
mise lock --platform macos-arm64,linux-x64,linux-arm64
```

Platforms `php-bin` does not build, such as `macos-x64` and `windows-x64`, get
no entry and do not stop the lock. mise also writes `linux-x64-musl` and
`linux-arm64-musl` entries by default. It does not tell the plugin which libc
an entry is for, so those entries name the glibc archive, which a musl system
refuses to install. Set `lockfile_platforms` to the platforms you use to leave
them out:

```toml
[settings]
lockfile_platforms = ["macos-arm64", "linux-x64", "linux-arm64"]
```

A version with no build for one of the platforms, such as a patch released
before Linux builds, fails the lock with the same `no ... build` error.

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

The bundled extensions can differ slightly between platforms; each install's
`php.ini` lists the ones that install has.

Installing a new patch of a branch you already have copies `php.ini` from your
newest install of that branch, so your settings and extension choices follow
you to `8.4.6`. The `extension_dir` line is rewritten to point at the new
install. Extensions bundled with the new install that the old file never
mentioned get their default lines. `mise install -f` of a version you already
have starts from your newest *other* install of that branch, so edits made only
in the reinstalled version are replaced.

To keep a version's own settings through a reinstall, copy its `php.ini` out
of the install folder first and name the copy in `MISE_PHP_CARRY_INI`:

```bash
cp "$(mise where php@8.4.6)/bin/php.ini" /tmp/php-8.4.6.ini
MISE_PHP_CARRY_INI=/tmp/php-8.4.6.ini mise install -f php@8.4.6
```

The named file is carried forward exactly as another install's would be. When
the variable is set but the file cannot be read, the install goes on and
carries from your newest other install instead. Set the variable for that one
command only: exported from a shell profile, it would carry the same file into
every PHP install, whatever its branch.

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

### HTTPS certificates on Linux

The Linux binaries look for CA certificates under `/etc/ssl`, where Debian,
Ubuntu, and Arch keep them; Fedora and RHEL keep theirs under `/etc/pki`. So
each Linux install's `php.ini` names the first CA bundle the system has, from
this list, for both OpenSSL and curl:

```text
/etc/ssl/certs/ca-certificates.crt
/etc/pki/tls/certs/ca-bundle.crt
/etc/ssl/cert.pem
/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem
/etc/ssl/ca-bundle.pem
```

On RHEL, for example:

```ini
; CA certificates for HTTPS, found on this system by mise-php.
openssl.cafile = "/etc/pki/tls/certs/ca-bundle.crt"
curl.cainfo = "/etc/pki/tls/certs/ca-bundle.crt"
```

With none of them present, both settings stay unset. A carried-forward
`php.ini` keeps any `openssl.cafile` or `curl.cainfo` line it already has,
active or commented out, as you wrote it; only a setting the file never
mentions is added. macOS installs get no CA lines.

### Building your own extensions

Each install includes `phpize`, `php-config`, and the PHP headers, so
[PIE](https://github.com/php/pie) and `phpize` can build extensions against it.
Install the Xcode Command Line Tools and `autoconf` (for example
`brew install autoconf`) first, or on Linux a C compiler, `make`, and
`autoconf` (for example `sudo apt install build-essential autoconf`), then run
PIE with the PHP you want to extend:

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

For a version such as `8.4.5`, the plugin requires two assets on the matching
`php-bin` GitHub Release: the archive for your platform and `SHA256SUMS`.

```text
php-8.4.5-cli-macos-aarch64.tar.gz
php-8.4.5-cli-linux-x86_64.tar.gz
php-8.4.5-cli-linux-aarch64.tar.gz
SHA256SUMS
```

`SHA256SUMS` lists every archive in the release, and the plugin uses the line
for your platform's archive. Installation fails if the release, archive,
checksum file, or exact checksum entry is missing. Mise performs the SHA-256
verification before activation.

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

The test suite runs on macOS arm64 and on Linux x86_64 or aarch64, and
installs fixture archives for the platform it runs on. It serves local fixture
releases and verifies paginated version listing and ordering, rebuild-revision
resolution per platform, the errors for a missing platform build and a musl
host, lockfile entries for every platform, checksum-backed installation
against a `SHA256SUMS` with several entries, the Linux CA lines in `php.ini`, that no token reaches a server other than `api.github.com`,
`php.ini` creation and carry-forward across archive layouts and PIE-added
lines, carry-forward from a file named in `MISE_PHP_CARRY_INI`, build-kit
relocation, and `PATH` activation through mise.

## Contributing and security

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Report security issues using
[`SECURITY.md`](SECURITY.md), not a public issue.

## License

MIT. Downloaded PHP archives carry their own notices and licenses.
