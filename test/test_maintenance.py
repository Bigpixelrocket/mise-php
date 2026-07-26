import pathlib
import tempfile
import unittest

from maintenance.admission import AdmissionError, protected
from maintenance.consumer import compare, digest, readiness, write


class MaintenanceConsumerTests(unittest.TestCase):
    def test_opaque_policy_comparison(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            policy = root / "policy.json"
            commit = root / "commit.json"
            snapshot = root / "snapshot.json"
            events = root / "events"
            events.mkdir()
            policy.write_text('{"maintainedBranches":["8.5"]}\n')
            commit.write_text('{"sha":"' + "a" * 40 + '"}\n')
            write(snapshot, {"policyDigest": digest(policy.read_bytes())})
            result = compare(policy, commit, snapshot, events)
            self.assertEqual("quiet", result["trigger"])
            policy.write_text('{"maintainedBranches":["8.5","8.6"]}\n')
            self.assertEqual("policy_changed", compare(policy, commit, snapshot, events)["trigger"])

    def test_readiness_requires_exact_commits_and_digests(self):
        result = readiness(
            "new_branch:8.6",
            "a" * 40,
            "sha256:" + "b" * 64,
            "c" * 40,
            ["sha256:" + "d" * 64],
        )
        self.assertTrue(result["ready"])
        with self.assertRaises(Exception):
            readiness("new_branch:8.6", "main", "bad", "main", [])

    def test_protected_controls_are_not_admissible(self):
        self.assertTrue(protected(".github/workflows/maintenance.yml"))
        self.assertTrue(protected("maintenance/admission.py"))
        self.assertFalse(protected("lib/releases.lua"))


if __name__ == "__main__":
    unittest.main()
