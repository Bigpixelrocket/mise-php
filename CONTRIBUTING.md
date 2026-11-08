# Contributing

Changes are welcome through pull requests.

1. On an Apple Silicon Mac or on Linux x86_64 or aarch64 with glibc, with mise
   on your `PATH`, run `scripts/test.sh`. It trusts this checkout for its own
   mise calls and needs no network. For the pinned dev tools in `mise.toml`
   that CI also uses, run `mise trust && mise install` once, then lint with
   `mise exec -- shellcheck scripts/*.sh test/fixture-php.sh`.
2. Keep the `php-bin` artifact name and archive layout contract backward
   compatible.
3. Treat checksum verification, release filtering, and download URL changes as
   security-sensitive.
4. Do not weaken unsupported-platform checks or accept an artifact without a
   matching `SHA256SUMS` entry.

Pull requests require passing CI and one approving owner review. Direct pushes
to `main` are not part of the project workflow.

