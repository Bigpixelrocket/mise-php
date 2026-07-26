#!/usr/bin/env python3
"""Deterministic admission and sealing for repository-scoped mise changes."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import pathlib
import re
import subprocess
import sys
from typing import Any


PROTECTED = (
    ".github/codex/maintenance/*",
    ".github/workflows/*",
    ".codex/*",
    "schemas/agent-*",
    "maintenance/*",
    "scripts/admit-maintenance-plan",
    "scripts/seal-maintenance-patch",
    ".github/CODEOWNERS",
)
PROHIBITED = {"merge", "push", "tag", "release", "publish", "workflow_permissions", "secret_access"}


class AdmissionError(RuntimeError):
    pass


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def digest_file(path: pathlib.Path) -> str:
    return digest_bytes(path.read_bytes())


def load(path: pathlib.Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise AdmissionError(f"cannot load {path}: {error}") from error


def write(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical(value))


def protected(path: str) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in PROTECTED)


def validate_assessment(assessment: dict, contract: dict, digests: dict) -> None:
    if assessment.get("instructionDigests") != digests:
        raise AdmissionError("instruction digests changed")
    expected = {item["id"] for item in contract["completionCriteria"]}
    results = assessment.get("criteria", [])
    if {item.get("id") for item in results} != expected or len(results) != len(expected):
        raise AdmissionError("criterion results do not exactly match the contract")
    all_passed = (
        assessment.get("phaseStatus") == "complete"
        and all(item.get("status") == "passed" and item.get("evidence") for item in results)
        and assessment.get("unresolved") == []
    )
    if (assessment.get("goNoGo") == "go") != all_passed:
        raise AdmissionError("assessment go/no-go is internally inconsistent")


def admit(plan: dict, contract: dict, shared: pathlib.Path, phase: pathlib.Path, event: pathlib.Path, policy_digest: str, mise_head: str) -> dict:
    if plan.get("schemaVersion") != 1:
        raise AdmissionError("unsupported plan version")
    if plan.get("action") in {"blocked", "needs_human"}:
        raise AdmissionError("no-go plan cannot advance")
    digests = {
        "shared": digest_file(shared),
        "phaseTemplate": digest_file(phase),
        "eventContract": digest_file(event),
    }
    if plan.get("agentContract", {}).get("instructionDigests") != digests:
        raise AdmissionError("plan instruction digests changed")
    validate_assessment(plan.get("completionAssessment", {}), contract, digests)
    preconditions = plan.get("preconditions", {})
    if preconditions.get("misePhpHead") != mise_head:
        raise AdmissionError("mise-php base precondition is stale")
    if preconditions.get("supportPolicyDigest") != policy_digest:
        raise AdmissionError("php-bin policy precondition is stale")
    for patterns in plan.get("allowedPaths", {}).values():
        for pattern in patterns:
            if protected(pattern):
                raise AdmissionError(f"runtime plan admits protected path: {pattern}")
    if set(plan.get("agentOperations", [])) & PROHIBITED:
        raise AdmissionError("runtime plan grants irreversible authority")
    return {
        "admitted": True,
        "actionKey": plan["actionKey"],
        "planDigest": digest_bytes(canonical(plan)),
        "instructionDigests": digests,
    }


def seal(repo: pathlib.Path, base: str, plan: dict, result: dict, contract: dict, output: pathlib.Path) -> dict:
    digests = plan["agentContract"]["instructionDigests"]
    validate_assessment(result, contract, digests)
    if result["goNoGo"] != "go":
        raise AdmissionError("implementation result is no-go")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
    if head != base:
        raise AdmissionError("implementation checkout is not the admitted base")
    changed = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACDMRTUXB", base, "--"],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.splitlines()
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.splitlines()
    paths = sorted(set(changed + untracked))
    if not paths:
        raise AdmissionError("implementation produced no patch")
    allowed = [item for values in plan.get("allowedPaths", {}).values() for item in values]
    files = []
    for path in paths:
        candidate = repo / path
        if protected(path) or not any(fnmatch.fnmatch(path, pattern) for pattern in allowed):
            raise AdmissionError(f"forbidden diff path: {path}")
        if candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_size > 2_000_000:
            raise AdmissionError(f"unsupported diff entry: {path}")
        body = candidate.read_bytes()
        if b"\0" in body:
            raise AdmissionError(f"binary diff entry: {path}")
        mode = candidate.stat().st_mode & 0o777
        if mode not in {0o644, 0o755} or (mode == 0o755 and not path.startswith("scripts/")):
            raise AdmissionError(f"unexpected diff mode: {path}")
        text = body.decode("utf-8")
        if re.search(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|github_pat_|\\bsk-[A-Za-z0-9_-]{20,}", text):
            raise AdmissionError(f"secret-like material in diff: {path}")
        files.append({"path": path, "digest": digest_bytes(body)})
    output.mkdir(parents=True, exist_ok=True)
    patch = output / "sealed.patch"
    tracked_patch = subprocess.run(
        ["git", "diff", "--binary", "--full-index", base, "--"],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout
    parts = [tracked_patch]
    for path in untracked:
        result_diff = subprocess.run(
            ["git", "diff", "--binary", "--no-index", "/dev/null", path],
            cwd=repo,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
        )
        if result_diff.returncode not in {0, 1}:
            raise AdmissionError(f"cannot serialize {path}")
        parts.append(result_diff.stdout)
    patch.write_text("".join(parts))
    manifest = {
        "schemaVersion": 1,
        "baseSha": base,
        "actionKey": plan["actionKey"],
        "patchDigest": digest_file(patch),
        "files": files,
    }
    write(output / "patch-manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    admit_parser = sub.add_parser("admit")
    for name in ("plan", "contract", "shared", "phase", "event-contract", "output"):
        admit_parser.add_argument(f"--{name}", required=True, type=pathlib.Path)
    admit_parser.add_argument("--policy-digest", required=True)
    admit_parser.add_argument("--mise-head", required=True)
    seal_parser = sub.add_parser("seal")
    seal_parser.add_argument("--repo", type=pathlib.Path, default=pathlib.Path.cwd())
    seal_parser.add_argument("--base", required=True)
    for name in ("plan", "result", "contract", "output"):
        seal_parser.add_argument(f"--{name}", required=True, type=pathlib.Path)
    args = parser.parse_args()
    try:
        if args.command == "admit":
            value = admit(
                load(args.plan), load(args.contract), args.shared, args.phase,
                args.event_contract, args.policy_digest, args.mise_head,
            )
            write(args.output, value)
        else:
            value = seal(
                args.repo, args.base, load(args.plan), load(args.result),
                load(args.contract), args.output,
            )
        print(json.dumps(value))
        return 0
    except (AdmissionError, OSError, subprocess.CalledProcessError) as error:
        print(f"mise maintenance admission rejected: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
