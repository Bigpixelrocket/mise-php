# mise-php

A mise tool plugin for prebuilt, fat static PHP on Apple Silicon Macs.

The plugin lists releases from
[`bigpixelrocket/php-bin`](https://github.com/bigpixelrocket/php-bin), downloads
the matching CLI archive, verifies its SHA-256 checksum, and exposes `bin/php`.
It never compiles PHP locally.

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

Pin a full patch or rebuild revision when repeatability matters:

```toml
[tools]
php = "8.4.5"
```

Mise also reads the plugin from a repository declaration:

```toml
[plugins]
php = "https://github.com/bigpixelrocket/mise-php"

[tools]
php = "8.4"
```

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

The test suite serves a local fixture release and verifies version listing,
checksum-backed installation, and `PATH` activation through mise.

## Contributing and security

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Report security issues using
[`SECURITY.md`](SECURITY.md), not a public issue.

## License

MIT. Downloaded PHP archives carry their own notices and licenses.
