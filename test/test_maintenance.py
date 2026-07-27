import pathlib
import subprocess
import tempfile
import unittest
import json

from maintenance.admission import AdmissionError, admit, digest_file, protected, verify_merge
from maintenance.consumer import compare, digest, pinned_policy_urls, readiness, write


class MaintenanceConsumerTests(unittest.TestCase):
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
        self.assertTrue(protected(".github/workflows/maintenance.yml"))
        self.assertTrue(protected("maintenance/admission.py"))
        self.assertTrue(protected("maintenance-events/new-patch.json"))
        self.assertTrue(protected("readiness/new-branch.json"))
        self.assertFalse(protected("lib/releases.lua"))

    def test_policy_capture_urls_are_commit_pinned(self):
        sha = "a" * 40
        policy, invariants = pinned_policy_urls(sha)
        self.assertIn(f"/{sha}/support-policy.json", policy)
        self.assertIn(f"/{sha}/maintenance/policy-invariants.json", invariants)
        with self.assertRaises(Exception):
            pinned_policy_urls("main")

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


if __name__ == "__main__":
    unittest.main()
