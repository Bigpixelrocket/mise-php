"""Deterministic sealing and merge admission for policy synchronizations.

A synchronization changes only the generated support snapshot and the Lua policy
table derived from it. Sealing proves the diff is exactly that, bound to the captured
policy the plan names, and the merge gate proves the merged commit is exactly the
sealed and validated one.

This module imports from `autorelease.consumer`, so it is reached only as a package:
`scripts/seal-autorelease-patch` and `scripts/verify-merge-admission` are its
command-line entry points.
"""

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

# The readiness record shape, and the action-key alphabet inside it, are defined
# once, beside the filename mapping both repositories derive record and branch
# names from.
from autorelease.consumer import ConsumerError, check_readiness_record


PROTECTED_PATHS = pathlib.Path(__file__).with_name("protected-paths.json")
try:
    PROTECTED = tuple(json.loads(PROTECTED_PATHS.read_text())["patterns"])
except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
    raise RuntimeError(f"cannot load protected paths: {error}") from error
SECRET_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|\bgh[opusr]_[A-Za-z0-9]{30,}\b"
    r"|\bsk-[A-Za-z0-9_-]{20,}\b"
)


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
    """Match a repository path against the protected patterns.

    fnmatchcase, not fnmatch: fnmatch runs os.path.normcase first, which makes the
    answer depend on the host platform. Git paths are case-sensitive bytes and this
    gate decides admission, so the comparison has to be the same everywhere.
    """
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in PROTECTED)


def validate_readiness_record(record: Any) -> None:
    """Exact-shape check for records produced by consumer.readiness()."""
    try:
        check_readiness_record(record)
    except ConsumerError as error:
        raise AdmissionError(str(error)) from error


