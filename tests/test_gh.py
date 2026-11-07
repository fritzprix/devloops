#!/usr/bin/env python3
"""Tests for the unified gh helper module and consistent error handling across scripts."""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloops_lib import file_issue, issue_queue, pr_status, tick_gate
from devloops_lib.gh import GhError, gh


class GhHelperTest(unittest.TestCase):
    def test_gh_success_returns_stdout(self):
        with patch("devloops_lib.gh.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout='{"login": "testuser"}\n', stderr="")
            out = gh("api", "user")
            self.assertEqual(out, '{"login": "testuser"}\n')
            mock_run.assert_called_once_with(
                ["gh", "api", "user"], capture_output=True, text=True, encoding="utf-8"
            )

    def test_gh_nonzero_exit_raises_gh_error(self):
        with patch("devloops_lib.gh.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="authentication failed")
            with self.assertRaises(GhError) as cm:
                gh("repo", "view")
            self.assertIn("authentication failed", str(cm.exception))
            self.assertIn("code 1", str(cm.exception))

    def test_gh_command_not_found_raises_gh_error(self):
        with patch("devloops_lib.gh.subprocess.run", side_effect=FileNotFoundError("gh not found")):
            with self.assertRaises(GhError) as cm:
                gh("repo", "view")
            self.assertIn("Failed to execute gh", str(cm.exception))

    def test_failing_gh_gives_same_error_type_across_scripts(self):
        """Requirement 5: A failing gh call gives the same error type (GhError) from each script."""
        with patch("devloops_lib.gh.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="simulated gh failure")

            # 1. pr_status
            with self.assertRaises(GhError):
                pr_status.summarize(10, "owner", "repo", "me")

            # 2. tick_gate
            with self.assertRaises(GhError):
                tick_gate.compute_fingerprint()

            # 3. file_issue (get_existing_issue_fingerprints)
            with self.assertRaises(GhError):
                file_issue.get_existing_issue_fingerprints()

            # 4. file_issue (create_github_issue)
            with self.assertRaises(GhError):
                file_issue.create_github_issue("Title", "Body", ["label"])

            # 5. issue_queue (list_unjudged_issues)
            with self.assertRaises(GhError):
                issue_queue.list_unjudged_issues(config={})

            # 6. issue_queue (apply_verdict)
            with self.assertRaises(GhError):
                issue_queue.apply_verdict(42, "validated", "reason", "evidence:1", apply=True, config={})


if __name__ == "__main__":
    unittest.main()
