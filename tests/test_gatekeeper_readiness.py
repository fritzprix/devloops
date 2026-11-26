#!/usr/bin/env python3
"""Tests for gatekeeper readiness: CI/rubric gap assessment (issue #81)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from devloops_lib import gatekeeper_readiness as gr


class GatekeeperReadinessTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_missing_ci_detected(self):
        self.assertFalse(gr.has_pr_ci_workflow(self.root))
        (self.root / ".github" / "workflows").mkdir(parents=True)
        (self.root / ".github" / "workflows" / "push.yml").write_text(
            "on:\n  push:\n    branches: [main]\n",
            encoding="utf-8",
        )
        self.assertFalse(gr.has_pr_ci_workflow(self.root))
        (self.root / ".github" / "workflows" / "ci.yml").write_text(
            "on:\n  pull_request:\n  push:\n",
            encoding="utf-8",
        )
        self.assertTrue(gr.has_pr_ci_workflow(self.root))

    def test_missing_rubric(self):
        out = gr.assess_rubric(self.root, {})
        self.assertEqual(out["status"], "missing")
        self.assertEqual(out["action"], "open_issue")

    def test_present_rubric(self):
        text = (
            "# Review rubric\n\n"
            "## Acceptance criteria\n"
            "Each PR must pass tests and meet checklist items.\n\n"
            "## Judgment procedure\n"
            "Review the diff; mark each criterion pass or fail with file:line evidence.\n"
        )
        (self.root / "REVIEW_RUBRIC.md").write_text(text, encoding="utf-8")
        out = gr.assess_rubric(self.root, {})
        self.assertEqual(out["status"], "present")

    def test_ambiguous_rubric_files_needs_owner_issue(self):
        (self.root / "REVIEW_RUBRIC.md").write_text(
            "# Rubric\nCriteria TBD — owner must decide what counts as done.\n" * 5,
            encoding="utf-8",
        )
        out = gr.assess_rubric(self.root, {})
        self.assertEqual(out["status"], "ambiguous")
        self.assertEqual(out["action"], "open_issue")
        self.assertIn("needs-owner", out["goal"])

    def test_assess_readiness_lists_gaps_without_autonomous_prs(self):
        cfg = {"commands": {"test": "pytest", "lint": "ruff check"}}
        ready = gr.assess_readiness(start_dir=self.root, config=cfg)
        self.assertFalse(ready["ci"]["present"])
        kinds = [g["kind"] for g in ready["gaps"]]
        self.assertIn("ci", kinds)
        self.assertIn("rubric", kinds)
        # Issue #81: no bootstrap-PR work list — gaps become issues, not PRs.
        self.assertNotIn("autonomousWork", ready)
        for gap in ready["gaps"]:
            self.assertEqual(gap["action"], "open_issue")


if __name__ == "__main__":
    unittest.main()
