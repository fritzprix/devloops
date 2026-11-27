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

from devloops_lib import issue_close, pm_budget, pm_command, pm_induction, pm_invoke, pm_schedule
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
        # Explicit empty config: the repo's own devloops.config.yaml may configure
        # a different peer list; the code default is covered here (issue #71 D4).
        report = pm_schedule.exclusivity_report(pm_state, state_root=self.root, config={})
        self.assertFalse(report["exclusiveOk"])
        self.assertEqual(set(report["peerConflicts"]), set(pm_schedule.PEER_LOOPS))

    def test_exclusivity_ok_without_pm_scheduler(self):
        save_state(self.root / "worker-loop" / "state.json", {"scheduler": {"kind": "process", "id": 99}})
        report = pm_schedule.exclusivity_report({}, state_root=self.root)
        self.assertTrue(report["exclusiveOk"])

    def test_peer_tick_blocked_under_pm_cascade(self):
        self.assertFalse(pm_schedule.peer_tick_allowed({"schedule": {"pm": {"pmCascadeActive": True}}}))

    def test_qa_loop_is_a_peer(self):
        self.assertIn("qa-loop", pm_schedule.PEER_LOOPS)

    def test_peer_loops_come_from_config(self):
        # issue #71 deliverable 4 (epic #102): the peer list is scaffolding policy.
        cfg = {"pm": {"peer_loops": ["worker-loop", "reviewer-loop"]}}
        self.assertEqual(
            pm_schedule.configured_peer_loops(cfg),
            ("worker-loop", "reviewer-loop"),
        )
        # Empty/absent config falls back to the current default list.
        self.assertEqual(pm_schedule.configured_peer_loops({}), pm_schedule.PEER_LOOPS)

    def test_exclusivity_uses_configured_peer_loops(self):
        cfg = {"pm": {"peer_loops": ["custom-loop"]}}
        save_state(self.root / "custom-loop" / "state.json", {"scheduler": {"kind": "cron", "armed": True}})
        save_state(self.root / "worker-loop" / "state.json", {"scheduler": {"kind": "cron", "armed": True}})
        pm_state = {"scheduler": {"kind": "shell", "intervalSeconds": 600}}
        report = pm_schedule.exclusivity_report(pm_state, state_root=self.root, config=cfg)
        self.assertEqual(set(report["peerLoops"]), {"custom-loop"})
        self.assertEqual(report["peerConflicts"], ["custom-loop"])

    def test_exclusivity_flags_use_configured_peer_loops(self):
        cfg = {"pm": {"peer_loops": ["custom-loop"]}}
        pm_schedule.write_exclusivity_flags(self.root, True, config=cfg)
        state = load_state(self.root / "custom-loop" / "state.json")
        self.assertTrue(state["schedule"]["pm"]["pmCascadeActive"])
        self.assertFalse((self.root / "worker-loop" / "state.json").exists())

    def test_exclusivity_flags_arm_qa_schedule(self):
        pm_scheduler = {"scheduler": {"kind": "shell", "intervalSeconds": 600}}
        # Explicit empty config: the repo's devloops.config.yaml may override the
        # peer list; this test covers the code default including qa-loop.
        pm_schedule.write_exclusivity_flags(self.root, True, config={})
        for loop in pm_schedule.PEER_LOOPS:
            state = load_state(self.root / loop / "state.json")
            self.assertTrue(state["schedule"]["pm"]["pmCascadeActive"], loop)
            self.assertFalse(pm_schedule.peer_tick_allowed(state), loop)
        report = pm_schedule.exclusivity_report(pm_scheduler, state_root=self.root, config={})
        self.assertIn("qa-loop", report["peerLoops"])

    def test_exclusivity_reports_armed_qa_schedule_as_conflict(self):
        # G1.3: an armed qa-loop schedule while PM is armed must appear in peerConflicts.
        pm_scheduler = {"scheduler": {"kind": "shell", "intervalSeconds": 600}}
        save_state(self.root / "qa-loop" / "state.json", {"scheduler": {"kind": "process", "id": 7}})
        # config={}: the code default peer list includes qa-loop regardless of
        # the repo's devloops.config.yaml (issue #71 D4).
        report = pm_schedule.exclusivity_report(pm_scheduler, state_root=self.root, config={})
        self.assertIn("qa-loop", report["peerConflicts"])
        self.assertFalse(report["exclusiveOk"])

    def test_cascade_worker_slot_from_pr_work_alone(self):
        plan = pm_schedule.cascade_plan(slots=1, pipeline={"prsNeedingWorker": 2})
        self.assertEqual([p["role"] for p in plan], ["worker-loop"])

    def test_cascade_no_slots_when_all_parked(self):
        plan = pm_schedule.cascade_plan(slots=3, pipeline={})
        self.assertEqual(plan, [])
        plan = pm_schedule.cascade_plan(slots=3, pipeline={"approvedIssues": 0, "prsNeedingWorker": 0,
                                                           "prsNeedingReview": 0, "prsNeedingQa": 0})
        self.assertEqual(plan, [])

    def test_cascade_no_worker_slot_for_blocked_backlog(self):
        plan = pm_schedule.cascade_plan(slots=2, pipeline={"approvedIssues": 0, "prsNeedingQa": 1})
        self.assertEqual([p["role"] for p in plan], ["qa-loop"])

    def test_cascade_qa_slot_after_gatekeeper_and_worker(self):
        plan = pm_schedule.cascade_plan(slots=4, pipeline={
            "prsNeedingReview": 1, "approvedIssues": 1, "prsNeedingQa": 2, "producerDue": 1,
        })
        self.assertEqual([p["role"] for p in plan],
                         ["gatekeeper-loop", "worker-loop", "qa-loop", "producer-loop"])


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


