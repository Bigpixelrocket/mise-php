import pathlib
import subprocess
import tempfile
import unittest
import json
from unittest import mock

from autorelease import consumer
from autorelease.admission import AdmissionError, admit, digest_file, protected, verify_merge
from autorelease.consumer import (
    CaptureAbsent,
    ConsumerError,
    compare,
    digest,
    fetch_first_url,
    pinned_policy_urls,
    readiness,
    write,
)


class AutoreleaseConsumerTests(unittest.TestCase):
    def test_opaque_policy_comparison(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            policy = root / "policy.json"
            invariants = root / "invariants.json"
            commit = root / "commit.json"
            snapshot = root / "snapshot.json"
            events = root / "events"
            events.mkdir()
            invariants.write_text('{"schemaVersion":1,"target":{"os":"macOS","minimumVersion":"26.0","architecture":"arm64","sapi":"cli"},"allowPrereleases":false,"historicalExactVersionsRemainInstallable":true,"immutablePublishedAssets":true}\n')
            policy.write_text(json.dumps({
                "schemaVersion": 1,
                "policyInvariantsDigest": digest(invariants.read_bytes()),
                "maintainedBranches": ["8.5"],
                "sourceEvidenceDigests": [],
                "actionKey": "bootstrap",
                "acceptedAt": "2026-07-27T00:00:00Z",
            }) + "\n")
            commit.write_text('{"sha":"' + "a" * 40 + '"}\n')
            write(snapshot, {
                "schemaVersion": 1,
                "phpBinPolicyCommit": "a" * 40,
                "policyDigest": digest(policy.read_bytes()),
                "policyInvariantsDigest": digest(invariants.read_bytes()),
                "maintainedBranches": ["8.5"],
                "generated": True,
            })
            result = compare(policy, invariants, commit, snapshot, events)
            self.assertEqual("quiet", result["trigger"])
            policy.write_text(json.dumps({
                "schemaVersion": 1,
                "policyInvariantsDigest": digest(invariants.read_bytes()),
                "maintainedBranches": ["8.5", "8.6"],
                "sourceEvidenceDigests": [],
                "actionKey": "bootstrap",
                "acceptedAt": "2026-07-27T00:00:00Z",
            }) + "\n")
            self.assertEqual("policy_changed", compare(policy, invariants, commit, snapshot, events)["trigger"])

    def test_readiness_requires_exact_commits_and_digests(self):
        result = readiness(
            "new_branch:8.6",
            "a" * 40,
            "sha256:" + "b" * 64,
            "sha256:" + "e" * 64,
            "c" * 40,
            ["sha256:" + "d" * 64],
        )
        self.assertTrue(result["ready"])
        with self.assertRaises(Exception):
            readiness("new_branch:8.6", "main", "bad", "bad", "main", [])

    def test_protected_controls_are_not_admissible(self):
        self.assertTrue(protected(".github/codex-action-contract.json"))
        self.assertTrue(protected(".github/workflows/autorelease-consumer.yml"))
        self.assertTrue(protected("autorelease/admission.py"))
        self.assertTrue(protected("scripts/validate-codex-action-inputs"))
        self.assertTrue(protected("autorelease-events/new-patch.json"))
        self.assertTrue(protected("readiness/new-branch.json"))
        self.assertFalse(protected("lib/releases.lua"))

    def test_investigation_defers_required_checks_to_writable_jobs(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        instructions = (root / ".github/codex/autorelease/investigation.md").read_text()
        consumer = (root / ".github/workflows/autorelease-consumer.yml").read_text()
        self.assertIn("Treat `requiredChecks` as downstream exact-head gates", instructions)
        self.assertIn("do not run them in this read-only", instructions)
        self.assertIn("not-yet-run status as unresolved", instructions)
        self.assertIn(
            'nonGoals:["upstream_php_classification","repository_mutation",'
            '"required_check_execution","irreversible_github_effect"]',
            consumer,
        )

    def test_policy_capture_urls_are_commit_pinned(self):
        sha = "a" * 40
        policy, invariants = pinned_policy_urls(sha)
        self.assertIn(f"/{sha}/support-policy.json", policy)
        self.assertEqual(
            [
                f"/{sha}/autorelease/policy-invariants.json",
                f"/{sha}/maintenance/policy-invariants.json",
            ],
            [url.split("/php-bin")[-1] for url in invariants],
        )
        with self.assertRaises(ConsumerError):
            pinned_policy_urls("main")

    def test_policy_invariants_capture_prefers_the_current_path(self):
        urls = ("https://example.invalid/new.json", "https://example.invalid/old.json")
        output = pathlib.Path("unused.json")

        with mock.patch.object(consumer, "fetch_url", return_value={"url": urls[0]}) as fetch:
            self.assertEqual(urls[0], fetch_first_url(urls, output)["url"])
        fetch.assert_called_once_with(urls[0], output)

        # The pinned policy commit predates the rename, so the older path must
        # still resolve rather than fail the capture.
        absent = [CaptureAbsent("absent"), {"url": urls[1]}]
        with mock.patch.object(consumer, "fetch_url", side_effect=absent) as fetch:
            self.assertEqual(urls[1], fetch_first_url(urls, output)["url"])
        self.assertEqual(2, fetch.call_count)

        # A transport failure must surface rather than reach for the older path.
        with mock.patch.object(consumer, "fetch_url", side_effect=ConsumerError("timeout")):
            with self.assertRaises(ConsumerError):
                fetch_first_url(urls, output)

    def test_merge_gate_binds_single_commit_diff_and_preconditions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@invalid"], cwd=root, check=True)
            (root / "file.txt").write_text("base\n")
            subprocess.run(["git", "add", "file.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=root, check=True)
            base = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True, stdout=subprocess.PIPE
            ).stdout.strip()
            (root / "file.txt").write_text("validated\n")
            subprocess.run(["git", "add", "file.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "validated"], cwd=root, check=True)
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True, stdout=subprocess.PIPE
            ).stdout.strip()
            manifest = {
                "baseSha": base,
                "files": [{"path": "file.txt", "digest": digest((root / "file.txt").read_bytes()), "mode": "0o644"}],
            }
            state = {"misePhpHead": base}
            self.assertTrue(
                verify_merge(root, head, manifest, {"Plugin contract": "success"}, state, state)["admitted"]
            )
            with self.assertRaises(AdmissionError):
                verify_merge(root, head, manifest, {"Plugin contract": "success"}, state, {"misePhpHead": head})
            (root / "extra.txt").write_text("unsealed\n")
            subprocess.run(["git", "add", "extra.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "--amend", "--no-edit"], cwd=root, check=True)
            mutated = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True, stdout=subprocess.PIPE
            ).stdout.strip()
            with self.assertRaises(AdmissionError):
                verify_merge(root, mutated, manifest, {"Plugin contract": "success"}, state, state)

    def test_admission_binds_complete_policy_capture_and_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            shared = root / "shared.md"
            phase = root / "phase.md"
            event = root / "event.json"
            shared.write_text("shared\n")
            phase.write_text("phase\n")
            commit_sha = "a" * 40
            policy_digest = "sha256:" + "b" * 64
            invariants_digest = "sha256:" + "c" * 64
            preconditions = {
                "misePhpHead": "d" * 40,
                "phpBinPolicyCommit": commit_sha,
                "supportPolicyDigest": policy_digest,
                "policyInvariantsDigest": invariants_digest,
                "phpBinOperatorCommit": "e" * 40,
                "operatorState": "enabled",
            }
            contract = {
                "contractVersion": 1,
                "actionKey": "new_branch:8.6",
                "preconditions": preconditions,
                "completionCriteria": [{"id": "done"}],
            }
            event.write_text(json.dumps(contract) + "\n")
            captures = [
                ("php_bin_policy_selector", [{"sha": commit_sha}], "/0/sha"),
                ("php_bin_state", {"sha": commit_sha}, "/sha"),
                ("support_policy", {"maintainedBranches": ["8.6"]}, "/maintainedBranches"),
                ("policy_invariants", {"target": {"os": "macOS"}}, "/target"),
            ]
            manifest_records = []
            evidence = []
            for capture_id, body, pointer in captures:
                path = root / f"{capture_id}.json"
                path.write_text(json.dumps(body) + "\n")
                body_digest = digest(path.read_bytes())
                if capture_id == "support_policy":
                    policy_digest = body_digest
                    preconditions["supportPolicyDigest"] = body_digest
                elif capture_id == "policy_invariants":
                    invariants_digest = body_digest
                    preconditions["policyInvariantsDigest"] = body_digest
                manifest_records.append({"captureId": capture_id, "bodyPath": path.name, "digest": body_digest})
                evidence.append({"captureId": capture_id, "digest": body_digest, "locator": {"kind": "json_pointer", "value": pointer}})
            event.write_text(json.dumps(contract) + "\n")
            manifest = root / "capture.json"
            manifest.write_text(json.dumps({"schemaVersion": 1, "captures": manifest_records}) + "\n")
            digests = {
                "shared": digest_file(shared),
                "phaseTemplate": digest_file(phase),
                "eventContract": digest_file(event),
            }
            plan = {
                "schemaVersion": 1,
                "actionKey": "new_branch:8.6",
                "action": "new_branch",
                "agentContract": {"instructionDigests": digests},
                "completionAssessment": {
                    "instructionDigests": digests,
                    "phaseStatus": "complete",
                    "criteria": [{"id": "done", "status": "passed", "evidence": ["evidence[0]"]}],
                    "unresolved": [],
                    "goNoGo": "go",
                },
                "preconditions": preconditions,
                "evidence": evidence,
                "repositories": ["mise-php"],
                "editsRequired": True,
                "allowedPaths": {"mise-php": ["support-snapshot.json"]},
                "requiredChecks": ["Plugin contract"],
                "risk": "lifecycle",
                "agentOperations": [],
                "budgets": {"maxModelCalls": 1, "maxRetries": 1, "timeoutMinutes": 30},
            }
            result = admit(
                plan, contract, shared, phase, event, manifest,
                policy_digest, invariants_digest, preconditions["misePhpHead"],
            )
            self.assertTrue(result["admitted"])
            with self.assertRaises(AdmissionError):
                admit(
                    plan, contract, shared, phase, event, manifest,
                    "sha256:" + "f" * 64, invariants_digest, preconditions["misePhpHead"],
                )

    def test_token_created_prs_explicitly_dispatch_required_checks(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        ci = (root / ".github/workflows/ci.yml").read_text()
        protected_workflow = (root / ".github/workflows/protected-controls.yml").read_text()
        consumer = (root / ".github/workflows/autorelease-consumer.yml").read_text()
        dispatcher = (root / "scripts/dispatch-pr-checks").read_text()
        self.assertIn("workflow_dispatch:", ci)
        self.assertIn("workflow_dispatch:", protected_workflow)
        self.assertIn("pr_number:", protected_workflow)
        self.assertIn("gh workflow run ci.yml", dispatcher)
        self.assertIn("gh workflow run protected-controls.yml", dispatcher)
        self.assertIn('"repos/$repository/check-runs"', dispatcher)
        self.assertIn('"repos/$repository/statuses/$head_sha"', dispatcher)
        self.assertIn("Exact-head validator passed", dispatcher)
        self.assertIn("./scripts/dispatch-pr-checks", consumer)
        self.assertNotIn("gh pr checks", consumer)
        self.assertIn("checks: write", consumer)
        self.assertIn("statuses: write", consumer)


if __name__ == "__main__":
    unittest.main()
