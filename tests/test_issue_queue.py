#!/usr/bin/env python3
"""Tests for gatekeeper issue_queue (unjudged selection, skipping judged, safety rules)."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloops_lib import issue_queue
from devloops_lib.issue_queue import apply_verdict, filter_unjudged_producer_issues

# Import CLI main
import importlib.util
_script_path = REPO_ROOT / "skills" / "gatekeeper-loop" / "scripts" / "issue_queue.py"
_spec = importlib.util.spec_from_file_location("issue_queue_script", _script_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
issue_queue_main = _mod.main


class IssueQueueTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.state_file = self.tmp_dir / "state.json"
        self.cfg = {
            "project": {"repo": "my-org/my-repo"},
            "labels": {
                "producer": "auto:producer",
                "validated": "validated",
                "rejected": "rejected",
                "approved": "approved",
            },
        }

    def test_unjudged_selection_and_judged_skipped(self):
        """Selects producer issues lacking validated/rejected, skips judged and non-producer issues."""
        issues = [
            # 1. Unjudged producer issue -> KEEP
            {
                "number": 101,
                "title": "SQL Injection found",
                "labels": [{"name": "auto:producer"}, {"name": "auto:security"}],
                "body": "Details...",
            },
            # 2. Already validated -> SKIP
            {
                "number": 102,
                "title": "Doc drift",
                "labels": [{"name": "auto:producer"}, {"name": "auto:doc-drift"}, {"name": "validated"}],
                "body": "Details...",
            },
            # 3. Already rejected -> SKIP
            {
                "number": 103,
                "title": "UI UX defect",
                "labels": [{"name": "auto:producer"}, {"name": "auto:uiux"}, {"name": "rejected"}],
                "body": "Details...",
            },
            # 4. Human issue (no producer label) -> SKIP
            {
                "number": 104,
                "title": "Human reported issue",
                "labels": [{"name": "bug"}],
                "body": "Details...",
            },
            # 5. Another unjudged producer issue -> KEEP
            {
                "number": 105,
                "title": "Dead branch cleanup",
                "labels": [{"name": "auto:producer"}, {"name": "auto:cleanup"}],
                "body": "Details...",
            },
        ]

        unjudged = filter_unjudged_producer_issues(
            issues,
            producer_label=self.cfg["labels"]["producer"],
            validated_label=self.cfg["labels"]["validated"],
            rejected_label=self.cfg["labels"]["rejected"],
        )

        self.assertEqual(len(unjudged), 2)
        numbers = [item["number"] for item in unjudged]
        self.assertEqual(numbers, [101, 105])
        self.assertEqual(unjudged[0]["task"], "security")
        self.assertEqual(unjudged[1]["task"], "cleanup")

    def test_never_adds_approved(self):
        """Under no circumstances may gatekeeper issue validation add the approved label."""
        with self.assertRaises(ValueError):
            apply_verdict(
                issue_number=101,
                verdict="approved",
                reason="Looks good",
                evidence="src/auth.py:42",
                config=self.cfg,
                state_path=self.state_file,
                apply=False,
            )

    @patch("devloops_lib.issue_queue.subprocess.run")
    def test_never_closes_issue_and_applies_verdict(self, mock_run):
        """Applies verdict and edits body/label without ever calling 'gh issue close'."""
        mock_run.return_value = MagicMock(returncode=0, stdout="")

        fake_issue = {
            "number": 101,
            "title": "SQL Injection found",
            "body": "Initial description.",
            "labels": [{"name": "auto:producer"}, {"name": "auto:security"}],
        }

        # Apply verdict 'validated'
        result = apply_verdict(
            issue_number=101,
            verdict="validated",
            reason="Confirmed unsafe query formatting",
            evidence="src/db.py:55",
            config=self.cfg,
            state_path=self.state_file,
            apply=True,
            existing_issue=fake_issue,
        )

        self.assertEqual(result["verdict"], "validated")
        self.assertEqual(result["label"], "validated")
        self.assertEqual(result["task"], "security")

        # Verify calls to subprocess: should only call 'gh issue edit', never 'gh issue close'
        for call in mock_run.call_args_list:
            args = call[0][0]
            self.assertNotEqual(args[:3], ["gh", "issue", "close"])
            if args[:3] == ["gh", "issue", "edit"]:
                self.assertIn("--add-label", args)
                self.assertIn("validated", args)
                self.assertNotIn("approved", args)

        # Verify state recording
        self.assertTrue(self.state_file.exists())
        saved_state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(saved_state["producerStats"]["security"]["validated"], 1)
        self.assertEqual(saved_state["producerStats"]["security"]["rejected"], 0)

        # Apply another verdict 'rejected' on another security issue
        fake_issue_2 = {
            "number": 102,
            "title": "False alarm",
            "body": "Not a vulnerability.",
            "labels": [{"name": "auto:producer"}, {"name": "auto:security"}],
        }
        apply_verdict(
            issue_number=102,
            verdict="rejected",
            reason="Input is properly sanitized by middleware",
            evidence="src/sanitize.py:20",
            config=self.cfg,
            state_path=self.state_file,
            apply=True,
            existing_issue=fake_issue_2,
        )

        saved_state2 = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(saved_state2["producerStats"]["security"]["validated"], 1)
        self.assertEqual(saved_state2["producerStats"]["security"]["rejected"], 1)


if __name__ == "__main__":
    unittest.main()