class PmIssueCloseTest(unittest.TestCase):
    def test_software_merged_pr_signal(self):
        dod = {"signals": ["merged_pr"]}
        open_issues = [{"number": 41, "state": "OPEN"}]
        work = [
            {
                "number": 42,
                "merged": True,
                "title": "Add pm-loop",
                "body": "Fixes #41",
                "url": "https://github.com/o/r/pull/42",
            }
        ]
        actions = issue_close.closable_issues(open_issues, work, definition_of_done=dod)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["issueNumber"], 41)
        self.assertEqual(actions[0]["matchedSignals"], ["merged_pr"])
        self.assertIn("definition of done", actions[0]["comment"])

    def test_writing_approved_manuscript_signal(self):
        dod = {"signals": ["approved_manuscript"]}
        issues = [{"number": 7, "state": "OPEN"}]
        work = [
            {
                "title": "Chapter draft",
                "closesIssues": [7],
                "approvedManuscript": True,
                "url": "https://docs.example/ms-7",
            }
        ]
        actions = issue_close.closable_issues(issues, work, definition_of_done=dod)
        self.assertEqual(actions[0]["issueNumber"], 7)
        self.assertEqual(actions[0]["matchedSignals"], ["approved_manuscript"])

    def test_skips_when_issue_already_closed(self):
        issues = [{"number": 41, "state": "CLOSED"}]
        work = [{"merged": True, "body": "Fixes #41"}]
        self.assertEqual(issue_close.closable_issues(issues, work, signals=["merged_pr"]), [])

    def test_resolve_definition_of_done_prefers_config(self):
        cfg = {"pm": {"definition_of_done": {"signals": ["approved_manuscript"]}}}
        profile = {"projectType": "software", "definitionOfDone": ["merged_pr"]}
        dod = issue_close.resolve_definition_of_done(cfg, profile)
        self.assertEqual(dod["signals"], ["approved_manuscript"])
        self.assertEqual(dod["source"], "config")

    def test_partial_pr_refs_with_branch_number_not_closable(self):
        issues = [{"number": 123, "state": "OPEN"}]
        work = [{"merged": True, "title": "Part 1", "body": "Refs #123", "headRefName": "feat/x-123"}]
        self.assertEqual(issue_close.closable_issues(issues, work, signals=["merged_pr"]), [])

    def test_merged_pr_with_fixes_body_is_closable(self):
        issues = [{"number": 123, "state": "OPEN"}]
        work = [{"merged": True, "title": "Complete", "body": "Fixes #123", "headRefName": "feat/x-123"}]
        actions = issue_close.closable_issues(issues, work, signals=["merged_pr"])
        self.assertEqual([a["issueNumber"] for a in actions], [123])

    def test_merged_pr_with_closes_in_title_is_closable(self):
        issues = [{"number": 123, "state": "OPEN"}]
        work = [{"merged": True, "title": "Done (closes #123)", "body": "", "headRefName": "feat/x"}]
        actions = issue_close.closable_issues(issues, work, signals=["merged_pr"])
        self.assertEqual([a["issueNumber"] for a in actions], [123])

    def test_merged_pr_with_only_branch_number_not_closable(self):
        issues = [{"number": 123, "state": "OPEN"}]
        work = [{"merged": True, "title": "Work", "body": "No keyword", "headRefName": "feat/x-123"}]
        self.assertEqual(issue_close.closable_issues(issues, work, signals=["merged_pr"]), [])

    def test_stacked_pr_merged_into_parent_with_fixes_is_closable(self):
        issues = [{"number": 123, "state": "OPEN"}]
        work = [{
            "merged": True,
            "baseRefName": "feat/parent-100",
            "title": "Child PR",
            "body": "Fixes #123",
            "headRefName": "feat/child-123",
        }]
        actions = issue_close.closable_issues(issues, work, signals=["merged_pr"])
        self.assertEqual([a["issueNumber"] for a in actions], [123])


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
