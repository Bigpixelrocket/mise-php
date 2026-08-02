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
import urllib.error
import urllib.parse
from typing import Any


ACTION_KEY_RE = re.compile(
    r"^(new_patch:\d+\.\d+\.\d+|new_branch:\d+\.\d+|"
    r"branch_eol:\d+\.\d+:\d{4}-\d{2}-\d{2}|"
    r"recipe_rebuild:\d+\.\d+\.\d+:[1-9]\d*|"
    r"repair:\d+\.\d+\.\d+:[0-9a-f]{8,64}|"
    r"(?:source_unhealthy|health_failed|policy_failure|auth_failure):[0-9a-f]{8,64})$"
)
POLICY_COMMIT_SELECTOR_URL = (
    "https://api.github.com/repos/bigpixelrocket/php-bin/commits"
    "?sha=main&path=support-policy.json&per_page=1"
)
POLICY_COMMIT_ROOT = "https://api.github.com/repos/bigpixelrocket/php-bin/commits"
RAW_ROOT = "https://raw.githubusercontent.com/bigpixelrocket/php-bin"


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

    php-bin main keeps the pre-rename `maintenance/` path until its own
    autorelease change merges. Only a 404 falls through, so a transport failure
    still raises instead of silently reaching for the older document. Drop every
    path but the first once php-bin main has landed.
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


def compare(
    policy: pathlib.Path,
    invariants: pathlib.Path,
    policy_commit: pathlib.Path,
    snapshot: pathlib.Path,
    events: pathlib.Path,
) -> dict[str, Any]:
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
    incomplete = []
    if events.exists():
        for path in events.glob("*.json"):
            event = load(path)
            if event.get("state") not in {"mise_ready", "complete"}:
                incomplete.append(event.get("actionKey"))
    if len(incomplete) > 1 or any(not ACTION_KEY_RE.fullmatch(value or "") for value in incomplete):
        raise ConsumerError("local event state is ambiguous or invalid")
    if incomplete:
        trigger = "event_incomplete"
    elif (
        existing.get("policyDigest") != policy_digest
        or existing.get("policyInvariantsDigest") != invariants_digest
        or existing.get("phpBinPolicyCommit") != commit_sha
        or existing.get("maintainedBranches") != branches
    ):
        trigger = "policy_changed"
    else:
        trigger = "quiet"
    return {
        "schemaVersion": 1,
        "trigger": trigger,
        "actionKey": incomplete[0] if incomplete else policy_document.get("actionKey"),
        "policyDigest": policy_digest,
        "policyInvariantsDigest": invariants_digest,
        "phpBinPolicyCommit": commit_sha,
        "incompleteActions": sorted(incomplete),
        "modelCall": trigger != "quiet",
    }


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
    compare_parser.add_argument("--events", required=True, type=pathlib.Path)
    compare_parser.add_argument("--output", required=True, type=pathlib.Path)
    ready = sub.add_parser("readiness")
    ready.add_argument("--action-key", required=True)
    ready.add_argument("--php-bin-commit", required=True)
    ready.add_argument("--policy-digest", required=True)
    ready.add_argument("--policy-invariants-digest", required=True)
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
                    "captures": fetch_policy_set(args.output, args.invariants_output, args.commit_output),
                },
            )
        elif args.command == "compare":
            result = compare(args.policy, args.invariants, args.policy_commit, args.snapshot, args.events)
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
