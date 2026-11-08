# Contributing

Changes are welcome through pull requests.

1. On an Apple Silicon Mac or on Linux x86_64 or aarch64 with glibc, with mise
   installed, run `mise trust && mise install` once to get the pinned dev tools
   from `mise.toml`, then run `mise exec -- scripts/test.sh` (or
   `scripts/test.sh` in a mise-activated shell).
2. Keep the `php-bin` artifact name and archive layout contract backward
   compatible.
3. Treat checksum verification, release filtering, and download URL changes as
   security-sensitive.
4. Do not weaken unsupported-platform checks or accept an artifact without a
   matching `SHA256SUMS` entry.

Pull requests require passing CI and one approving owner review. Direct pushes
to `main` are not part of the project workflow.

