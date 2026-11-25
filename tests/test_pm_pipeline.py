#!/usr/bin/env python3
"""Tests for devloops_lib.pm_pipeline counter computation (issue #71)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloops_lib import pm_pipeline
from devloops_lib.pr_status import WORKER_PR_ACTIONS


def candidate(number: int, dependency_status: str = "none") -> dict:
    return {"number": number, "dependency": {"status": dependency_status}}


def candidates(*entries: dict, reached: bool = False) -> dict:
    return {
        "candidates": list(entries),
        "openPRLimit": {"unapproved": 3, "limit": 5, "reached": reached},
    }


class ComputeCountersTest(unittest.TestCase):
    def test_parked_prs_are_counted_nowhere(self):
        counters = pm_pipeline.compute_counters(
            worker_prs=[
                {"number": 1, "nextAction": "wait-ci"},
                {"number": 2, "nextAction": "none"},
            ],
            gatekeeper_entries=[
                {"number": 1, "status": "escalate"},
                {"number": 2, "status": "needs-human"},
                {"number": 3, "status": "wait-worker"},
                {"number": 4, "status": "approved"},
                {"number": 5, "status": "await-ci"},
            ],
            qa_entries=[
                {"number": 1, "status": "needs-human"},
                {"number": 2, "status": "wait-worker"},
                {"number": 3, "status": "accepted"},
            ],
            candidates=candidates(),
        )
        self.assertEqual(counters["prsNeedingWorker"], 0)
        self.assertEqual(counters["prsNeedingReview"], 0)
        self.assertEqual(counters["prsNeedingQa"], 0)
        self.assertEqual(counters["approvedIssues"], 0)
        self.assertFalse(counters["boardFull"])

    def test_review_statuses_and_worker_actions_counted(self):
        counters = pm_pipeline.compute_counters(
            worker_prs=[{"number": n, "nextAction": action} for n, action in enumerate(WORKER_PR_ACTIONS)],
            gatekeeper_entries=[{"number": 1, "status": "review"}, {"number": 2, "status": "review"}],
            qa_entries=[{"number": 1, "status": "review"}],
            candidates=candidates(candidate(10), candidate(11)),
        )
        self.assertEqual(counters["prsNeedingWorker"], len(WORKER_PR_ACTIONS))
        self.assertEqual(counters["prsNeedingReview"], 2)
        self.assertEqual(counters["prsNeedingQa"], 1)
        self.assertEqual(counters["approvedIssues"], 2)

    def test_blocked_approved_issue_is_not_an_approved_issue(self):
        counters = pm_pipeline.compute_counters(
            candidates=candidates(
                candidate(10, "wait"),
                candidate(11, "blocked-on-closure"),
                candidate(12, "stack"),
                candidate(13, "ready"),
            ),
        )
        self.assertEqual(counters["approvedIssues"], 2)

    def test_board_full_flag(self):
        counters = pm_pipeline.compute_counters(candidates=candidates(reached=True))
        self.assertTrue(counters["boardFull"])

    def test_empty_inputs_yield_zero_counters(self):
        self.assertEqual(
            pm_pipeline.compute_counters(),
            {
                "approvedIssues": 0,
                "prsNeedingWorker": 0,
                "prsNeedingReview": 0,
                "prsNeedingQa": 0,
                "boardFull": False,
            },
        )


if __name__ == "__main__":
    unittest.main()
