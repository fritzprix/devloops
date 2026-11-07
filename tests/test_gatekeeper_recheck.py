#!/usr/bin/env python3
"""Tests for the gatekeeper's pre-post head re-check and issue validation cap."""
import contextlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloops_lib import config
from devloops_lib.gh import GhError


def _load(name):
    path = REPO_ROOT / "skills" / "gatekeeper-loop" / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"gatekeeper_recheck_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


review_queue = _load("review_queue")
issue_queue = _load("issue_queue")

PR = {"state": "OPEN", "headRefOid": "abc", "baseRefName": "main"}


class CheckHeadTest(unittest.TestCase):
    def test_current_only_when_open_with_the_reviewed_head_and_base(self):
        self.assertTrue(review_queue.head_is_current(PR, "abc", "main"))
        self.assertTrue(review_queue.head_is_current(PR, "abc", None))
        self.assertFalse(review_queue.head_is_current(PR, "old", "main"))
        self.assertFalse(review_queue.head_is_current(PR, "abc", "dev"))
        self.assertFalse(review_queue.head_is_current({**PR, "state": "MERGED"}, "abc", "main"))

    def _run(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), \
                self.assertRaises(SystemExit) as cm:
            review_queue.main(list(argv))
        return cm.exception.code, out.getvalue()

    def test_exit_codes_keep_changed_and_error_apart(self):
        with patch.object(review_queue, "gh", return_value=json.dumps(PR)):
            self.assertEqual(self._run("--check-head", "7", "--head", "abc", "--base", "main"), (0, "VALID\n"))
            code, text = self._run("--check-head", "7", "--head", "old")
            self.assertEqual(code, 1)
            self.assertIn("CHANGED", text)
        # An unreachable GitHub must not read as "changed" or "valid".
        with patch.object(review_queue, "gh", side_effect=GhError("down")):
            self.assertEqual(self._run("--check-head", "7", "--head", "abc")[0], 3)

    def test_check_head_needs_the_reviewed_head(self):
        self.assertEqual(self._run("--check-head", "7")[0], 2)


class IssueValidationCapTest(unittest.TestCase):
    ISSUES = [{"number": n, "title": str(n), "task": "security"} for n in (1, 2, 3)]

    def test_cap_defaults_to_one_and_has_a_floor(self):
        self.assertEqual(config.DEFAULT_CONFIG["gatekeeper"]["max_issue_validations_per_tick"], 1)
        self.assertEqual(len(issue_queue.validation_batch(self.ISSUES, config.DEFAULT_CONFIG)), 1)
        self.assertEqual(len(issue_queue.validation_batch(self.ISSUES, {})), 1)
        zero = {"gatekeeper": {"max_issue_validations_per_tick": 0}}
        self.assertEqual(len(issue_queue.validation_batch(self.ISSUES, zero)), 1)

    def test_cap_comes_from_config_and_keeps_queue_order(self):
        parsed = config.parse_yaml_subset("gatekeeper:\n  max_issue_validations_per_tick: 2\n")
        self.assertEqual([i["number"] for i in issue_queue.validation_batch(self.ISSUES, parsed)], [1, 2])

    def _run_batch(self, issues, cfg):
        out = io.StringIO()
        with patch.object(issue_queue, "list_unjudged_issues", return_value=issues), \
                patch.object(issue_queue, "load_config", return_value=cfg), \
                contextlib.redirect_stdout(out):
            code = issue_queue.main(["--batch"])
        return code, out.getvalue()

    def test_batch_prints_json_or_noop(self):
        code, text = self._run_batch(self.ISSUES, {"gatekeeper": {"max_issue_validations_per_tick": 2}, "state_dir": ".x"})
        self.assertEqual(code, 0)
        self.assertEqual([i["number"] for i in json.loads(text)], [1, 2])
        self.assertEqual(self._run_batch([], {"state_dir": ".x"}), (1, "NOOP\n"))


if __name__ == "__main__":
    unittest.main()
