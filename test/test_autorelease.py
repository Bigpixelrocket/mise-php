import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest
import json
import os
from unittest import mock

from autorelease import admission, consumer
from autorelease.admission import AdmissionError, digest_file, protected, seal, verify_merge
from autorelease.consumer import (
    CaptureAbsent,
    ConsumerError,
    compare,
    digest,
    fetch_first_url,
    pinned_policy_urls,
    readiness,
    render_snapshot,
    synchronization_plan,
    synchronize,
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

    def test_synchronized_policy_without_readiness_resumes_it(self):
        # A run that merged the synchronization and stopped before its readiness
        # record merged leaves the snapshot current: the comparison must still ask
        # for the record, or php-bin waits for it without end.
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            policy = root / "policy.json"
            invariants = root / "invariants.json"
            commit = root / "commit.json"
            snapshot = root / "snapshot.json"
            readiness_dir = root / "readiness"
            readiness_dir.mkdir()
            invariants.write_text('{"schemaVersion":1,"target":{"os":"macOS","minimumVersion":"26.0","architecture":"arm64","sapi":"cli"},"allowPrereleases":false,"historicalExactVersionsRemainInstallable":true,"immutablePublishedAssets":true}\n')
            policy.write_text(json.dumps({
                "schemaVersion": 1,
                "policyInvariantsDigest": digest(invariants.read_bytes()),
                "maintainedBranches": ["8.5", "8.6"],
                "sourceEvidenceDigests": ["sha256:" + "f" * 64],
                "actionKey": "new_branch:8.6",
                "acceptedAt": "2026-11-20T00:00:00Z",
            }) + "\n")
            commit.write_text('{"sha":"' + "a" * 40 + '"}\n')
            write(snapshot, {
                "schemaVersion": 1,
                "phpBinPolicyCommit": "a" * 40,
                "policyDigest": digest(policy.read_bytes()),
                "policyInvariantsDigest": digest(invariants.read_bytes()),
                "maintainedBranches": ["8.5", "8.6"],
                "generated": True,
            })
            result = compare(policy, invariants, commit, snapshot, readiness_dir)
            self.assertEqual("readiness_pending", result["trigger"])
            self.assertFalse(result["synchronize"])
            # Callers that do not name the readiness folder keep the old verdict.
            self.assertEqual("quiet", compare(policy, invariants, commit, snapshot)["trigger"])
            # Planning a synchronization for it is refused: the snapshot is current.
            with self.assertRaises(ConsumerError):
                synchronization_plan(result, root / "unused.json", "d" * 40, "e" * 40, "enabled")

            record = readiness(
                "new_branch:8.6",
                "a" * 40,
                result["policyDigest"],
                result["policyInvariantsDigest"],
                "c" * 40,
                ["sha256:" + "d" * 64],
            )
            write(readiness_dir / "new_branch-8.6.json", record)
            self.assertEqual("quiet", compare(policy, invariants, commit, snapshot, readiness_dir)["trigger"])

            # A valid record for this action bound to an earlier policy commit or digest
            # is superseded: the run replaces it through the normal readiness path
            # rather than failing every day.
            for key, value in (
                ("phpBinPolicyCommit", "b" * 40),
                ("policyDigest", "sha256:" + "0" * 64),
                ("policyInvariantsDigest", "sha256:" + "1" * 64),
            ):
                write(readiness_dir / "new_branch-8.6.json", {**record, key: value})
                result = compare(policy, invariants, commit, snapshot, readiness_dir)
                self.assertEqual("readiness_superseded", result["trigger"], key)
                self.assertEqual([key], result["readiness"]["mismatched"])
                self.assertEqual("readiness/new_branch-8.6.json", result["readiness"]["record"])
                self.assertFalse(result["synchronize"])

            # A record automation could not have written is never replaced: the run
            # raises it for the owner once instead.
            for corrupt in (
                {**record, "ready": False},
                {**record, "state": "published"},
                {**record, "actionKey": "new_branch:8.7"},
                {**record, "extra": 1},
            ):
                write(readiness_dir / "new_branch-8.6.json", corrupt)
                result = compare(policy, invariants, commit, snapshot, readiness_dir)
                self.assertEqual("readiness_blocked", result["trigger"], corrupt)
                self.assertEqual(
                    digest((readiness_dir / "new_branch-8.6.json").read_bytes()),
                    result["readiness"]["recordDigest"],
                )
            (readiness_dir / "new_branch-8.6.json").write_text("{not json\n")
            result = compare(policy, invariants, commit, snapshot, readiness_dir)
            self.assertEqual("readiness_blocked", result["trigger"])
            self.assertIn("problem", result["readiness"])
            (readiness_dir / "new_branch-8.6.json").write_bytes(b"\xff\xfe{}\n")
            result = compare(policy, invariants, commit, snapshot, readiness_dir)
            self.assertEqual("readiness_blocked", result["trigger"])
            self.assertEqual(digest(b"\xff\xfe{}\n"), result["readiness"]["recordDigest"])

            # A symlink or directory in the record's place is blocked too, and never
            # read through or crashes the comparison.
            (readiness_dir / "new_branch-8.6.json").unlink()
            write(root / "elsewhere.json", record)
            place = readiness_dir / "new_branch-8.6.json"
            for make, remove in (
                (lambda: place.symlink_to(root / "elsewhere.json"), place.unlink),
                (lambda: place.symlink_to(root / "missing.json"), place.unlink),
                (place.mkdir, place.rmdir),
            ):
                make()
                result = compare(policy, invariants, commit, snapshot, readiness_dir)
                self.assertEqual("readiness_blocked", result["trigger"])
                self.assertEqual("readiness record is not a regular file", result["readiness"]["problem"])
                self.assertEqual(digest(b""), result["readiness"]["recordDigest"])
                remove()
            # A regular file that cannot be read is blocked the same way.
            write(place, record)
            place.chmod(0)
            try:
                if not os.access(place, os.R_OK):
                    result = compare(policy, invariants, commit, snapshot, readiness_dir)
                    self.assertEqual("readiness_blocked", result["trigger"])
                    self.assertTrue(result["readiness"]["problem"].startswith("readiness record cannot be read"))
                    self.assertEqual(digest(b""), result["readiness"]["recordDigest"])
            finally:
                place.chmod(0o644)

            # A policy change still synchronizes first, whatever the record says.
            (readiness_dir / "new_branch-8.6.json").unlink()
            commit.write_text('{"sha":"' + "b" * 40 + '"}\n')
            self.assertEqual("policy_changed", compare(policy, invariants, commit, snapshot, readiness_dir)["trigger"])

    def test_bootstrap_policy_needs_no_readiness_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(
                {"state": "not_required"},
                consumer.readiness_state(
                    "bootstrap", "a" * 40, "sha256:" + "b" * 64, "sha256:" + "c" * 64, pathlib.Path(temporary)
                ),
            )

    def test_blocked_readiness_raises_one_owner_issue_without_failing_the_run(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/autorelease-consumer.yml").read_text()
        job = workflow[workflow.index("\n  report-blocked-readiness:"):workflow.index("\n  synchronize:\n")]
        self.assertIn("if: needs.compare.outputs.trigger == 'readiness_blocked'", job)
        self.assertIn("issues: write", job)
        self.assertNotIn("contents: write", job)
        # One issue per record, found by its exact title, and a comment only when the
        # record bytes or the policy it should match changed.
        self.assertIn('title="Readiness record needs owner review: $record"', job)
        self.assertIn('select(.pull_request == null and .title == $title and .user.login == $bot)', job)
        # The repository is public: only issues and comments this workflow wrote
        # count, over every page.
        self.assertIn('bot="github-actions[bot]"', job)
        self.assertIn('select(.user.login == $bot) | .body', job)
        self.assertEqual(2, job.count("gh api --paginate"))
        self.assertNotIn("--limit", job)
        self.assertIn('fingerprint="$record_digest for policy $policy_commit"', job)
        self.assertLess(job.index('grep -Fq "$fingerprint"'), job.index("gh issue comment"))
        self.assertNotIn("exit 1", job)
        # Only this job may write issues; the rest of the workflow stays without them.
        self.assertEqual(1, workflow.count("issues: write"))
        # A blocked record never reaches the readiness job.
        resume = workflow[workflow.index("      - name: Resume a readiness record"):]
        resume = resume[:resume.index("      - name:", 10)]
        self.assertNotIn("readiness_blocked", resume)

    def test_consumer_resumes_pending_readiness_through_one_record_job(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/autorelease-consumer.yml").read_text()
        self.assertIn("--readiness-dir readiness", workflow)
        # Only a changed policy is planned; a pending record must not reach the plan.
        self.assertIn("if: steps.compare.outputs.trigger == 'policy_changed' &&", workflow)
        resume = workflow[workflow.index("      - name: Resume a readiness record"):]
        resume = resume[:resume.index("      - name:", 10)]
        self.assertIn("steps.compare.outputs.trigger == 'readiness_pending'", resume)
        self.assertIn("steps.compare.outputs.trigger == 'readiness_superseded'", resume)
        record_job = workflow[workflow.index("  record-readiness:"):]
        self.assertIn("needs.compare.outputs.resume == 'true'", record_job)
        self.assertIn("needs.merge-and-record-readiness.result == 'success'", record_job)
        self.assertIn("!cancelled()", record_job)
        self.assertIn('test "$(git rev-parse origin/main)" = "$READY_COMMIT"', record_job)
        self.assertIn('--mise-commit "$READY_COMMIT"', record_job)
        # A pause while the readiness checks ran stops the merge.
        self.assertLess(
            record_job.index("autorelease-operator.json"), record_job.index("gh pr merge")
        )
        self.assertIn('if [[ "$operator_state" != "enabled" ]]; then', record_job)
        # php-bin's system verifier finds the operator-bound merge gate by this job name.
        merge_job = workflow[workflow.index("  merge-and-record-readiness:"):workflow.index("  record-readiness:")]
        self.assertIn("phpBinOperatorCommit", merge_job)
        self.assertIn("operatorState", merge_job)

    def test_consumer_never_rewrites_a_branch_and_supersedes_stale_pull_requests(self):
        # A synchronization pull request that never merged leaves its branch on an older
        # main. Each run pushes a branch of its own instead of rewriting that one, and
        # closes what earlier runs left open, never a pull request from a fork.
        root = pathlib.Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/autorelease-consumer.yml").read_text()
        self.assertNotIn("--force", workflow)
        self.assertNotIn("git push -f", workflow)
        merge_job = workflow[workflow.index("  merge-and-record-readiness:"):workflow.index("  record-readiness:")]
        self.assertIn('branch="$stem-${{ github.run_id }}"', merge_job)
        self.assertIn('git push origin "HEAD:refs/heads/$branch"', merge_job)
        self.assertLess(merge_job.index("gh pr create"), merge_job.index("gh pr close"))
        record_job = workflow[workflow.index("  record-readiness:"):]
        for job in (merge_job, record_job):
            self.assertIn(".isCrossRepository == false", job)
            self.assertIn(".title == $title", job)

    def test_readiness_rerun_reuses_only_its_own_branch_and_pull_request(self):
        # A rerun after the job pushed its branch, with or without opening the pull
        # request, reuses both instead of failing on the pushed branch or opening a
        # second pull request. It reuses the branch only when it holds exactly this
        # run's record on the same main, and never rewrites it.
        root = pathlib.Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/autorelease-consumer.yml").read_text()
        record_job = workflow[workflow.index("  record-readiness:"):]
        step = record_job[record_job.index("      - name: Create readiness record"):]
        step = step[:step.index("      - name: Validate and merge readiness record")]
        self.assertIn('branch="autorelease/readiness-${{ github.run_id }}"', step)
        self.assertIn('git ls-remote --exit-code --heads origin "refs/heads/$branch"', step)
        self.assertIn('"$existing $base"', step)
        self.assertIn('!= "$record" ]]', step)
        self.assertIn("./scripts/consume-php-policy same-readiness", step)
        self.assertLess(step.index("same-readiness"), step.index('git checkout -B "$branch" "$existing"'))
        self.assertIn(".headRefName != $branch", step)
        self.assertIn('git push origin "HEAD:refs/heads/$branch"', step)
        self.assertNotIn("git push origin HEAD\n", step)
        self.assertLess(step.index('gh pr list --head "$branch" --state open'), step.index("gh pr create"))
        self.assertIn('if [[ -z "$number" ]]; then', step)

    def test_same_readiness_accepts_only_the_record_this_run_would_write(self):
        record = readiness(
            "new_branch:8.6", "a" * 40, "sha256:" + "b" * 64, "sha256:" + "e" * 64, "c" * 40,
            ["sha256:" + "d" * 64],
        )
        consumer.same_readiness({**record, "recordedAt": "2000-01-01T00:00:00Z"}, record)
        for key, value in (
            ("misePhpCommit", "f" * 40),
            ("phpBinPolicyCommit", "f" * 40),
            ("evidenceDigests", ["sha256:" + "0" * 64]),
        ):
            with self.assertRaisesRegex(ConsumerError, key):
                consumer.same_readiness({**record, key: value}, record)
        with self.assertRaises(ConsumerError):
            consumer.same_readiness({**record, "ready": False}, record)
        with tempfile.TemporaryDirectory() as temporary:
            existing = pathlib.Path(temporary) / "existing.json"
            candidate = pathlib.Path(temporary) / "candidate.json"
            write(existing, {**record, "recordedAt": "2000-01-01T00:00:00Z"})
            write(candidate, record)

            def same():
                return subprocess.run(
                    ["./scripts/consume-php-policy", "same-readiness",
                     "--existing", str(existing), "--candidate", str(candidate)],
                    check=False, capture_output=True, text=True,
                ).returncode

            self.assertEqual(0, same())
            write(existing, {**record, "misePhpCommit": "f" * 40})
            self.assertEqual(1, same())

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
        # admission and consumer both judge readiness records and their action keys; a
        # second copy of either rule drifts silently against php-bin.
        # re.compile caches by pattern, so identical copies are indistinguishable at
        # runtime; the single definition is only observable in the source.
        source = pathlib.Path("autorelease/admission.py").read_text()
        self.assertIn("from autorelease.consumer import ConsumerError, check_readiness_record", source)
        self.assertNotIn("ACTION_KEY_RE = re.compile", source)
        self.assertNotIn("READINESS_RECORD_KEYS", source)
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

    def test_unused_action_key_families_are_rejected(self):
        # Nothing in this repository reads a repair or auth_failure key from php-bin:
        # the consumer only ever sees the policy's own lifecycle key. Neither family
        # names a record here, and a readiness record carrying one is invalid.
        for action_key in ("repair:8.5.9:deadbeef", "auth_failure:deadbeef"):
            with self.assertRaises(ConsumerError, msg=action_key):
                consumer.action_filename(action_key)
            with self.assertRaises(ConsumerError, msg=action_key):
                readiness(action_key, "a" * 40, "sha256:" + "b" * 64, "sha256:" + "c" * 64, "d" * 40,
                          ["sha256:" + "e" * 64])
            result = subprocess.run(
                ["./scripts/consume-php-policy", "action-filename", action_key],
                check=False, capture_output=True, text=True,
            )
            self.assertEqual(1, result.returncode, action_key)
        for action_key in (
            "new_patch:8.5.9", "new_branch:8.6", "branch_eol:8.2:2026-12-31", "recipe_rebuild:8.5.9:2",
            "source_unhealthy:deadbeef", "health_failed:deadbeef", "policy_failure:deadbeef",
        ):
            consumer.action_filename(action_key)

    def test_pull_request_bodies_use_real_newlines(self):
        # gh passes --body through verbatim, so a "\n" inside a double-quoted shell
        # string reaches the pull request as a backslash and an n.
        root = pathlib.Path(__file__).resolve().parents[1]
        for path in (root / ".github/workflows").glob("*.yml"):
            for match in re.finditer(r'--body "((?:[^"\\]|\\.)*)"', path.read_text()):
                self.assertNotIn("\\n", match.group(1), path.name)
        workflow = (root / ".github/workflows/autorelease-consumer.yml").read_text()
        line = next(line.strip() for line in workflow.splitlines() if line.strip().startswith('body="$(printf'))
        rendered = subprocess.run(
            ["bash", "-c", f'validated=abc123; {line}; printf "%s" "$body"'],
            check=True, capture_output=True, text=True,
        ).stdout
        self.assertEqual("Deterministically sealed mise-php autorelease patch.\n\nValidated commit: `abc123`.", rendered)

    def test_protected_controls_are_not_admissible(self):
        self.assertTrue(protected(".github/workflows/autorelease-consumer.yml"))
        self.assertTrue(protected("autorelease/admission.py"))
        self.assertTrue(protected("autorelease/consumer.py"))
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
            # A shared file automation may rewrite would fail the gate on the next
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

    def test_consumer_runs_no_model_and_no_repair(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        for path in (root / ".github/workflows").glob("*.yml"):
            text = path.read_text().lower()
            self.assertNotIn("openai", text, path.name)
            self.assertNotIn("codex", text, path.name)
        consumer_workflow = (root / ".github/workflows/autorelease-consumer.yml").read_text()
        self.assertNotIn("repair", consumer_workflow.replace("There is no repair phase", ""))
        for command in ("consume-php-policy plan", "consume-php-policy synchronize", "./scripts/generate-policy-lua"):
            self.assertIn(command, consumer_workflow)
        for leftover in (".codex", ".github/codex", ".github/codex-action-contract.json", "schemas"):
            self.assertFalse((root / leftover).exists(), leftover)

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

    # Writes a complete policy capture in the layout `consume-php-policy fetch` leaves
    # and returns its manifest path and the decision `compare` would write for it.
    def capture_fixture(self, root, branches=("8.5", "8.6"), action_key="new_branch:8.6"):
        commit_sha = "a" * 40
        invariants = root / "policy-invariants.json"
        invariants.write_text('{"schemaVersion":1}\n')
        policy = root / "support-policy.json"
        policy.write_text(json.dumps({"maintainedBranches": list(branches), "actionKey": action_key}) + "\n")
        bodies = {
            "php_bin_policy_selector": (root / "php-bin-main-selector.json", [{"sha": commit_sha}]),
            "php_bin_state": (root / "php-bin-main.json", {"sha": commit_sha}),
        }
        records = []
        for capture_id, (path, body) in bodies.items():
            path.write_text(json.dumps(body) + "\n")
            records.append({"captureId": capture_id, "bodyPath": path.name, "digest": digest_file(path)})
        records.append({"captureId": "support_policy", "bodyPath": policy.name, "digest": digest_file(policy)})
        records.append({"captureId": "policy_invariants", "bodyPath": invariants.name, "digest": digest_file(invariants)})
        manifest = root / "policy-capture.json"
        manifest.write_text(json.dumps({"schemaVersion": 1, "captures": records}) + "\n")
        decision = {
            "schemaVersion": 1,
            "trigger": "policy_changed",
            "actionKey": action_key,
            "policyDigest": digest_file(policy),
            "policyInvariantsDigest": digest_file(invariants),
            "phpBinPolicyCommit": commit_sha,
            "synchronize": True,
        }
        return manifest, decision

    def test_synchronization_plan_is_bound_to_the_complete_policy_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            manifest, decision = self.capture_fixture(root)
            plan = synchronization_plan(decision, manifest, "d" * 40, "e" * 40, "enabled")
            self.assertEqual("new_branch:8.6", plan["actionKey"])
            self.assertEqual({"mise-php": ["lib/policy.lua", "support-snapshot.json"]}, plan["allowedPaths"])
            self.assertEqual(
                {
                    "misePhpHead": "d" * 40,
                    "phpBinPolicyCommit": "a" * 40,
                    "supportPolicyDigest": decision["policyDigest"],
                    "policyInvariantsDigest": decision["policyInvariantsDigest"],
                    "phpBinOperatorCommit": "e" * 40,
                    "operatorState": "enabled",
                },
                plan["preconditions"],
            )
            self.assertEqual(4, len(plan["evidenceDigests"]))
            rejected = (
                ({**decision, "trigger": "quiet"}, "enabled"),
                ({**decision, "actionKey": "bootstrap"}, "enabled"),
                ({**decision, "policyDigest": "sha256:" + "f" * 64}, "enabled"),
                ({**decision, "phpBinPolicyCommit": "b" * 40}, "enabled"),
                (decision, "paused"),
            )
            for bad_decision, state in rejected:
                with self.assertRaises(ConsumerError, msg=(bad_decision, state)):
                    synchronization_plan(bad_decision, manifest, "d" * 40, "e" * 40, state)
            with self.assertRaises(ConsumerError):
                synchronization_plan(decision, manifest, "main", "e" * 40, "enabled")
            (root / "php-bin-main.json").write_text('{"sha":"' + "b" * 40 + '"}\n')
            with self.assertRaisesRegex(ConsumerError, "captured body changed"):
                synchronization_plan(decision, manifest, "d" * 40, "e" * 40, "enabled")
            document = json.loads(manifest.read_text())
            document["captures"] = document["captures"][1:]
            manifest.write_text(json.dumps(document))
            with self.assertRaisesRegex(ConsumerError, "incomplete"):
                synchronization_plan(decision, manifest, "d" * 40, "e" * 40, "enabled")

    def test_synchronize_regenerates_the_snapshot_in_its_reviewed_layout(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        snapshot = root / "support-snapshot.json"
        self.assertEqual(snapshot.read_text(), render_snapshot(json.loads(snapshot.read_text())))
        with tempfile.TemporaryDirectory() as temporary:
            work = pathlib.Path(temporary)
            manifest, decision = self.capture_fixture(work)
            plan = synchronization_plan(decision, manifest, "d" * 40, "e" * 40, "enabled")
            target = work / "support-snapshot.json.out"
            written = synchronize(plan, work / "support-policy.json", target)
            self.assertEqual(["8.5", "8.6"], written["maintainedBranches"])
            self.assertEqual(render_snapshot(written), target.read_text())
            self.assertEqual("a" * 40, json.loads(target.read_text())["phpBinPolicyCommit"])
            (work / "support-policy.json").write_text('{"maintainedBranches":["8.6"]}\n')
            with self.assertRaisesRegex(ConsumerError, "not the one the plan is bound to"):
                synchronize(plan, work / "support-policy.json", target)

    def test_synchronization_end_to_end_seals_exactly_the_generated_pair(self):
        project = pathlib.Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            repo = root / "repo"
            (repo / "lib").mkdir(parents=True)
            (repo / "scripts").mkdir()
            shutil.copy(project / "support-snapshot.json", repo / "support-snapshot.json")
            shutil.copy(project / "lib/policy.lua", repo / "lib/policy.lua")
            shutil.copy(project / "scripts/generate-policy-lua", repo / "scripts/generate-policy-lua")
            for arguments in (["init", "-q", "-b", "main"], ["config", "user.name", "test"],
                              ["config", "user.email", "test@invalid"], ["add", "-A"], ["commit", "-q", "-m", "base"]):
                subprocess.run(["git", *arguments], cwd=repo, check=True)
            base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True,
                                  stdout=subprocess.PIPE).stdout.strip()
            # validate runs this suite at every sealed commit the consumer produces, so the
            # added branch is derived from the live snapshot rather than named: a snapshot
            # that already lists 8.6 must not make the fixture policy non-canonical.
            maintained = json.loads((project / "support-snapshot.json").read_text())["maintainedBranches"]
            major, minor = maintained[-1].split(".")
            added = f"{major}.{int(minor) + 1}"
            branches = [*maintained, added]
            manifest, decision = self.capture_fixture(root, branches, f"new_branch:{added}")
            plan = synchronization_plan(decision, manifest, base, "e" * 40, "enabled")
            synchronize(plan, root / "support-policy.json", repo / "support-snapshot.json")
            subprocess.run([str(repo / "scripts/generate-policy-lua")], check=True)
            sealed = seal(repo, base, plan, root / "support-policy.json", root / "sealed")
            self.assertEqual(
                ["lib/policy.lua", "support-snapshot.json"], [item["path"] for item in sealed["files"]]
            )
            self.assertIn(f'"{added}",', (repo / "lib/policy.lua").read_text())

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
        return repo, base, {
            "plan": {
                "actionKey": "new_branch:8.6",
                "preconditions": preconditions,
                "allowedPaths": {"mise-php": ["support-snapshot.json", "lib/policy.lua"]},
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

    def test_protected_controls_pass_owner_authored_changes_before_bot_exemptions(self):
        # The owner short-circuit must sit after the no-protected-path exit and
        # before the automation exemption, so it can never widen what a bot
        # identity is allowed to merge.
        root = pathlib.Path(__file__).resolve().parents[1]
        protected_workflow = (root / ".github/workflows/protected-controls.yml").read_text()
        owner_pass = protected_workflow.index("if author.lower() == reviewer:")
        self.assertLess(protected_workflow.index("No protected control path changed."), owner_pass)
        self.assertLess(owner_pass, protected_workflow.index('re.fullmatch(r"autorelease/readiness-'))

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
