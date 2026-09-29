#!/usr/bin/env python3
"""Deterministic consumption of the accepted php-bin support policy.

php-bin classifies PHP lifecycle events and accepts the support policy; this
module never looks at upstream PHP data. It captures that policy at the exact
commit that last changed it, compares it with the local snapshot, binds one
synchronization to the exact captured bytes and commits, regenerates
`support-snapshot.json` from it, and writes the exact-commit readiness record
php-bin requires before it publishes a new branch.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import re
import sys
import urllib.request
import urllib.error
import urllib.parse
from typing import Any


ACTION_KEY_RE = re.compile(
    r"^(new_patch:\d+\.\d+\.\d+|new_branch:\d+\.\d+|"
    r"branch_eol:\d+\.\d+:\d{4}-\d{2}-\d{2}|"
    r"recipe_rebuild:\d+\.\d+\.\d+:[1-9]\d*|"
    r"(?:source_unhealthy|health_failed|policy_failure):[0-9a-f]{8,64})$"
)
POLICY_COMMIT_SELECTOR_URL = (
    "https://api.github.com/repos/bigpixelrocket/php-bin/commits"
    "?sha=main&path=support-policy.json&per_page=1"
)
POLICY_COMMIT_ROOT = "https://api.github.com/repos/bigpixelrocket/php-bin/commits"
RAW_ROOT = "https://raw.githubusercontent.com/bigpixelrocket/php-bin"
POLICY_CAPTURE_IDS = frozenset({"php_bin_policy_selector", "php_bin_state", "support_policy", "policy_invariants"})
# The only paths a synchronization writes: the snapshot, and the Lua policy table
# scripts/generate-policy-lua derives from it.
SYNCHRONIZED_PATHS = ["lib/policy.lua", "support-snapshot.json"]
READINESS_RECORD_KEYS = frozenset({
    "schemaVersion", "actionKey", "state", "ready", "phpBinPolicyCommit",
    "policyDigest", "policyInvariantsDigest", "misePhpCommit",
    "evidenceDigests", "recordedAt",
})
# The fields that bind a readiness record to one accepted policy. A record whose
# other fields are valid but whose binding names another policy was written for a
# policy that has since been superseded.
READINESS_BINDING = ("phpBinPolicyCommit", "policyDigest", "policyInvariantsDigest")
SNAPSHOT_FIELDS = (
    "schemaVersion",
    "phpBinPolicyCommit",
    "policyDigest",
    "policyInvariantsDigest",
    "maintainedBranches",
    "generated",
)


class ConsumerError(RuntimeError):
    pass


class CaptureAbsent(ConsumerError):
    """The capture URL resolved but the document is not published at that path."""


class RestrictedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        old = urllib.parse.urlparse(req.full_url)
        new = urllib.parse.urlparse(newurl)
        if new.scheme != "https" or new.hostname != old.hostname:
            raise urllib.error.HTTPError(newurl, code, "cross-host redirect rejected", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


ACTION_FILENAME_MAP = str.maketrans({":": "-", "/": "-"})


def action_filename(action_key: str, suffix: str = ".json") -> str:
    """Return the single file or branch name an action key may occupy.

    php-bin names event records from an action key with exactly this mapping, and the
    readiness record it reads back is matched by name, so the two repositories share one
    definition of it. The key comes from a captured policy or plan and reaches shell
    arguments and repository paths, so its alphabet is re-asserted at this boundary.
    """
    if not ACTION_KEY_RE.fullmatch(action_key):
        raise ConsumerError(f"invalid action key: {action_key}")
    return action_key.translate(ACTION_FILENAME_MAP) + suffix


def now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def load(path: pathlib.Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ConsumerError(f"cannot load {path}: {error}") from error


def write(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(canonical(value))
    temporary.replace(path)


def fetch_url(url: str, output: pathlib.Path) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"api.github.com", "raw.githubusercontent.com"}:
        raise ConsumerError("policy capture URL is outside the reviewed HTTPS allowlist")
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "bigpixelrocket-autorelease/1"},
    )
    opener = urllib.request.build_opener(RestrictedRedirect)
    last_error: Exception | None = None
    for _attempt in range(3):
        try:
            with opener.open(request, timeout=30) as response:
                body = response.read(1_000_001)
                if len(body) > 1_000_000:
                    raise ConsumerError("php-bin policy response is too large")
                json.loads(body)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(body)
                return {
                    "url": url,
                    "retrievedAt": now(),
                    "status": response.status,
                    "contentType": response.headers.get("Content-Type"),
                    "etag": response.headers.get("ETag"),
                    "lastModified": response.headers.get("Last-Modified"),
                    "digest": digest(body),
                    "bodyPath": output.name,
                }
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise CaptureAbsent(f"policy capture path is not published: {url}") from error
            last_error = error
        except (OSError, urllib.error.URLError, json.JSONDecodeError, ConsumerError) as error:
            last_error = error
    raise ConsumerError(f"policy capture failed after bounded retries: {type(last_error).__name__}")


def fetch_first_url(urls: tuple[str, ...], output: pathlib.Path) -> dict[str, Any]:
    """Capture the first published path, recording which one supplied the bytes.

    Captures pin to the commit that last changed `support-policy.json`, because
    that document binds itself to its invariants by digest and the two only
    agree within the tree php-bin reviewed them in. Pinning to the newest change
    of either document instead would pair new invariants with a policy still
    carrying the previous digest, which `compare` rejects.

    That commit can be arbitrarily old, and php-bin has moved this file before,
    so the invariants path is whatever the layout was at the time. This list is
    permanent compatibility with historical layouts, newest first: extend it on
    the next move rather than expecting to shorten it. Only a 404 falls through,
    so a transport failure still raises instead of reaching for an older
    document.
    """
    for url in urls[:-1]:
        try:
            return fetch_url(url, output)
        except CaptureAbsent:
            continue
    return fetch_url(urls[-1], output)


def pinned_policy_urls(commit_sha: str) -> tuple[str, tuple[str, ...]]:
    if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
        raise ConsumerError("php-bin main state has no exact commit")
    return (
        f"{RAW_ROOT}/{commit_sha}/support-policy.json",
        (
            f"{RAW_ROOT}/{commit_sha}/autorelease/policy-invariants.json",
            f"{RAW_ROOT}/{commit_sha}/maintenance/policy-invariants.json",
        ),
    )


def fetch_policy_set(
    policy_output: pathlib.Path,
    invariants_output: pathlib.Path,
    commit_output: pathlib.Path,
) -> list[dict[str, Any]]:
    selector_output = commit_output.with_name(f"{commit_output.stem}-selector{commit_output.suffix}")
    selector_capture = {
        "captureId": "php_bin_policy_selector",
        **fetch_url(POLICY_COMMIT_SELECTOR_URL, selector_output),
    }
    selected = load(selector_output)
    if not isinstance(selected, list) or len(selected) != 1:
        raise ConsumerError("php-bin policy commit selector is empty or ambiguous")
    commit_sha = selected[0].get("sha", "")
    policy_url, invariants_urls = pinned_policy_urls(commit_sha)
    commit_capture = {
        "captureId": "php_bin_state",
        **fetch_url(f"{POLICY_COMMIT_ROOT}/{commit_sha}", commit_output),
    }
    return [
        selector_capture,
        commit_capture,
        {"captureId": "support_policy", **fetch_url(policy_url, policy_output)},
        {"captureId": "policy_invariants", **fetch_first_url(invariants_urls, invariants_output)},
    ]


def check_readiness_record(record: Any) -> None:
    """Exact-shape check for records produced by readiness().

    Raises ConsumerError naming the first problem. The merge gate and the
    comparison both use it, so a record either repository could write back is
    judged the same way everywhere.
    """
    if not isinstance(record, dict) or set(record) != READINESS_RECORD_KEYS:
        raise ConsumerError("readiness record has unexpected shape")
    if record["schemaVersion"] != 1 or record["state"] != "mise_ready" or record["ready"] is not True:
        raise ConsumerError("readiness record has invalid state")
    if not ACTION_KEY_RE.fullmatch(str(record["actionKey"])):
        raise ConsumerError("readiness record has invalid action key")
    for key in ("phpBinPolicyCommit", "misePhpCommit"):
        if not re.fullmatch(r"[0-9a-f]{40}", str(record[key])):
            raise ConsumerError(f"readiness record {key} is not an exact SHA")
    for key in ("policyDigest", "policyInvariantsDigest"):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(record[key])):
            raise ConsumerError(f"readiness record {key} is not a digest")
    digests = record["evidenceDigests"]
    if (
        not isinstance(digests, list)
        or not digests
        or digests != sorted(digests)
        or not all(isinstance(item, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", item) for item in digests)
    ):
        raise ConsumerError("readiness record evidence digests are invalid")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", str(record["recordedAt"])):
        raise ConsumerError("readiness record timestamp is invalid")


def readiness_state(
    action_key: str,
    php_bin_commit: str,
    policy_digest: str,
    policy_invariants_digest: str,
    readiness_dir: pathlib.Path,
) -> dict[str, Any]:
    """Classify the readiness record for a synchronized lifecycle policy.

    A synchronization merges the snapshot first and records readiness afterwards, in a
    separate pull request, so a run that stops between the two leaves the snapshot
    current with no matching record. The returned `state` is:

    - `not_required`: a `bootstrap` policy needs no record.
    - `recorded`: the record is valid and bound to exactly this policy.
    - `missing`: there is no record; the run must write it.
    - `superseded`: a valid record for this action names another policy commit or
      digest, because php-bin accepted a newer policy for the same action. The run
      replaces it through the normal readiness pull request, bound to this policy.
    - `blocked`: the record is not a regular file, unreadable, malformed, not
      ready, or names another action. Nothing automated wrote it, so replacing it
      could override a deliberate owner edit; the run raises it for the owner
      instead.

    `record` is the record's repository path, `problem` says what is wrong with a
    blocked record and `recordDigest` identifies its exact bytes, and `mismatched`
    lists the binding fields a superseded record names differently. Exact-commit
    semantics are unchanged: only a record bound to this exact policy is
    `recorded`, and php-bin reads nothing else as ready.
    """
    if action_key == "bootstrap":
        return {"state": "not_required"}
    path = readiness_dir / action_filename(action_key)
    result: dict[str, Any] = {"record": f"readiness/{path.name}"}
    # A symlink or directory at the record's path is nothing automation writes, and
    # its bytes cannot be read the way a record's are; its digest, like that of a
    # record that cannot be read, is of no bytes.
    if path.is_symlink() or (path.exists() and not path.is_file()):
        return {
            **result,
            "state": "blocked",
            "problem": "readiness record is not a regular file",
            "recordDigest": digest(b""),
        }
    if not path.exists():
        return {**result, "state": "missing"}
    try:
        content = path.read_bytes()
    except OSError as error:
        return {
            **result,
            "state": "blocked",
            "problem": f"readiness record cannot be read: {error.strerror}",
            "recordDigest": digest(b""),
        }
    try:
        record = json.loads(content.decode("utf-8"))
        check_readiness_record(record)
        if record["actionKey"] != action_key:
            raise ConsumerError("readiness record names another action key")
    except (ValueError, ConsumerError) as error:
        return {
            **result,
            "state": "blocked",
            "problem": str(error),
            "recordDigest": digest(content),
        }
    expected = {
        "phpBinPolicyCommit": php_bin_commit,
        "policyDigest": policy_digest,
        "policyInvariantsDigest": policy_invariants_digest,
    }
    mismatched = [key for key in READINESS_BINDING if record[key] != expected[key]]
    if mismatched:
        return {**result, "state": "superseded", "mismatched": mismatched}
    return {**result, "state": "recorded"}


def same_readiness(existing: Any, candidate: Any) -> None:
    """Prove an existing record states exactly what a fresh one for this run would.

    A rerun of the job that records readiness finds the branch its earlier attempt
    pushed. It may reuse that branch only when the record there is valid and equal to
    the one it would write now in every field but `recordedAt`; anything else is not
    this run's record, and the rerun stops rather than rewrite a branch.
    """
    check_readiness_record(existing)
    check_readiness_record(candidate)
    differing = sorted(
        key for key in READINESS_RECORD_KEYS - {"recordedAt"} if existing[key] != candidate[key]
    )
    if differing:
        raise ConsumerError(f"existing readiness record differs from this run's: {', '.join(differing)}")


# Each readiness state maps to the comparison trigger the consumer workflow acts on.
READINESS_TRIGGERS = {
    "missing": "readiness_pending",
    "superseded": "readiness_superseded",
    "blocked": "readiness_blocked",
}


def compare(
    policy: pathlib.Path,
    invariants: pathlib.Path,
    policy_commit: pathlib.Path,
    snapshot: pathlib.Path,
    readiness_dir: pathlib.Path | None = None,
) -> dict[str, Any]:
    """Compare the captured policy with the local snapshot and name the next step.

    The trigger is `policy_changed` when the snapshot must be synchronized. When the
    snapshot is current, the readiness record in `readiness_dir` decides:
    `readiness_pending` when the lifecycle action has no record, `readiness_superseded`
    when its record is bound to an earlier policy, `readiness_blocked` when its record
    needs the owner, and `quiet` otherwise; the decision then carries the record's
    `readiness` state. Without `readiness_dir` the record is not consulted.
    """
    policy_digest = digest(policy.read_bytes())
    policy_document = load(policy)
    invariants_document = load(invariants)
    invariants_digest = digest(invariants.read_bytes())
    if set(policy_document) != {
        "schemaVersion",
        "policyInvariantsDigest",
        "maintainedBranches",
        "sourceEvidenceDigests",
        "actionKey",
        "acceptedAt",
    }:
        raise ConsumerError("captured php-bin support policy has unknown or missing fields")
    if policy_document.get("schemaVersion") != 1:
        raise ConsumerError("unsupported captured php-bin support policy version")
    if set(invariants_document) != {
        "schemaVersion",
        "target",
        "allowPrereleases",
        "historicalExactVersionsRemainInstallable",
        "immutablePublishedAssets",
    }:
        raise ConsumerError("captured php-bin invariants have unknown or missing fields")
    if invariants_document.get("schemaVersion") != 1:
        raise ConsumerError("unsupported captured php-bin invariant version")
    if policy_document.get("policyInvariantsDigest") != invariants_digest:
        raise ConsumerError("captured support policy is not bound to captured reviewed invariants")
    if invariants_document.get("target") != {
        "os": "macOS", "minimumVersion": "26.0", "architecture": "arm64", "sapi": "cli"
    }:
        raise ConsumerError("captured php-bin target invariants changed")
    if invariants_document.get("allowPrereleases") is not False:
        raise ConsumerError("captured php-bin policy permits prereleases")
    if invariants_document.get("historicalExactVersionsRemainInstallable") is not True:
        raise ConsumerError("captured php-bin policy disables historical exact installs")
    if invariants_document.get("immutablePublishedAssets") is not True:
        raise ConsumerError("captured php-bin policy permits published asset replacement")
    branches = policy_document.get("maintainedBranches")
    if not (
        isinstance(branches, list)
        and all(re.fullmatch(r"\d+\.\d+", value) for value in branches)
        and branches == sorted(set(branches), key=lambda value: tuple(map(int, value.split("."))))
    ):
        raise ConsumerError("captured php-bin branches are invalid or non-canonical")
    evidence = policy_document.get("sourceEvidenceDigests")
    if not (
        isinstance(evidence, list)
        and all(re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in evidence)
        and evidence == sorted(set(evidence))
    ):
        raise ConsumerError("captured php-bin support evidence is invalid or non-canonical")
    policy_action = policy_document.get("actionKey")
    if not (
        policy_action == "bootstrap"
        or re.fullmatch(
            r"(?:new_branch:\d+\.\d+|branch_eol:\d+\.\d+:\d{4}-\d{2}-\d{2})",
            policy_action or "",
        )
    ):
        raise ConsumerError("captured php-bin support action key is invalid")
    if policy_action != "bootstrap" and not evidence:
        raise ConsumerError("captured php-bin support policy lacks accepted evidence")
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", policy_document.get("acceptedAt", "")
    ):
        raise ConsumerError("captured php-bin support acceptance time is invalid")
    commit_document = load(policy_commit)
    commit_sha = commit_document.get("sha", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
        raise ConsumerError("captured php-bin main state has no exact commit")
    existing = load(snapshot) if snapshot.exists() else {}
    if existing and (
        set(existing)
        != {
            "schemaVersion", "phpBinPolicyCommit", "policyDigest",
            "policyInvariantsDigest", "maintainedBranches", "generated",
        }
        or existing.get("schemaVersion") != 1
        or existing.get("generated") is not True
    ):
        raise ConsumerError("local support snapshot has unknown, missing, or invalid fields")
    if (
        existing.get("policyDigest") != policy_digest
        or existing.get("policyInvariantsDigest") != invariants_digest
        or existing.get("phpBinPolicyCommit") != commit_sha
        or existing.get("maintainedBranches") != branches
    ):
        trigger = "policy_changed"
        state = None
    elif readiness_dir is not None:
        state = readiness_state(policy_action, commit_sha, policy_digest, invariants_digest, readiness_dir)
        trigger = READINESS_TRIGGERS.get(state["state"], "quiet")
    else:
        trigger = "quiet"
        state = None
    decision = {
        "schemaVersion": 1,
        "trigger": trigger,
        "actionKey": policy_document.get("actionKey"),
        "policyDigest": policy_digest,
        "policyInvariantsDigest": invariants_digest,
        "phpBinPolicyCommit": commit_sha,
        "synchronize": trigger == "policy_changed",
    }
    if state is not None:
        decision["readiness"] = state
    return decision


def _captured_body(capture_manifest: pathlib.Path, capture: dict[str, Any]) -> pathlib.Path:
    """Resolve one captured body beside its manifest and prove it still has its digest."""
    body_path = capture.get("bodyPath")
    if not isinstance(body_path, str) or not body_path or "/" in body_path or body_path.startswith("."):
        raise ConsumerError(f"captured body path is unsafe: {body_path}")
    path = capture_manifest.parent / body_path
    if not path.is_file() or digest(path.read_bytes()) != capture.get("digest"):
        raise ConsumerError(f"captured body changed: {capture.get('captureId')}")
    return path


def synchronization_plan(
    decision: dict[str, Any],
    capture_manifest: pathlib.Path,
    mise_head: str,
    operator_commit: str,
    operator_state: str,
) -> dict[str, Any]:
    """Bind one snapshot synchronization to the exact captured policy and commits.

    The plan is derived, never authored: its action key is the accepted policy's own
    lifecycle key, its preconditions are the exact mise-php base, php-bin policy
    commit, policy and invariants digests, and php-bin operator state, and its only
    admitted paths are the snapshot and the Lua table generated from it. It fails
    closed when the capture set is incomplete, a body changed after capture, the
    selector and commit captures disagree, unattended mutation is paused, or the
    policy carries no lifecycle key to record readiness for (a hand-edited
    `bootstrap` policy).
    """
    if decision.get("trigger") != "policy_changed":
        raise ConsumerError("only a changed policy needs a synchronization")
    action_key = decision.get("actionKey")
    if not isinstance(action_key, str) or not ACTION_KEY_RE.fullmatch(action_key):
        raise ConsumerError(f"accepted policy carries no lifecycle action key to synchronize: {action_key}")
    for name, value in {"mise-php head": mise_head, "php-bin operator commit": operator_commit}.items():
        if not re.fullmatch(r"[0-9a-f]{40}", value or ""):
            raise ConsumerError(f"{name} is not an exact SHA")
    if operator_state != "enabled":
        raise ConsumerError("unattended mutation is paused in php-bin")
    document = load(capture_manifest)
    captures = document.get("captures") if isinstance(document, dict) else None
    if not isinstance(document, dict) or document.get("schemaVersion") != 1 or not isinstance(captures, list):
        raise ConsumerError("invalid policy capture manifest")
    by_id = {item.get("captureId"): item for item in captures if isinstance(item, dict)}
    if len(by_id) != len(captures) or set(by_id) != POLICY_CAPTURE_IDS:
        raise ConsumerError("policy capture set is incomplete or ambiguous")
    bodies = {capture_id: _captured_body(capture_manifest, capture) for capture_id, capture in by_id.items()}
    if by_id["support_policy"].get("digest") != decision.get("policyDigest"):
        raise ConsumerError("captured support policy digest changed")
    if by_id["policy_invariants"].get("digest") != decision.get("policyInvariantsDigest"):
        raise ConsumerError("captured policy invariants digest changed")
    selector = load(bodies["php_bin_policy_selector"])
    commit = load(bodies["php_bin_state"])
    policy_commit = decision.get("phpBinPolicyCommit")
    if (
        not isinstance(selector, list)
        or len(selector) != 1
        or not isinstance(selector[0], dict)
        or selector[0].get("sha") != policy_commit
        or not isinstance(commit, dict)
        or commit.get("sha") != policy_commit
    ):
        raise ConsumerError("captured php-bin policy commit changed")
    return {
        "schemaVersion": 1,
        "actionKey": action_key,
        "preconditions": {
            "misePhpHead": mise_head,
            "phpBinPolicyCommit": policy_commit,
            "supportPolicyDigest": decision["policyDigest"],
            "policyInvariantsDigest": decision["policyInvariantsDigest"],
            "phpBinOperatorCommit": operator_commit,
            "operatorState": operator_state,
        },
        "allowedPaths": {"mise-php": list(SYNCHRONIZED_PATHS)},
        "evidenceDigests": sorted(capture["digest"] for capture in by_id.values()),
    }


def render_snapshot(snapshot: dict[str, Any]) -> str:
    """Render the support snapshot in its reviewed layout: fixed key order, inline lists."""
    lines = []
    for key in SNAPSHOT_FIELDS:
        value = snapshot[key]
        rendered = (
            "[" + ", ".join(json.dumps(item) for item in value) + "]"
            if isinstance(value, list)
            else json.dumps(value)
        )
        lines.append(f"  {json.dumps(key)}: {rendered}")
    return "{\n" + ",\n".join(lines) + "\n}\n"


def synchronize(plan: dict[str, Any], policy: pathlib.Path, snapshot: pathlib.Path) -> dict[str, Any]:
    """Regenerate the support snapshot from the exact policy a plan is bound to."""
    preconditions = plan.get("preconditions") if isinstance(plan, dict) else None
    if not isinstance(preconditions, dict):
        raise ConsumerError("synchronization plan has no preconditions")
    if digest(policy.read_bytes()) != preconditions.get("supportPolicyDigest"):
        raise ConsumerError("captured support policy is not the one the plan is bound to")
    branches = load(policy).get("maintainedBranches")
    if not (
        isinstance(branches, list)
        and all(isinstance(value, str) and re.fullmatch(r"\d+\.\d+", value) for value in branches)
        and branches == sorted(set(branches), key=lambda value: tuple(map(int, value.split("."))))
    ):
        raise ConsumerError("captured php-bin branches are invalid or non-canonical")
    document = {
        "schemaVersion": 1,
        "phpBinPolicyCommit": preconditions.get("phpBinPolicyCommit"),
        "policyDigest": preconditions.get("supportPolicyDigest"),
        "policyInvariantsDigest": preconditions.get("policyInvariantsDigest"),
        "maintainedBranches": branches,
        "generated": True,
    }
    snapshot.write_text(render_snapshot(document))
    return document


def readiness(
    action_key: str,
    php_bin_commit: str,
    policy_digest: str,
    policy_invariants_digest: str,
    mise_commit: str,
    evidence_digests: list[str],
) -> dict[str, Any]:
    if not ACTION_KEY_RE.fullmatch(action_key):
        raise ConsumerError("invalid action key")
    for name, value in {
        "php-bin commit": php_bin_commit,
        "mise-php commit": mise_commit,
    }.items():
        if not re.fullmatch(r"[0-9a-f]{40}", value):
            raise ConsumerError(f"{name} is not an exact SHA")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", policy_digest):
        raise ConsumerError("invalid policy digest")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", policy_invariants_digest):
        raise ConsumerError("invalid policy invariants digest")
    if not evidence_digests or not all(
        re.fullmatch(r"sha256:[0-9a-f]{64}", item) for item in evidence_digests
    ):
        raise ConsumerError("readiness requires exact evidence digests")
    return {
        "schemaVersion": 1,
        "actionKey": action_key,
        "state": "mise_ready",
        "ready": True,
        "phpBinPolicyCommit": php_bin_commit,
        "policyDigest": policy_digest,
        "policyInvariantsDigest": policy_invariants_digest,
        "misePhpCommit": mise_commit,
        "evidenceDigests": sorted(evidence_digests),
        "recordedAt": now(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("fetch")
    fetch.add_argument("--output", required=True, type=pathlib.Path)
    fetch.add_argument("--invariants-output", required=True, type=pathlib.Path)
    fetch.add_argument("--commit-output", required=True, type=pathlib.Path)
    fetch.add_argument("--manifest", required=True, type=pathlib.Path)
    compare_parser = sub.add_parser("compare")
    compare_parser.add_argument("--policy", required=True, type=pathlib.Path)
    compare_parser.add_argument("--invariants", required=True, type=pathlib.Path)
    compare_parser.add_argument("--policy-commit", required=True, type=pathlib.Path)
    compare_parser.add_argument("--snapshot", required=True, type=pathlib.Path)
    compare_parser.add_argument("--readiness-dir", type=pathlib.Path)
    compare_parser.add_argument("--output", required=True, type=pathlib.Path)
    ready = sub.add_parser("readiness")
    ready.add_argument("--action-key", required=True)
    ready.add_argument("--php-bin-commit", required=True)
    ready.add_argument("--policy-digest", required=True)
    ready.add_argument("--policy-invariants-digest", required=True)
    ready.add_argument("--mise-commit", required=True)
    ready.add_argument("--evidence-digest", action="append", required=True)
    ready.add_argument("--output", required=True, type=pathlib.Path)
    plan = sub.add_parser("plan")
    plan.add_argument("--decision", required=True, type=pathlib.Path)
    plan.add_argument("--capture-manifest", required=True, type=pathlib.Path)
    plan.add_argument("--mise-head", required=True)
    plan.add_argument("--operator-commit", required=True)
    plan.add_argument("--operator-state", required=True)
    plan.add_argument("--output", required=True, type=pathlib.Path)
    sync = sub.add_parser("synchronize")
    sync.add_argument("--plan", required=True, type=pathlib.Path)
    sync.add_argument("--policy", required=True, type=pathlib.Path)
    sync.add_argument("--snapshot", required=True, type=pathlib.Path)
    same = sub.add_parser("same-readiness")
    same.add_argument("--existing", required=True, type=pathlib.Path)
    same.add_argument("--candidate", required=True, type=pathlib.Path)
    filename = sub.add_parser("action-filename")
    filename.add_argument("action_key")
    filename.add_argument("--suffix", default=".json")
    args = parser.parse_args()
    try:
        if args.command == "fetch":
            write(
                args.manifest,
                {
                    "schemaVersion": 1,
                    "captures": fetch_policy_set(args.output, args.invariants_output, args.commit_output),
                },
            )
        elif args.command == "plan":
            result = synchronization_plan(
                load(args.decision),
                args.capture_manifest,
                args.mise_head,
                args.operator_commit,
                args.operator_state,
            )
            write(args.output, result)
            print(json.dumps(result))
        elif args.command == "synchronize":
            print(json.dumps(synchronize(load(args.plan), args.policy, args.snapshot)))
        elif args.command == "same-readiness":
            same_readiness(load(args.existing), load(args.candidate))
        elif args.command == "action-filename":
            print(action_filename(args.action_key, args.suffix))
        elif args.command == "compare":
            result = compare(
                args.policy, args.invariants, args.policy_commit, args.snapshot, args.readiness_dir
            )
            write(args.output, result)
            print(json.dumps(result))
        else:
            result = readiness(
                args.action_key,
                args.php_bin_commit,
                args.policy_digest,
                args.policy_invariants_digest,
                args.mise_commit,
                args.evidence_digest,
            )
            write(args.output, result)
            print(json.dumps(result))
        return 0
    except (ConsumerError, OSError, json.JSONDecodeError) as error:
        print(f"autorelease consumer rejected input: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
