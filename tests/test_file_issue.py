#!/usr/bin/env python3
"""Tests for file_issue (deduplication, capping, labeling, dry run default)."""
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

from devloops_lib import file_issue, producer
from devloops_lib.file_issue import file_producer_issues

# Import CLI main
import importlib.util
_script_path = REPO_ROOT / "skills" / "producer-loop" / "scripts" / "file_issue.py"
_spec = importlib.util.spec_from_file_location("file_issue_script", _script_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
file_issue_main = _mod.main


class FileIssueTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.state_file = self.tmp_dir / "state.json"
        self.cfg = {
            "project": {"repo": "my-org/my-repo"},
            "labels": {"producer": "auto:producer"},
            "producer": {
                "max_new_issues_per_run": 2,
                "task_max_new_issues": {"cleanup": 1},
            },
        }

    def test_dedup_from_state(self):
        """Skips filing if the fingerprint is already present in state.json."""
        task = "security"
        key = "sql-injection:src/db.py:100"
        fp = producer.compute_fingerprint(task, key)

        state = {"fingerprints": {fp: {"task": task, "key": key}}}
        self.state_file.write_text(json.dumps(state), encoding="utf-8")

        findings = [{"task": task, "key": key, "title": "SQL Injection", "body": "details"}]
        result = file_producer_issues(
            findings=findings,
            task=task,
            config=self.cfg,
            state_path=self.state_file,
            create=False,
            existing_fps=set(),
        )

        self.assertEqual(len(result["created"]), 0)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(result["skipped"][0]["reason"], "fingerprint in state")
        self.assertEqual(result["skipped"][0]["fp"], fp)

    def test_dedup_from_issue_marker(self):
        """Skips filing if any existing issue contains the marker <!-- producer:fp=<sha> -->."""
        task = "security"
        key = "exposed-token:src/api.py:42"
        fp = producer.compute_fingerprint(task, key)

        findings = [{"task": task, "key": key, "title": "Exposed Token", "body": "details"}]
        existing_fps = {fp}

        result = file_producer_issues(
            findings=findings,
            task=task,
            config=self.cfg,
            state_path=self.state_file,
            create=False,
            existing_fps=existing_fps,
        )

        self.assertEqual(len(result["created"]), 0)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(result["skipped"][0]["reason"], "fingerprint in existing issue")
        self.assertEqual(result["skipped"][0]["fp"], fp)

    def test_cap_new_issues_per_run(self):
        """Caps newly filed issues per run at max_new_issues_per_run (with per-task override)."""
        task = "security"  # limit is 2 from config
        findings = [
            {"task": task, "key": f"vuln-{i}", "title": f"Vuln {i}", "body": f"details {i}"}
            for i in range(5)
        ]

        result = file_producer_issues(
            findings=findings,
            task=task,
            config=self.cfg,
            state_path=self.state_file,
            create=False,
            existing_fps=set(),
        )

        self.assertEqual(len(result["created"]), 2)
        self.assertEqual(len(result["skipped"]), 3)
        for skipped in result["skipped"]:
            self.assertIn("cap reached", skipped["reason"])

        # Test per-task override (cleanup is capped at 1)
        cleanup_findings = [
            {"task": "cleanup", "key": f"branch-{i}", "title": f"Branch {i}", "body": "details"}
            for i in range(3)
        ]
        cleanup_res = file_producer_issues(
            findings=cleanup_findings,
            task="cleanup",
            config=self.cfg,
            state_path=self.state_file,
            create=False,
            existing_fps=set(),
        )
        self.assertEqual(len(cleanup_res["created"]), 1)
        self.assertEqual(len(cleanup_res["skipped"]), 2)

    def test_labels_attached_properly(self):
        """Attaches labels.producer and auto:<task> to created issues."""
        task = "e2e"
        findings = [{"task": task, "key": "broken-flow", "title": "E2E Failure", "body": "step failed"}]

        result = file_producer_issues(
            findings=findings,
            task=task,
            config=self.cfg,
            state_path=self.state_file,
            create=False,
            existing_fps=set(),
        )

        self.assertEqual(len(result["created"]), 1)
        created_item = result["created"][0]
        self.assertIn("auto:producer", created_item["labels"])
        self.assertIn("auto:e2e", created_item["labels"])
        # Body contains fingerprint marker
        fp = producer.compute_fingerprint(task, "broken-flow")
        self.assertIn(f"<!-- producer:fp={fp} -->", created_item["body"])

    @patch("devloops_lib.file_issue.subprocess.run")
    def test_dry_run_default_vs_create(self, mock_run):
        """Dry run does not invoke gh create or mutate state; --create does."""
        task = "uiux"
        key = "missing-alt"
        fp = producer.compute_fingerprint(task, key)

        findings = [{"task": task, "key": key, "title": "Missing Alt", "body": "image has no alt"}]

        # 1. Dry run (default create=False)
        result_dry = file_producer_issues(
            findings=findings,
            task=task,
            config=self.cfg,
            state_path=self.state_file,
            create=False,
            existing_fps=set(),
        )
        self.assertTrue(result_dry["dry_run"])
        self.assertEqual(len(result_dry["created"]), 1)
        mock_run.assert_not_called()
        self.assertFalse(self.state_file.exists())

        # 2. Actual creation (create=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="https://github.com/my-org/my-repo/issues/10")
        result_create = file_producer_issues(
            findings=findings,
            task=task,
            config=self.cfg,
            state_path=self.state_file,
            create=True,
            existing_fps=set(),
        )
        self.assertFalse(result_create["dry_run"])
        self.assertEqual(len(result_create["created"]), 1)
        mock_run.assert_called_once()
        # Verify gh call arguments
        call_args = mock_run.call_args[0][0]
        self.assertEqual(call_args[0], "gh")
        self.assertEqual(call_args[1], "issue")
        self.assertEqual(call_args[2], "create")
        self.assertIn("--label", call_args)
        self.assertIn("auto:producer", call_args)
        self.assertIn("auto:uiux", call_args)

        # Verify state.json was written with the fingerprint
        self.assertTrue(self.state_file.exists())
        saved_state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertIn(fp, saved_state["fingerprints"])


if __name__ == "__main__":
    unittest.main()
