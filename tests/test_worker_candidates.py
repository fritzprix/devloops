"""Unit tests for the shared worker-candidate filter (issue #66)."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from devloops_lib import worker_candidates
from devloops_lib.tick_gate import find_actionable_backlog, preflight_decision


class WorkerCandidatesTest(unittest.TestCase):
    def test_requires_approved(self):
        reason = worker_candidates.exclusion_reason({"bug"}, [], "me")
        self.assertEqual(reason, "missing approved")

    def test_rejects_rejected_label(self):
        reason = worker_candidates.exclusion_reason(
            {"approved", "rejected"}, [], "me"
        )
        self.assertEqual(reason, "label: rejected")

    def test_rejects_other_excluded_labels(self):
        reason = worker_candidates.exclusion_reason(
            {"approved", "wontfix"}, [], "me"
        )
        self.assertEqual(reason, "label: wontfix")

    def test_rejects_other_assignee(self):
        reason = worker_candidates.exclusion_reason(
            {"approved"}, ["other"], "me"
        )
        self.assertEqual(reason, "assigned to other")

    def test_allows_self_assignee(self):
        self.assertIsNone(
            worker_candidates.exclusion_reason({"approved"}, ["me"], "me")
        )

    def test_rejects_open_blockers(self):
        reason = worker_candidates.exclusion_reason(
            {"approved"}, [], "me", open_blocker_numbers=[12, 3]
        )
        self.assertEqual(reason, "open blocker #3, #12")

    def test_rejects_parent_with_sub_issues(self):
        reason = worker_candidates.exclusion_reason(
            {"approved"}, [], "me", sub_issue_numbers=[2, 5], title="Parent"
        )
        self.assertEqual(reason, "parent of #2, #5")

    def test_rejects_epic_label(self):
        reason = worker_candidates.exclusion_reason(
            {"approved", "epic"}, [], "me", title="Anything"
        )
        self.assertEqual(reason, "label: epic")

    def test_rejects_umbrella_title(self):
        reason = worker_candidates.exclusion_reason(
            {"approved"}, [], "me", title="Epic: park blocked loop cases"
        )
        self.assertEqual(reason, "title names epic/umbrella")

    def test_is_skipped_matches_updated_at(self):
        skipped = {"10": {"updatedAt": "2026-01-01T00:00:00Z", "reason": "x"}}
        self.assertTrue(
            worker_candidates.is_skipped(10, "2026-01-01T00:00:00Z", skipped)
        )
        self.assertFalse(
            worker_candidates.is_skipped(10, "2026-01-02T00:00:00Z", skipped)
        )

    def test_find_actionable_backlog_parity(self):
        def issue(number, labels, assignees=(), updated="2026-01-01T00:00:00Z"):
            return {
                "number": number,
                "updatedAt": updated,
                "labels": {"nodes": [{"name": l} for l in labels]},
                "assignees": {"nodes": [{"login": a} for a in assignees]},
            }

        gate_data = {
            "prs": [
                {
                    "number": 1,
                    "title": "fix: close #101",
                    "bodyText": "Fixes #101",
                    "headRefName": "feat/x",
                }
            ],
            "issues": [
                issue(100, ["approved"]),
                issue(101, ["approved"]),  # closed by PR
                issue(102, ["approved", "rejected"]),
                issue(103, ["approved"], ["someone"]),
                issue(104, ["enhancement"]),
                issue(105, ["approved"], ["me"]),
                issue(106, ["approved"]),  # open blocker
                issue(107, ["approved"], updated="2026-01-01T00:00:00Z"),  # skipped
            ],
        }
        skipped = {"107": {"updatedAt": "2026-01-01T00:00:00Z", "reason": "wait"}}

        def blockers(_forge, number):
            return [9] if number == 106 else []

        def subs(_forge, number):
            return [1, 2] if number == 108 else []

        gate_data["issues"].append(
            issue(108, ["approved"], updated="2026-01-01T00:00:00Z")  # parent
        )
        got = find_actionable_backlog(
            gate_data,
            me="me",
            forge=object(),
            skipped=skipped,
            open_blockers_fn=blockers,
            list_sub_issues_fn=subs,
        )
        self.assertEqual(got, [100, 105])

    def test_preflight_decision_fails_closed_when_forge_unavailable(self):
        # G1.1: get_forge errors must surface, not yield an unchecked backlog.
        with patch("devloops_lib.tick_gate.fetch_gate_data", return_value={"prs": [], "issues": []}), \
             patch("devloops_lib.tick_gate.gh", return_value="me\n"), \
             patch("devloops_lib.forge.detect.get_forge", side_effect=RuntimeError("no forge")):
            with self.assertRaises(RuntimeError):
                preflight_decision(config={}, state={})


if __name__ == "__main__":
    unittest.main()
