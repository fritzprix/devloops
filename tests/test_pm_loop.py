#!/usr/bin/env python3
"""Tests for pm-loop command parsing, schedule exclusivity, induction, and budget."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloops_lib import pm_budget, pm_command, pm_induction, pm_invoke, pm_schedule
from devloops_lib.tick_gate import load_state, save_state


class PmCommandTest(unittest.TestCase):
    def test_parse_minimal_tick(self):
        p = pm_command.parse_pm_invoke("/pm-loop")
        self.assertEqual(p.action, "tick")
        self.assertIsNone(p.interval_seconds)
        self.assertIsNone(p.slots)

    def test_parse_interval_slots_delegation(self):
        p = pm_command.parse_pm_invoke("/pm-loop 10m slots: 3 delegation: claude")
        self.assertEqual(p.action, "tick")
        self.assertEqual(p.interval_seconds, 600)
        self.assertEqual(p.slots, 3)
        self.assertEqual(p.delegation, "claude")

    def test_parse_status_and_stop(self):
        self.assertEqual(pm_command.parse_pm_invoke("/pm-loop status").action, "status")
        self.assertEqual(pm_command.parse_pm_invoke("/pm-loop stop").action, "stop")

    def test_invalid_delegation_raises(self):
        with self.assertRaises(ValueError):
            pm_command.parse_pm_invoke("/pm-loop delegation: unknown")


class PmBudgetTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_file = Path(self._tmp.name) / "state.json"

    def test_persist_and_prompt_once(self):
        cfg = {"pm": {"default_interval": "10m"}}
        parsed = pm_command.parse_pm_invoke("/pm-loop 10m slots: 2 delegation: cursor")
        state, prompts = pm_budget.apply_invoke({}, parsed, cfg)
        self.assertEqual(prompts, [])
        self.assertEqual(state["budget"]["slots"], 2)
        self.assertEqual(state["budget"]["delegation"], "cursor")
        self.assertEqual(state["budget"]["intervalSeconds"], 600)

        bare, prompts2 = pm_budget.apply_invoke(state, pm_command.parse_pm_invoke("/pm-loop"), cfg)
        self.assertEqual(prompts2, [])
        self.assertEqual(bare["budget"]["slots"], 2)

    def test_missing_budget_prompts(self):
        cfg = {"pm": {"default_interval": "10m"}}
        state, prompts = pm_budget.apply_invoke({}, pm_command.parse_pm_invoke("/pm-loop"), cfg)
        self.assertEqual(set(prompts), {"slots", "delegation"})

    def test_record_user_answer(self):
        state = pm_budget.record_user_budget_answer({}, "slots", "3")
        state = pm_budget.record_user_budget_answer(state, "delegation", "harness")
        self.assertTrue(pm_budget.budget_is_complete(state["budget"]))


class PmScheduleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_exclusivity_conflict_when_peers_armed(self):
        pm_state = {"scheduler": {"kind": "shell", "intervalSeconds": 600}}
        for loop in pm_schedule.PEER_LOOPS:
            path = self.root / loop / "state.json"
            save_state(path, {"scheduler": {"kind": "cron", "armed": True}})
        report = pm_schedule.exclusivity_report(pm_state, state_root=self.root)
        self.assertFalse(report["exclusiveOk"])
        self.assertEqual(set(report["peerConflicts"]), set(pm_schedule.PEER_LOOPS))

    def test_exclusivity_ok_without_pm_scheduler(self):
        save_state(self.root / "worker-loop" / "state.json", {"scheduler": {"kind": "process", "id": 99}})
        report = pm_schedule.exclusivity_report({}, state_root=self.root)
        self.assertTrue(report["exclusiveOk"])

    def test_peer_tick_blocked_under_pm_cascade(self):
        self.assertFalse(pm_schedule.peer_tick_allowed({"schedule": {"pm": {"pmCascadeActive": True}}}))


class PmInductionTest(unittest.TestCase):
    def test_policy_priority_skill_over_folklore(self):
        winner = pm_induction.resolve_policy_source(
            [
                {"source": "folklore", "rule": "merge when green"},
                {"source": "skill", "rule": "human merges only"},
            ]
        )
        self.assertEqual(winner["winner"]["source"], "skill")

    def test_profile_does_not_contain_budget(self):
        profile = pm_induction.build_project_profile(config={"project": {"repo": "o/r"}}, signals={"hasCi": True})
        self.assertIn("humanOnly", profile)
        self.assertNotIn("slots", json.dumps(profile))


class PmInvokeIntegrationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_file = Path(self._tmp.name) / "state.json"
        self.root = Path(self._tmp.name)
        (self.root / "devloops.config.yaml").write_text("state_dir: .\npm_state\npm:\n  default_interval: 10m\n", encoding="utf-8")

    def test_invoke_persists_budget(self):
        with mock.patch("devloops_lib.pm_invoke.get_state_path", return_value=self.state_file):
            rc = pm_invoke.main(['/pm-loop 10m slots: 3 delegation: claude', "--state-file", str(self.state_file)])
        self.assertEqual(rc, 0)
        state = load_state(self.state_file)
        self.assertEqual(state["budget"]["slots"], 3)
        self.assertEqual(state["scheduler"]["kind"], "shell")


if __name__ == "__main__":
    unittest.main()
