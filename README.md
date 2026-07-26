# mise-php

A mise tool plugin for prebuilt, fat static PHP on Apple Silicon Macs.

The plugin lists releases from
[`bigpixelrocket/php-bin`](https://github.com/bigpixelrocket/php-bin), downloads
the matching CLI archive, verifies its SHA-256 checksum, and exposes `bin/php`.
It never compiles PHP locally.

## Status

The plugin contract and offline end-to-end tests are implemented. Real-world
installation remains gated on the first green, published `php-bin` release.

## Requirements

- macOS 26 (Tahoe) or newer on arm64 / aarch64
- a current mise release with vfox tool-plugin support

The plugin supports the maintained PHP branches 8.2 through 8.5. PHP branches
that have reached end of life are intentionally not listed or installable.

Other operating systems and Intel Macs receive an explicit unsupported-target
error. Older macOS releases cannot load the published binaries.

## Install

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