def seal(
    repo: pathlib.Path,
    base: str,
    plan: dict,
    policy_path: pathlib.Path,
    output: pathlib.Path,
) -> dict:
    """Seal the regenerated snapshot and Lua table against the plan's exact base.

    Only admitted, unprotected UTF-8 text files may change; the snapshot must equal
    the captured policy the plan is bound to, and lib/policy.lua must be exactly its
    generated form, so neither file can move without the other.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", base or ""):
        raise AdmissionError("base is not an exact commit SHA")
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
        if protected(path) or not any(fnmatch.fnmatchcase(path, pattern) for pattern in allowed):
            raise AdmissionError(f"forbidden diff path: {path}")
        if candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_size > 2_000_000:
            raise AdmissionError(f"unsupported diff entry: {path}")
        body = candidate.read_bytes()
        if b"\0" in body:
            raise AdmissionError(f"binary diff entry: {path}")
        mode = candidate.stat().st_mode & 0o777
        if mode not in {0o644, 0o755} or (mode == 0o755 and not path.startswith("scripts/")):
            raise AdmissionError(f"unexpected diff mode: {path}")
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise AdmissionError(f"diff entry is not valid UTF-8: {path}") from error
        if SECRET_RE.search(text):
            raise AdmissionError(f"secret-like material in diff: {path}")
        if path == "support-snapshot.json":
            try:
                snapshot = json.loads(text)
            except json.JSONDecodeError as error:
                raise AdmissionError("support snapshot is not valid JSON") from error
            preconditions = plan.get("preconditions", {})
            policy = load(policy_path)
            if digest_file(policy_path) != preconditions.get("supportPolicyDigest"):
                raise AdmissionError("captured support policy changed after admission")
            if snapshot.get("phpBinPolicyCommit") != preconditions.get("phpBinPolicyCommit"):
                raise AdmissionError("support snapshot commit is not the admitted php-bin policy commit")
            if snapshot.get("policyDigest") != preconditions.get("supportPolicyDigest"):
                raise AdmissionError("support snapshot digest is not the admitted php-bin policy digest")
            if snapshot.get("policyInvariantsDigest") != preconditions.get("policyInvariantsDigest"):
                raise AdmissionError("support snapshot invariant digest changed")
            branches = snapshot.get("maintainedBranches")
            if not (
                isinstance(branches, list)
                and all(re.fullmatch(r"\d+\.\d+", value) for value in branches)
                and branches == sorted(set(branches), key=lambda value: tuple(map(int, value.split("."))))
            ):
                raise AdmissionError("support snapshot branches are invalid or non-canonical")
            if branches != policy.get("maintainedBranches"):
                raise AdmissionError("support snapshot branches do not equal the captured policy")
            if set(snapshot) != {
                "schemaVersion", "phpBinPolicyCommit", "policyDigest",
                "policyInvariantsDigest", "maintainedBranches", "generated",
            } or snapshot.get("schemaVersion") != 1 or snapshot.get("generated") is not True:
                raise AdmissionError("support snapshot has unknown, missing, or invalid fields")
        files.append({"path": path, "digest": digest_bytes(body), "mode": oct(mode)})
    # The plugin filters branches through the generated lib/policy.lua, so either file
    # changing alone would ship a filter that disagrees with the accepted snapshot.
    if "support-snapshot.json" in paths or "lib/policy.lua" in paths:
        maintained = load(repo / "support-snapshot.json").get("maintainedBranches", [])
        expected_policy_lines = [
            "-- Generated by scripts/generate-policy-lua from support-snapshot.json.",
            "-- Do not edit by hand; regenerate when the snapshot changes.",
            "return {",
            "    maintained = {",
            *[f'        "{branch}",' for branch in maintained],
            "    },",
            "}",
        ]
        policy_lua = repo / "lib" / "policy.lua"
        if not policy_lua.is_file() or policy_lua.read_text().splitlines() != expected_policy_lines:
            raise AdmissionError("support snapshot changed without regenerating lib/policy.lua")
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
            ["git", "diff", "--binary", "--no-index", "--", "/dev/null", path],
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


def git(repo: pathlib.Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=repo, check=True, text=True, stdout=subprocess.PIPE
    ).stdout.strip()


def verify_merge(
    repo: pathlib.Path,
    expected_head: str,
    manifest: dict,
    checks: dict,
    preconditions: dict,
    current: dict,
) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", expected_head or ""):
        raise AdmissionError("expected head is not an exact commit SHA")
    if git(repo, "rev-parse", "HEAD") != expected_head:
        raise AdmissionError("PR head does not equal validated SHA")
    if not checks or any(value != "success" for value in checks.values()):
        raise AdmissionError("required checks did not succeed")
    if preconditions != current:
        raise AdmissionError("merge preconditions changed")
    base = manifest.get("baseSha", "")
    if not re.fullmatch(r"[0-9a-f]{40}", base):
        raise AdmissionError("sealed manifest has no exact base SHA")
    if git(repo, "rev-list", "--parents", "-n", "1", expected_head).split() != [expected_head, base]:
        raise AdmissionError("validated commit is not a single commit on the sealed base")
    records = manifest.get("files", [])
    if not isinstance(records, list):
        raise AdmissionError("sealed manifest files are invalid")
    paths = {item.get("path") for item in records if isinstance(item, dict)}
    if len(paths) != len(records) or None in paths:
        raise AdmissionError("sealed manifest paths are invalid")
    actual = set(
        git(repo, "diff", "--name-only", "--diff-filter=ACDMRTUXB", base, expected_head, "--").splitlines()
    )
    if actual != paths:
        raise AdmissionError("final diff does not equal the sealed manifest")
    for record in records:
        path = record["path"]
        candidate = repo / path
        if protected(path):
            raise AdmissionError(f"sealed manifest contains protected path: {path}")
        if not candidate.is_file() or digest_file(candidate) != record.get("digest"):
            raise AdmissionError(f"validated file changed: {path}")
        if oct(candidate.stat().st_mode & 0o777) != record.get("mode"):
            raise AdmissionError(f"validated file mode changed: {path}")
    return {"admitted": True, "headSha": expected_head}


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    seal_parser = sub.add_parser("seal")
    seal_parser.add_argument("--repo", type=pathlib.Path, default=pathlib.Path.cwd())
    seal_parser.add_argument("--base", required=True)
    for name in ("plan", "output"):
        seal_parser.add_argument(f"--{name}", required=True, type=pathlib.Path)
    seal_parser.add_argument("--policy", required=True, type=pathlib.Path)
    verify_parser = sub.add_parser("verify-merge")
    verify_parser.add_argument("--repo", type=pathlib.Path, default=pathlib.Path.cwd())
    verify_parser.add_argument("--head", required=True)
    for name in ("manifest", "checks", "preconditions", "current"):
        verify_parser.add_argument(f"--{name}", required=True, type=pathlib.Path)
    args = parser.parse_args()
    try:
        if args.command == "seal":
            value = seal(args.repo, args.base, load(args.plan), args.policy, args.output)
        else:
            value = verify_merge(
                args.repo,
                args.head,
                load(args.manifest),
                load(args.checks),
                load(args.preconditions),
                load(args.current),
            )
        print(json.dumps(value))
        return 0
    except (AdmissionError, OSError, subprocess.CalledProcessError) as error:
        print(f"mise autorelease admission rejected: {error}", file=sys.stderr)
        return 1
