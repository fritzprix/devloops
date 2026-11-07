#!/usr/bin/env python3
"""Tests for the worker tick plan and per-item re-checks."""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloops_lib import config
from devloops_lib.gh import GhError
from devloops_lib.tick_gate import EXIT_ERROR, EXIT_NOOP, EXIT_RUN

_path = REPO_ROOT / "skills" / "worker-loop" / "scripts" / "plan_tick.py"
_spec = importlib.util.spec_from_file_location("worker_plan_tick", _path)
plan_tick = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plan_tick)


def _candidate(number, status="none", updated="t1"):
    return {
        "number": number, "title": f"Issue {number}", "updatedAt": updated,
        "dependency": {"status": status, "dependsOn": [], "open": [], "stackOn": None},
    }


def _candidates(*entries, reached=False):
    return {"openPRLimit": {"unapproved": 1, "limit": 5, "reached": reached}, "candidates": list(entries), "excluded": []}


class BuildPlanTest(unittest.TestCase):
    def _keys(self, plan):
        return [(item["kind"], item["number"]) for item in plan["items"]]

    def test_plan_has_prs_in_priority_order_then_issues(self):
        prs = [
            {"number": 3, "nextAction": "fix-ci"},
            {"number": 4, "nextAction": "wait-ci"},
            {"number": 5, "nextAction": "none"},
            {"number": 6, "nextAction": "resolve-conflict"},
            {"number": 7, "nextAction": "triage-comments"},
            {"number": 8, "nextAction": "restack"},
            {"number": 9, "nextAction": "address-gatekeeper"},
        ]
        plan = plan_tick.build_plan(prs, _candidates(_candidate(1), _candidate(2)), {}, 3)
        self.assertEqual(self._keys(plan), [
            ("pr", 6), ("pr", 8), ("pr", 9), ("pr", 7), ("pr", 3), ("issue", 1), ("issue", 2),
        ])
        self.assertEqual(plan["items"][0]["action"], "resolve-conflict")
        self.assertEqual(plan["maxItems"], 3)

    def test_dependent_issue_waits_for_its_dependency(self):
        plan = plan_tick.build_plan([], _candidates(_candidate(1), _candidate(2, status="wait")), {}, 1)
        self.assertEqual(self._keys(plan), [("issue", 1)])
        self.assertEqual(plan["waiting"], [2])

    def test_stacked_issue_is_in_the_plan_with_its_base(self):
        entry = _candidate(1, status="stack")
        entry["dependency"]["stackOn"] = {"pr": 9, "head": "feat/x", "approved": False}
        plan = plan_tick.build_plan([], _candidates(entry), {}, 1)
        self.assertEqual(plan["items"][0]["dependency"]["stackOn"]["head"], "feat/x")

    def test_skip_applies_only_while_the_issue_is_unchanged(self):
        skipped = {"1": {"updatedAt": "t1"}, "2": {"updatedAt": "t0"}}
        plan = plan_tick.build_plan([], _candidates(_candidate(1), _candidate(2)), skipped, 1)
        self.assertEqual(self._keys(plan), [("issue", 2)])

    def test_pr_limit_removes_issues_but_keeps_prs(self):
        prs = [{"number": 3, "nextAction": "fix-ci"}]
        plan = plan_tick.build_plan(prs, _candidates(_candidate(1), reached=True), {}, 2)
        self.assertEqual(self._keys(plan), [("pr", 3)])


class RecheckTest(unittest.TestCase):
    def test_pr_is_valid_only_with_the_planned_action(self):
        self.assertTrue(plan_tick.pr_item_is_current([{"nextAction": "fix-ci"}], "fix-ci"))
        self.assertFalse(plan_tick.pr_item_is_current([{"nextAction": "none"}], "fix-ci"))
        self.assertFalse(plan_tick.pr_item_is_current([], "fix-ci"))

    def test_issue_that_changed_during_the_tick_is_not_valid(self):
        fresh = _candidates(_candidate(1, updated="t1"), _candidate(2, status="wait"))
        self.assertTrue(plan_tick.issue_item_is_current(fresh, 1, "t1"))
        self.assertFalse(plan_tick.issue_item_is_current(fresh, 1, "t0"))
        self.assertFalse(plan_tick.issue_item_is_current(fresh, 2, "t1"))
        self.assertFalse(plan_tick.issue_item_is_current(fresh, 3, "t1"))


class MainTest(unittest.TestCase):
    def _run(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = plan_tick.main(list(argv))
        return code, out.getvalue()

    def test_check_exit_codes(self):
        with patch.object(plan_tick, "summarize_prs", return_value=[{"nextAction": "none"}]):
            self.assertEqual(self._run("--check-pr", "3", "--action", "fix-ci"), (EXIT_NOOP, "CHANGED\n"))
        with patch.object(plan_tick, "fetch_candidates", return_value=_candidates(_candidate(1))):
            self.assertEqual(self._run("--check-issue", "1", "--updated-at", "t1"), (EXIT_RUN, "VALID\n"))
        with patch.object(plan_tick, "summarize_prs", side_effect=GhError("down")):
            self.assertEqual(self._run("--check-pr", "3", "--action", "fix-ci")[0], EXIT_ERROR)

    def test_plan_uses_state_and_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text(json.dumps({"skipped": {"1": {"updatedAt": "t1"}}}), encoding="utf-8")
            with patch.object(plan_tick, "summarize_prs", return_value=[]), \
                    patch.object(plan_tick, "fetch_candidates", return_value=_candidates(_candidate(1), _candidate(2))), \
                    patch.object(plan_tick, "load_config", return_value={"worker": {"max_items_per_tick": 4}}):
                code, text = self._run("--state-file", str(state))
        plan = json.loads(text)
        self.assertEqual(code, EXIT_RUN)
        self.assertEqual(plan["maxItems"], 4)
        self.assertEqual([item["number"] for item in plan["items"]], [2])

    def test_max_items_default_and_floor(self):
        self.assertEqual(config.DEFAULT_CONFIG["worker"]["max_items_per_tick"], 1)
        self.assertEqual(plan_tick.max_items_per_tick({}), 1)
        self.assertEqual(plan_tick.max_items_per_tick({"worker": {"max_items_per_tick": 0}}), 1)
        parsed = config.parse_yaml_subset("worker:\n  max_items_per_tick: 3\n")
        self.assertEqual(plan_tick.max_items_per_tick(parsed), 3)


if __name__ == "__main__":
    unittest.main()
