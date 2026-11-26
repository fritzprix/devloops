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

    def test_rejects_parent_when_sub_issues_all_closed(self):
        # Criterion 5: closed children still mark the issue as a parent.
        reason = worker_candidates.exclusion_reason(
            {"approved"}, [], "me", sub_issue_numbers=[7, 8], title="Done parent"
        )
        self.assertEqual(reason, "parent of #7, #8")

    def test_rejects_epic_label(self):
        reason = worker_candidates.exclusion_reason(
            {"approved", "epic"}, [], "me", title="Anything"
        )
        self.assertEqual(reason, "label: epic")

    def test_umbrella_title_needs_policy(self):
        # No built-in title guess (issue #95 owner rescope): policy-only.
        reason = worker_candidates.exclusion_reason(
            {"approved"}, [], "me", title="Epic: park blocked loop cases"
        )
        self.assertIsNone(reason)
        reason = worker_candidates.exclusion_reason(
            {"approved"}, [], "me", title="Epic: park blocked loop cases",
            policy={"epic_title_patterns": [r"\bepic\b"]},
        )
        self.assertEqual(reason, "title matches epic pattern '\\\\bepic\\\\b'")

    def test_policy_require_approved_false_admits_unapproved(self):
        reason = worker_candidates.exclusion_reason(
            {"bug"}, [], "me", title="Fix x",
            policy={"require_approved": False},
        )
        self.assertIsNone(reason)

    def test_policy_custom_epic_label_and_excluded_labels(self):
        reason = worker_candidates.exclusion_reason(
            {"approved", "umbrella"}, [], "me", title="Anything",
            policy={"epic_label": "umbrella"},
        )
        self.assertEqual(reason, "label: umbrella")
        reason = worker_candidates.exclusion_reason(
            {"approved", "badlabel"}, [], "me", title="Anything",
            policy={"excluded_labels": ["badlabel"]},
        )
        self.assertEqual(reason, "label: badlabel")

    def test_is_skipped_matches_updated_at(self):
        skipped = {"10": {"updatedAt": "2026-01-01T00:00:00Z", "reason": "x"}}
        self.assertTrue(
            worker_candidates.is_skipped(10, "2026-01-01T00:00:00Z", skipped)
        )
        self.assertFalse(
            worker_candidates.is_skipped(10, "2026-01-02T00:00:00Z", skipped)
        )

    def test_find_actionable_backlog_parity(self):
        def issue(number, labels, assignees=(), updated="2026-01-01T00:00:00Z", title=""):
            return {
                "number": number,
                "title": title,
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

    def test_gate_shaped_title_only_epic_excluded(self):
        # G1.1: gate GraphQL nodes carry title; title-only epics must not RUN
        # when the scaffolding policy lists title patterns (issue #95 rescope).
        issue = {
            "number": 74,
            "title": "Epic: park blocked loop cases",
            "updatedAt": "2026-01-01T00:00:00Z",
            "labels": {"nodes": [{"name": "approved"}]},
            "assignees": {"nodes": []},
        }
        got = find_actionable_backlog(
            {"prs": [], "issues": [issue]},
            me="me",
            forge=object(),
            policy={"epic_title_patterns": [r"\bepic\b"]},
            open_blockers_fn=lambda *_a, **_k: [],
            list_sub_issues_fn=lambda *_a, **_k: [],
        )
        self.assertEqual(got, [])
        # Without the policy, the title alone does not exclude (no built-in guess).
        got = find_actionable_backlog(
            {"prs": [], "issues": [issue]},
            me="me",
            forge=object(),
            open_blockers_fn=lambda *_a, **_k: [],
            list_sub_issues_fn=lambda *_a, **_k: [],
        )
        self.assertEqual(got, [74])

    def test_noop_when_only_parent_or_epic_remain(self):
        # G1.2 / criterion 4: backlog empty → gate would NOOP.
        def issue(number, labels, title="", sub=None):
            return {
                "number": number,
                "title": title,
                "updatedAt": "2026-01-01T00:00:00Z",
                "labels": {"nodes": [{"name": l} for l in labels]},
                "assignees": {"nodes": []},
                "_subs": sub or [],
            }

        issues = [
            issue(1, ["approved", "epic"], title="Big theme"),
            issue(2, ["approved"], title="Umbrella: rollout"),
            issue(3, ["approved"], title="Parent tracker", sub=[10, 11]),
        ]

        def subs(_forge, number):
            for i in issues:
                if i["number"] == number:
                    return list(i["_subs"])
            return []

        got = find_actionable_backlog(
            {"prs": [], "issues": issues},
            me="me",
            forge=object(),
            policy={"epic_title_patterns": [r"\bumbrella\b", r"\btracking issue\b", r"\bepic\b"]},
            open_blockers_fn=lambda *_a, **_k: [],
            list_sub_issues_fn=subs,
        )
        self.assertEqual(got, [])

    def test_parent_with_all_closed_sub_issues_excluded(self):
        # G1.2 / criterion 5: list_sub_issues returns numbers regardless of state.
        issue = {
            "number": 50,
            "title": "Parent with closed children",
            "updatedAt": "2026-01-01T00:00:00Z",
            "labels": {"nodes": [{"name": "approved"}]},
            "assignees": {"nodes": []},
        }
        got = find_actionable_backlog(
            {"prs": [], "issues": [issue]},
            me="me",
            forge=object(),
            open_blockers_fn=lambda *_a, **_k: [],
            list_sub_issues_fn=lambda *_a, **_k: [51, 52],  # closed children
        )
        self.assertEqual(got, [])

    def test_preflight_decision_fails_closed_when_forge_unavailable(self):
        # get_forge errors must surface, not yield an unchecked backlog.
        with patch("devloops_lib.tick_gate.fetch_gate_data", return_value={"prs": [], "issues": []}), \
             patch("devloops_lib.tick_gate.gh", return_value="me\n"), \
             patch("devloops_lib.forge.detect.get_forge", side_effect=RuntimeError("no forge")):
            with self.assertRaises(RuntimeError):
                preflight_decision(config={}, state={})


if __name__ == "__main__":
    unittest.main()
