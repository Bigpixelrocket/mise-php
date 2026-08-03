"""Deterministic admission and sealing for repository-scoped mise changes.

This module imports from `autorelease.consumer`, so it is reached only as a package:
`scripts/admit-autorelease-plan`, `scripts/seal-autorelease-patch` and
`scripts/verify-merge-admission` are its command-line entry points.
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

# The admissible action-key alphabet is defined once, beside the filename mapping that
# both repositories derive record and branch names from.
from autorelease.consumer import ACTION_KEY_RE


PROTECTED_PATHS = pathlib.Path(__file__).with_name("protected-paths.json")
try:
    PROTECTED = tuple(json.loads(PROTECTED_PATHS.read_text())["patterns"])
except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
    raise RuntimeError(f"cannot load protected paths: {error}") from error
PROHIBITED = {"merge", "push", "tag", "release", "publish", "workflow_permissions", "secret_access"}
SECRET_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|\bgh[opusr]_[A-Za-z0-9]{30,}\b"
    r"|\bsk-[A-Za-z0-9_-]{20,}\b"
)
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REQUIRED_PLAN_CHECKS = ["Plugin contract"]
READINESS_RECORD_KEYS = {
    "schemaVersion", "actionKey", "state", "ready", "phpBinPolicyCommit",
    "policyDigest", "policyInvariantsDigest", "misePhpCommit",
    "evidenceDigests", "recordedAt",
}


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


def contained_path(root: pathlib.Path, value: Any, label: str) -> pathlib.Path:
    if not isinstance(value, str) or not value:
        raise AdmissionError(f"{label} is missing")
    relative = pathlib.PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise AdmissionError(f"unsafe {label}: {value}")
    resolved_root = root.resolve()
    resolved = (resolved_root / pathlib.Path(*relative.parts)).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise AdmissionError(f"unsafe {label}: {value}")
    return resolved


def protected(path: str) -> bool:
    """Match a repository path against the protected patterns.

    fnmatchcase, not fnmatch: fnmatch runs os.path.normcase first, which makes the
    answer depend on the host platform. Git paths are case-sensitive bytes and this
    gate decides admission, so the comparison has to be the same everywhere.
    """
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in PROTECTED)


def validate_readiness_record(record: Any) -> None:
    """Exact-shape check for records produced by consumer.readiness()."""
    if not isinstance(record, dict) or set(record) != READINESS_RECORD_KEYS:
        raise AdmissionError("readiness record has unexpected shape")
    if record["schemaVersion"] != 1 or record["state"] != "mise_ready" or record["ready"] is not True:
        raise AdmissionError("readiness record has invalid state")
    if not ACTION_KEY_RE.fullmatch(str(record["actionKey"])):
        raise AdmissionError("readiness record has invalid action key")
    for key in ("phpBinPolicyCommit", "misePhpCommit"):
        if not re.fullmatch(r"[0-9a-f]{40}", str(record[key])):
            raise AdmissionError(f"readiness record {key} is not an exact SHA")
    for key in ("policyDigest", "policyInvariantsDigest"):
        if not SHA256_RE.fullmatch(str(record[key])):
            raise AdmissionError(f"readiness record {key} is not a digest")
    digests = record["evidenceDigests"]
    if (
        not isinstance(digests, list)
        or not digests
        or digests != sorted(digests)
        or not all(isinstance(item, str) and SHA256_RE.fullmatch(item) for item in digests)
    ):
        raise AdmissionError("readiness record evidence digests are invalid")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", str(record["recordedAt"])):
        raise AdmissionError("readiness record timestamp is invalid")


def validate_assessment(assessment: dict, contract: dict, digests: dict) -> None:
    if not isinstance(contract, dict):
        raise AdmissionError("task contract must be an object")
    criteria = contract.get("completionCriteria")
    if not isinstance(criteria, list) or not criteria or not all(isinstance(item, dict) for item in criteria):
        raise AdmissionError("completion criteria must be non-empty objects")
    criterion_ids = [item.get("id") for item in criteria]
    if not all(isinstance(item, str) and item for item in criterion_ids) or len(criterion_ids) != len(set(criterion_ids)):
        raise AdmissionError("completion criterion ids are missing or duplicated")
    if assessment.get("instructionDigests") != digests:
        raise AdmissionError("instruction digests changed")
    expected = set(criterion_ids)
    results = assessment.get("criteria", [])
    if not isinstance(results, list) or not all(isinstance(item, dict) for item in results):
        raise AdmissionError("criterion results must be objects")
    if {item.get("id") for item in results} != expected or len(results) != len(expected):
        raise AdmissionError("criterion results do not exactly match the contract")
    all_passed = (
        assessment.get("phaseStatus") == "complete"
        and all(item.get("status") == "passed" and item.get("evidence") for item in results)
        and assessment.get("unresolved") == []
    )
    if (assessment.get("goNoGo") == "go") != all_passed:
        raise AdmissionError("assessment go/no-go is internally inconsistent")


def resolve_pointer(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise AdmissionError("invalid JSON pointer")
    value = document
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        try:
            value = value[int(token)] if isinstance(value, list) else value[token]
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise AdmissionError("evidence JSON pointer does not resolve") from error
    return value


def admit(
    plan: dict,
    contract: dict,
    shared: pathlib.Path,
    phase: pathlib.Path,
    event: pathlib.Path,
    capture_manifest: pathlib.Path,
    policy_digest: str,
    invariants_digest: str,
    mise_head: str,
) -> dict:
    if plan.get("schemaVersion") != 1:
        raise AdmissionError("unsupported plan version")
    if plan.get("action") not in {
        "no_change", "new_patch", "new_branch", "branch_eol", "repair",
        "reconcile_partial", "blocked", "needs_human",
    }:
        raise AdmissionError("invalid autorelease action")
    if plan.get("action") in {"blocked", "needs_human"}:
        raise AdmissionError("no-go plan cannot advance")
    action_key = plan.get("actionKey", "")
    if not ACTION_KEY_RE.fullmatch(action_key):
        raise AdmissionError("invalid autorelease action key")
    if action_key != contract.get("actionKey"):
        raise AdmissionError("plan action key changed from the event contract")
    if action_key.startswith("new_branch:") and plan.get("action") != "new_branch":
        raise AdmissionError("new-branch action key has inconsistent classification")
    if action_key.startswith("branch_eol:") and plan.get("action") != "branch_eol":
        raise AdmissionError("EOL action key has inconsistent classification")
    digests = {
        "shared": digest_file(shared),
        "phaseTemplate": digest_file(phase),
        "eventContract": digest_file(event),
    }
    if plan.get("agentContract", {}).get("instructionDigests") != digests:
        raise AdmissionError("plan instruction digests changed")
    validate_assessment(plan.get("completionAssessment", {}), contract, digests)
    preconditions = plan.get("preconditions", {})
    if preconditions != contract.get("preconditions"):
        raise AdmissionError("plan preconditions changed from the event contract")
    if preconditions.get("misePhpHead") != mise_head:
        raise AdmissionError("mise-php base precondition is stale")
    if preconditions.get("supportPolicyDigest") != policy_digest:
        raise AdmissionError("php-bin policy precondition is stale")
    if preconditions.get("policyInvariantsDigest") != invariants_digest:
        raise AdmissionError("php-bin invariant precondition is stale")
    if preconditions.get("operatorState") != "enabled" or not re.fullmatch(
        r"[0-9a-f]{40}", preconditions.get("phpBinOperatorCommit", "")
    ):
        raise AdmissionError("php-bin operator precondition is not enabled and exact")
    capture_document = load(capture_manifest)
    captures = capture_document.get("captures", [])
    if capture_document.get("schemaVersion") != 1 or not isinstance(captures, list):
        raise AdmissionError("invalid policy capture manifest")
    captures_by_id = {item.get("captureId"): item for item in captures if isinstance(item, dict)}
    if set(captures_by_id) != {
        "php_bin_policy_selector", "php_bin_state", "support_policy", "policy_invariants"
    }:
        raise AdmissionError("policy capture set is incomplete or ambiguous")
    if captures_by_id["support_policy"].get("digest") != policy_digest:
        raise AdmissionError("captured support policy digest changed")
    if captures_by_id["policy_invariants"].get("digest") != invariants_digest:
        raise AdmissionError("captured policy invariants digest changed")
    selector_body = load(
        contained_path(
            capture_manifest.parent,
            captures_by_id["php_bin_policy_selector"].get("bodyPath"),
            "captured policy selector path",
        )
    )
    commit_body = load(
        contained_path(
            capture_manifest.parent,
            captures_by_id["php_bin_state"].get("bodyPath"),
            "captured php-bin state path",
        )
    )
    if (
        not isinstance(selector_body, list)
        or len(selector_body) != 1
        or selector_body[0].get("sha") != preconditions.get("phpBinPolicyCommit")
        or commit_body.get("sha") != preconditions.get("phpBinPolicyCommit")
    ):
        raise AdmissionError("captured php-bin policy commit changed")
    evidence = plan.get("evidence", [])
    if not isinstance(evidence, list) or len(evidence) != len(captures_by_id):
        raise AdmissionError("plan does not cite the complete captured policy set")
    evidence_refs = set()
    for index, item in enumerate(evidence):
        capture = captures_by_id.get(item.get("captureId")) if isinstance(item, dict) else None
        if capture is None or item.get("digest") != capture.get("digest"):
            raise AdmissionError("plan evidence does not match the captured policy")
        body_path = contained_path(
            capture_manifest.parent,
            capture.get("bodyPath"),
            "captured policy body path",
        )
        if not body_path.is_file() or digest_file(body_path) != capture.get("digest"):
            raise AdmissionError("captured policy body changed")
        locator = item.get("locator", {})
        if locator.get("kind") != "json_pointer":
            raise AdmissionError("policy evidence requires a JSON pointer")
        resolve_pointer(load(body_path), locator.get("value", ""))
        evidence_refs.add(f"evidence[{index}]")
    precondition_refs = {f"preconditions.{key}" for key in preconditions}
    for criterion in plan.get("completionAssessment", {}).get("criteria", []):
        for reference in criterion.get("evidence", []):
            if reference not in evidence_refs and reference not in precondition_refs:
                raise AdmissionError("completion evidence reference does not resolve")
    repositories = plan.get("repositories")
    if not isinstance(repositories, list) or "mise-php" not in repositories or any(
        value not in {"php-bin", "mise-php"} for value in repositories
    ):
        raise AdmissionError("plan repository authority is invalid")
    if plan.get("editsRequired") is not True:
        raise AdmissionError("changed accepted policy requires a synchronized snapshot edit")
    if plan.get("requiredChecks") != REQUIRED_PLAN_CHECKS:
        raise AdmissionError("required deterministic checks changed")
    if plan.get("risk") not in {"routine", "compatibility", "lifecycle", "recovery", "policy-sensitive"}:
        raise AdmissionError("invalid plan risk")
    if plan.get("action") in {"new_branch", "branch_eol"} and plan.get("risk") != "lifecycle":
        raise AdmissionError("lifecycle coordination requires lifecycle risk")
    allowed = plan.get("allowedPaths", {})
    if not isinstance(allowed, dict):
        raise AdmissionError("allowed paths must be an object")
    flattened = []
    for patterns in allowed.values():
        if not isinstance(patterns, list):
            raise AdmissionError("allowed path set must be an array")
        for pattern in patterns:
            pure = pathlib.PurePosixPath(pattern)
            if pure.is_absolute() or ".." in pure.parts:
                raise AdmissionError(f"unsafe allowed path: {pattern}")
            if protected(pattern):
                raise AdmissionError(f"runtime plan admits protected path: {pattern}")
            flattened.append(pattern)
    if not any(fnmatch.fnmatchcase("support-snapshot.json", pattern) for pattern in flattened):
        raise AdmissionError("policy synchronization does not admit the generated support snapshot")
    # Sealing rejects a snapshot edit whose lib/policy.lua was not regenerated, so a
    # plan that cannot carry the regenerated file is unsatisfiable rather than risky.
    if not any(fnmatch.fnmatchcase("lib/policy.lua", pattern) for pattern in flattened):
        raise AdmissionError("policy synchronization does not admit the generated lib/policy.lua")
    operations = plan.get("agentOperations")
    if not isinstance(operations, list) or not all(isinstance(item, str) for item in operations):
        raise AdmissionError("agent operations must be an array of strings")
    if set(operations) & PROHIBITED:
        raise AdmissionError("runtime plan grants irreversible authority")
    budgets = plan.get("budgets")
    if not isinstance(budgets, dict) or not budgets:
        raise AdmissionError("plan must declare reviewed budgets")
    for field, upper, label in (
        ("maxModelCalls", 5, "model-call"),
        ("maxRetries", 3, "retry"),
        ("timeoutMinutes", 60, "time"),
    ):
        value = budgets.get(field)
        if not isinstance(value, int) or isinstance(value, bool):
            raise AdmissionError(f"{field} must be an integer")
        if not 0 < value <= upper:
            raise AdmissionError(f"{label} budget is outside reviewed bound")
    return {
        "admitted": True,
        "actionKey": plan["actionKey"],
        "planDigest": digest_bytes(canonical(plan)),
        "instructionDigests": digests,
    }


def seal(
    repo: pathlib.Path,
    base: str,
    plan: dict,
    result: dict,
    contract: dict,
    policy_path: pathlib.Path,
    output: pathlib.Path,
) -> dict:
    digests = plan["agentContract"]["instructionDigests"]
    validate_assessment(result, contract, digests)
    if result["goNoGo"] != "go":
        raise AdmissionError("implementation result is no-go")
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
    admit_parser = sub.add_parser("admit")
    for name in ("plan", "contract", "shared", "phase", "event-contract", "output"):
        admit_parser.add_argument(f"--{name}", required=True, type=pathlib.Path)
    admit_parser.add_argument("--capture-manifest", required=True, type=pathlib.Path)
    admit_parser.add_argument("--policy-digest", required=True)
    admit_parser.add_argument("--invariants-digest", required=True)
    admit_parser.add_argument("--mise-head", required=True)
    seal_parser = sub.add_parser("seal")
    seal_parser.add_argument("--repo", type=pathlib.Path, default=pathlib.Path.cwd())
    seal_parser.add_argument("--base", required=True)
    for name in ("plan", "result", "contract", "output"):
        seal_parser.add_argument(f"--{name}", required=True, type=pathlib.Path)
    seal_parser.add_argument("--policy", required=True, type=pathlib.Path)
    verify_parser = sub.add_parser("verify-merge")
    verify_parser.add_argument("--repo", type=pathlib.Path, default=pathlib.Path.cwd())
    verify_parser.add_argument("--head", required=True)
    for name in ("manifest", "checks", "preconditions", "current"):
        verify_parser.add_argument(f"--{name}", required=True, type=pathlib.Path)
    args = parser.parse_args()
    try:
        if args.command == "admit":
            value = admit(
                load(args.plan), load(args.contract), args.shared, args.phase,
                args.event_contract, args.capture_manifest, args.policy_digest,
                args.invariants_digest, args.mise_head,
            )
            write(args.output, value)
        elif args.command == "seal":
            value = seal(
                args.repo, args.base, load(args.plan), load(args.result),
                load(args.contract), args.policy, args.output,
            )
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
