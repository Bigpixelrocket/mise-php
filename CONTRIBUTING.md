# Contributing

Changes are welcome through pull requests.

1. Run `scripts/test.sh` on an Apple Silicon Mac with mise installed.
2. Keep the `php-bin` artifact name and archive layout contract backward
   compatible.
3. Treat checksum verification, release filtering, and download URL changes as
   security-sensitive.
4. Do not weaken unsupported-platform checks or accept an artifact without a
   matching `SHA256SUMS` entry.

Pull requests require passing CI and one approving owner review. Direct pushes
to `main` are not part of the project workflow.

