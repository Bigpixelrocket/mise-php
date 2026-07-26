#!/usr/bin/env python3
"""Opaque policy comparison and exact-commit readiness records.

This module does not classify PHP lifecycle events. Codex owns semantics.
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
from typing import Any


ACTION_KEY_RE = re.compile(
    r"^(new_patch:\d+\.\d+\.\d+|new_branch:\d+\.\d+|"
    r"branch_eol:\d+\.\d+:\d{4}-\d{2}-\d{2}|"
    r"recipe_rebuild:\d+\.\d+\.\d+:[1-9]\d*|"
    r"repair:\d+\.\d+\.\d+:[0-9a-f]{8,64})$"
)
POLICY_URL = "https://raw.githubusercontent.com/bigpixelrocket/php-bin/main/support-policy.json"
POLICY_COMMIT_URL = "https://api.github.com/repos/bigpixelrocket/php-bin/commits/main"


class ConsumerError(RuntimeError):
    pass


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
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "bigpixelrocket-maintenance/1"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
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


def compare(
    policy: pathlib.Path,
    policy_commit: pathlib.Path,
    snapshot: pathlib.Path,
    events: pathlib.Path,
) -> dict[str, Any]:
    policy_digest = digest(policy.read_bytes())
    commit_document = load(policy_commit)
    commit_sha = commit_document.get("sha", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
        raise ConsumerError("captured php-bin main state has no exact commit")
    existing = load(snapshot) if snapshot.exists() else {}
    incomplete = []
    if events.exists():
        for path in events.glob("*.json"):
            event = load(path)
            if event.get("state") not in {"mise_ready", "complete"}:
                incomplete.append(event.get("actionKey"))
    if incomplete:
        trigger = "event_incomplete"
    elif existing.get("policyDigest") != policy_digest:
        trigger = "policy_changed"
    else:
        trigger = "quiet"
    return {
        "schemaVersion": 1,
        "trigger": trigger,
        "policyDigest": policy_digest,
        "phpBinPolicyCommit": commit_sha,
        "incompleteActions": sorted(incomplete),
        "modelCall": trigger != "quiet",
    }


def readiness(
    action_key: str,
    php_bin_commit: str,
    policy_digest: str,
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
        "misePhpCommit": mise_commit,
        "evidenceDigests": sorted(evidence_digests),
        "recordedAt": now(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("fetch")
    fetch.add_argument("--output", required=True, type=pathlib.Path)
    fetch.add_argument("--commit-output", required=True, type=pathlib.Path)
    fetch.add_argument("--manifest", required=True, type=pathlib.Path)
    compare_parser = sub.add_parser("compare")
    compare_parser.add_argument("--policy", required=True, type=pathlib.Path)
    compare_parser.add_argument("--policy-commit", required=True, type=pathlib.Path)
    compare_parser.add_argument("--snapshot", required=True, type=pathlib.Path)
    compare_parser.add_argument("--events", required=True, type=pathlib.Path)
    compare_parser.add_argument("--output", required=True, type=pathlib.Path)
    ready = sub.add_parser("readiness")
    ready.add_argument("--action-key", required=True)
    ready.add_argument("--php-bin-commit", required=True)
    ready.add_argument("--policy-digest", required=True)
    ready.add_argument("--mise-commit", required=True)
    ready.add_argument("--evidence-digest", action="append", required=True)
    ready.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()
    try:
        if args.command == "fetch":
            write(
                args.manifest,
                {
                    "schemaVersion": 1,
                    "captures": [
                        fetch_url(POLICY_URL, args.output),
                        fetch_url(POLICY_COMMIT_URL, args.commit_output),
                    ],
                },
            )
        elif args.command == "compare":
            result = compare(args.policy, args.policy_commit, args.snapshot, args.events)
            write(args.output, result)
            print(json.dumps(result))
        else:
            result = readiness(
                args.action_key,
                args.php_bin_commit,
                args.policy_digest,
                args.mise_commit,
                args.evidence_digest,
            )
            write(args.output, result)
            print(json.dumps(result))
        return 0
    except (ConsumerError, OSError, json.JSONDecodeError) as error:
        print(f"maintenance consumer rejected input: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
