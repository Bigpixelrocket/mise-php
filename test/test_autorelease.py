import pathlib
import subprocess
import tempfile
import unittest
import json
from unittest import mock

from autorelease import admission, consumer
from autorelease.admission import AdmissionError, admit, digest_file, protected, seal, verify_merge
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
            result = compare(policy, invariants, commit, snapshot)
            self.assertEqual("quiet", result["trigger"])
            policy.write_text(json.dumps({
                "schemaVersion": 1,
                "policyInvariantsDigest": digest(invariants.read_bytes()),
                "maintainedBranches": ["8.5", "8.6"],
                "sourceEvidenceDigests": [],
                "actionKey": "bootstrap",
                "acceptedAt": "2026-07-27T00:00:00Z",
            }) + "\n")
            self.assertEqual("policy_changed", compare(policy, invariants, commit, snapshot)["trigger"])

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

    def test_validate_readiness_record_accepts_consumer_output(self):
        record = consumer.readiness(
            "new_patch:8.5.9",
            "a" * 40,
            "sha256:" + "b" * 64,
            "sha256:" + "c" * 64,
            "d" * 40,
            ["sha256:" + "e" * 64],
        )
        admission.validate_readiness_record(record)

    def test_validate_readiness_record_rejects_tampering(self):
        record = consumer.readiness(
            "new_patch:8.5.9",
            "a" * 40,
            "sha256:" + "b" * 64,
            "sha256:" + "c" * 64,
            "d" * 40,
            ["sha256:" + "e" * 64],
        )
        for corrupt in (
            {**record, "ready": False},
            {**record, "state": "published"},
            {**record, "actionKey": "merge:now"},
            {**record, "extra": 1},
            {k: v for k, v in record.items() if k != "evidenceDigests"},
        ):
            with self.assertRaises(admission.AdmissionError):
                admission.validate_readiness_record(corrupt)

    def test_action_key_alphabet_and_filename_have_one_definition(self):
        # admission and consumer both name files and branches from an action key; a
        # second copy of either rule drifts silently against php-bin.
        # re.compile caches by pattern, so identical copies are indistinguishable at
        # runtime; the single definition is only observable in the source.
        source = pathlib.Path("autorelease/admission.py").read_text()
        self.assertIn("from autorelease.consumer import ACTION_KEY_RE", source)
        self.assertNotIn("ACTION_KEY_RE = re.compile", source)
        self.assertEqual(admission.ACTION_KEY_RE.pattern, consumer.ACTION_KEY_RE.pattern)
        self.assertEqual(
            "branch_eol-8.2-2026-12-31.json", consumer.action_filename("branch_eol:8.2:2026-12-31")
        )
        self.assertEqual("new_patch-8.5.9", consumer.action_filename("new_patch:8.5.9", ""))
        with self.assertRaises(ConsumerError):
            consumer.action_filename("../escape")
        # The workflow reaches the helper through the same entry point as every other
        # consumer subcommand, so the shell sites cannot re-derive the mapping.
        result = subprocess.run(
            ["./scripts/consume-php-policy", "action-filename", "new_patch:8.5.9"],
            check=True, text=True, stdout=subprocess.PIPE,
        )
        self.assertEqual("new_patch-8.5.9.json", result.stdout.strip())
        workflow = pathlib.Path(".github/workflows/autorelease-consumer.yml").read_text()
        self.assertNotIn("tr ':/'", workflow)

    def test_protected_controls_are_not_admissible(self):
        self.assertTrue(protected(".github/codex-action-contract.json"))
        self.assertTrue(protected(".github/workflows/autorelease-consumer.yml"))
        self.assertTrue(protected("autorelease/admission.py"))
        self.assertTrue(protected("scripts/validate-codex-action-inputs"))
        self.assertTrue(protected("autorelease-events/new-patch.json"))
        self.assertTrue(protected("readiness/new-branch.json"))
        self.assertFalse(protected("lib/releases.lua"))

    def test_gate_harness_paths_are_protected(self):
        for path in ("scripts/test.sh", "scripts/check-public-language.sh",
                     "scripts/consume-php-policy", "scripts/generate-policy-lua",
                     "test/test_autorelease.py"):
            self.assertTrue(protected(path), path)
        # Runtime patches regenerate the policy table, so the generated file stays admissible.
        self.assertFalse(protected("lib/policy.lua"))

    def test_codeowners_covers_every_protected_script(self):
        patterns = json.loads(pathlib.Path("autorelease/protected-paths.json").read_text())["patterns"]
        codeowners = pathlib.Path(".github/CODEOWNERS").read_text()
        for pattern in patterns:
            if "*" not in pattern:
                self.assertIn(f"/{pattern} ", codeowners, pattern)

    def test_shared_file_manifest_gates_the_consumer_run(self):
        # ~20 files are duplicated from php-bin and most had drifted silently. The
        # manifest declares the intended-identical set; the consumer compares it
        # against php-bin at the exact pinned commit before it mutates anything.
        root = pathlib.Path(__file__).resolve().parents[1]
        manifest = json.loads((root / "autorelease/shared-files.json").read_text())
        self.assertEqual(1, manifest["schemaVersion"])
        paths = manifest["paths"]
        self.assertEqual(sorted(set(paths)), paths)
        # An emptied manifest satisfies every shape assertion while gating nothing,
        # so the scripts the gate exists for are named outright.
        self.assertLessEqual(
            {
                "scripts/assert-admission-checks",
                "scripts/check-public-language.sh",
                "scripts/dispatch-pr-checks",
            },
            set(paths),
        )
        for path in paths:
            self.assertTrue((root / path).is_file(), path)
            # A shared file an agent may rewrite would fail the gate on the next
            # run, so every listed path needs owner review of its own.
            self.assertTrue(protected(path), path)
        self.assertTrue(protected("autorelease/shared-files.json"))
        consumer = (root / ".github/workflows/autorelease-consumer.yml").read_text()
        self.assertIn("jq -r '.paths[]' autorelease/shared-files.json", consumer)
        self.assertIn('if [[ "${#shared[@]}" -eq 0 ]]; then', consumer)

    def test_secret_scanner_catches_sk_tokens(self):
        for secret in (
            "key = sk-" + "a" * 24,
            "github_pat_" + "a" * 22,
            "ghp_" + "a" * 36,
            "-----BEGIN OPENSSH PRIVATE KEY-----",
        ):
            self.assertIsNotNone(admission.SECRET_RE.search(secret), secret)
        for benign in ("task-" + "a" * 24, "github_pat_x", "flask-login"):
            self.assertIsNone(admission.SECRET_RE.search(benign), benign)

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

    def test_agent_task_criteria_come_from_one_table(self):
        # Criteria used to be authored as jq literals in three workflow steps, so the
        # only way to check them was matching workflow source text. They now come from
        # one script and the emitted JSON is what the agent actually receives.
        script = str(pathlib.Path(__file__).resolve().parents[1] / "scripts/prepare-agent-task")
        expected = {
            "investigation": [
                "phase-goal-correct",
                "policy-difference-explained",
                "authority-explicit",
                "no-unresolved-work",
            ],
            "implementation": [
                "phase-goal-correct",
                "admitted-diff-complete",
                "advisory-checks-recorded",
                "no-unresolved-work",
            ],
            "repair": [
                "phase-goal-correct",
                "failure-cause-removed",
                "advisory-checks-recorded",
                "no-unresolved-work",
            ],
        }
        for phase, ids in expected.items():
            emitted = json.loads(
                subprocess.run([script, "--phase", phase], capture_output=True, check=True).stdout
            )
            self.assertEqual(ids, [criterion["id"] for criterion in emitted], phase)
            for criterion in emitted:
                self.assertEqual(["id", "requirement", "evidenceRequired"], list(criterion), phase)
                self.assertTrue(all(criterion.values()), phase)
        self.assertNotEqual(0, subprocess.run([script, "--phase", "audit"], capture_output=True).returncode)

    def test_assert_admission_checks_covers_the_plugin_contract_bucket(self):
        # The consumer merge gates only ever pass --check-name, so this repository's
        # copy of the shared script must keep that path working on its own.
        script = str(pathlib.Path(__file__).resolve().parents[1] / "scripts/assert-admission-checks")
        with tempfile.TemporaryDirectory() as temporary:
            checks = pathlib.Path(temporary) / "checks.json"
            checks.write_text(json.dumps([{"name": "Plugin contract", "bucket": "pass"}]))
            subprocess.run([script, "--check-name", "Plugin contract", "--checks", str(checks)], check=True)
            checks.write_text(json.dumps([{"name": "Script checks", "bucket": "pass"}]))
            result = subprocess.run(
                [script, "--check-name", "Plugin contract", "--checks", str(checks)], capture_output=True
            )
            self.assertNotEqual(0, result.returncode)

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

        # The pin can target any historical php-bin layout, so a superseded
        # invariants path must still resolve rather than fail the capture.
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

    def test_merge_admission_cli_prints_the_verdict_and_fails_closed(self):
        # The merge job reaches the gate through this entry point and reads nothing
        # but its exit status, so a rejection that exits 0 would merge an unadmitted
        # patch. The in-process test above covers what the gate decides.
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            for arguments in (
                ["init", "-q", "-b", "main"],
                ["config", "user.name", "test"],
                ["config", "user.email", "test@invalid"],
            ):
                subprocess.run(["git", *arguments], cwd=root, check=True)
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
            paths = {
                "manifest": {
                    "baseSha": base,
                    "files": [{
                        "path": "file.txt",
                        "digest": digest((root / "file.txt").read_bytes()),
                        "mode": "0o644",
                    }],
                },
                "checks": {"Plugin contract": "success"},
                "preconditions": {"misePhpHead": base},
                "current": {"misePhpHead": base},
            }
            for name, body in paths.items():
                (root / f"{name}.json").write_text(json.dumps(body) + "\n")

            def run_gate(expected_head):
                return subprocess.run(
                    [
                        "./scripts/verify-merge-admission",
                        "--repo", str(root),
                        "--head", expected_head,
                        "--manifest", str(root / "manifest.json"),
                        "--checks", str(root / "checks.json"),
                        "--preconditions", str(root / "preconditions.json"),
                        "--current", str(root / "current.json"),
                    ],
                    check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )

            admitted = run_gate(head)
            self.assertEqual(0, admitted.returncode, admitted.stderr)
            self.assertTrue(json.loads(admitted.stdout)["admitted"])
            rejected = run_gate(base)
            self.assertEqual(1, rejected.returncode)
            self.assertIn("mise autorelease admission rejected", rejected.stderr)

    # Returns an admissible plan plus the remaining admit() arguments by keyword, so
    # a test can vary one part of the plan without rebuilding the policy capture.
    def admission_fixture(self, root):
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
            "allowedPaths": {"mise-php": ["support-snapshot.json", "lib/policy.lua"]},
            "requiredChecks": ["Plugin contract"],
            "risk": "lifecycle",
            "agentOperations": [],
            "budgets": {"maxModelCalls": 1, "maxRetries": 1, "timeoutMinutes": 30},
        }
        return plan, {
            "contract": contract,
            "shared": shared,
            "phase": phase,
            "event": event,
            "capture_manifest": manifest,
            "policy_digest": policy_digest,
            "invariants_digest": invariants_digest,
            "mise_head": preconditions["misePhpHead"],
        }

    def test_admission_binds_complete_policy_capture_and_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan, arguments = self.admission_fixture(pathlib.Path(temporary))
            self.assertTrue(admit(plan, **arguments)["admitted"])
            with self.assertRaises(AdmissionError):
                admit(plan, **{**arguments, "policy_digest": "sha256:" + "f" * 64})

    def test_admission_requires_the_generated_policy_lua_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan, arguments = self.admission_fixture(pathlib.Path(temporary))
            plan["allowedPaths"] = {"mise-php": ["support-snapshot.json"]}
            with self.assertRaises(AdmissionError) as ctx:
                admit(plan, **arguments)
            self.assertIn("lib/policy.lua", str(ctx.exception))
            plan["allowedPaths"] = {"mise-php": ["support-snapshot.json", "lib/policy.lua"]}
            self.assertTrue(admit(plan, **arguments)["admitted"])

    def generated_policy_lua(self, branches):
        return (
            "-- Generated by scripts/generate-policy-lua from support-snapshot.json.\n"
            "-- Do not edit by hand; regenerate when the snapshot changes.\n"
            "return {\n"
            "    maintained = {\n"
            + "".join(f'        "{branch}",\n' for branch in branches)
            + "    },\n"
            "}\n"
        )

    # Commits a repo whose base snapshot and lib/policy.lua both list base_branches and
    # returns the repo, its base commit, and the remaining seal() arguments by keyword.
    def seal_fixture(self, root, accepted, base_branches):
        repo = root / "repo"
        (repo / "lib").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@invalid"], cwd=repo, check=True)
        policy = root / "support-policy.json"
        policy.write_text(json.dumps({"maintainedBranches": accepted}) + "\n")
        preconditions = {
            "misePhpHead": "d" * 40,
            "phpBinPolicyCommit": "a" * 40,
            "supportPolicyDigest": digest_file(policy),
            "policyInvariantsDigest": "sha256:" + "c" * 64,
            "phpBinOperatorCommit": "e" * 40,
            "operatorState": "enabled",
        }
        (repo / "support-snapshot.json").write_text(json.dumps({
            "schemaVersion": 1,
            "phpBinPolicyCommit": preconditions["phpBinPolicyCommit"],
            "policyDigest": preconditions["supportPolicyDigest"],
            "policyInvariantsDigest": preconditions["policyInvariantsDigest"],
            "maintainedBranches": base_branches,
            "generated": True,
        }) + "\n")
        (repo / "lib" / "policy.lua").write_text(self.generated_policy_lua(base_branches))
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, stdout=subprocess.PIPE
        ).stdout.strip()
        digests = {
            "shared": "sha256:" + "1" * 64,
            "phaseTemplate": "sha256:" + "2" * 64,
            "eventContract": "sha256:" + "3" * 64,
        }
        return repo, base, {
            "plan": {
                "actionKey": "new_branch:8.6",
                "agentContract": {"instructionDigests": digests},
                "preconditions": preconditions,
                "allowedPaths": {"mise-php": ["support-snapshot.json", "lib/policy.lua"]},
            },
            "result": {
                "instructionDigests": digests,
                "phaseStatus": "complete",
                "criteria": [{"id": "done", "status": "passed", "evidence": ["preconditions.misePhpHead"]}],
                "unresolved": [],
                "goNoGo": "go",
            },
            "contract": {
                "actionKey": "new_branch:8.6",
                "preconditions": preconditions,
                "completionCriteria": [{"id": "done"}],
            },
            "policy_path": policy,
            "output": root / "sealed",
        }

    def test_snapshot_diff_requires_matching_policy_lua(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            accepted = ["8.3", "8.4", "8.5", "8.6"]
            repo, base, arguments = self.seal_fixture(root, accepted, ["8.2", "8.3", "8.4", "8.5"])
            snapshot = repo / "support-snapshot.json"
            document = json.loads(snapshot.read_text())
            document["maintainedBranches"] = accepted
            snapshot.write_text(json.dumps(document) + "\n")
            with self.assertRaises(AdmissionError) as ctx:
                seal(repo, base, **arguments)
            self.assertIn("policy.lua", str(ctx.exception))
            (repo / "lib" / "policy.lua").write_text(self.generated_policy_lua(accepted))
            manifest = seal(repo, base, **arguments)
            self.assertEqual(
                ["lib/policy.lua", "support-snapshot.json"], [item["path"] for item in manifest["files"]]
            )

    def test_policy_lua_diff_requires_matching_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            maintained = ["8.3", "8.4", "8.5", "8.6"]
            repo, base, arguments = self.seal_fixture(root, maintained, maintained)
            # A lone lib/policy.lua edit would widen the plugin's branch filter with no
            # snapshot evidence that php-bin accepted the added branch.
            (repo / "lib" / "policy.lua").write_text(self.generated_policy_lua(maintained + ["9.0"]))
            with self.assertRaises(AdmissionError) as ctx:
                seal(repo, base, **arguments)
            self.assertIn("policy.lua", str(ctx.exception))

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
