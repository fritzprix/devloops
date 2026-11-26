#!/usr/bin/env python3
"""Tests for devloops_lib.pm_pipeline counter computation (issue #71)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloops_lib import contract, pm_pipeline
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

    def test_label_parked_escalated_pr_excluded_despite_actionable_classifiers(self):
        # G1.1: escalate + needs-owner labels park the PR even when qa says review
        # and worker nextAction is fix-ci (failing CI on an escalated PR).
        labels = [contract.GATEKEEPER_ESCALATED, contract.NEEDS_OWNER]
        counters = pm_pipeline.compute_counters(
            worker_prs=[{"number": 9, "nextAction": "fix-ci", "labels": labels}],
            gatekeeper_entries=[{"number": 9, "status": "escalate", "labels": labels}],
            qa_entries=[{"number": 9, "status": "review", "labels": labels}],
            candidates=candidates(),
        )
        self.assertEqual(counters["prsNeedingWorker"], 0)
        self.assertEqual(counters["prsNeedingReview"], 0)
        self.assertEqual(counters["prsNeedingQa"], 0)

    def test_address_gatekeeper_counts_when_not_parked(self):
        counters = pm_pipeline.compute_counters(
            worker_prs=[{"number": 3, "nextAction": "address-gatekeeper", "labels": []}],
            gatekeeper_entries=[{"number": 3, "status": "review", "labels": []}],
            qa_entries=[{"number": 3, "status": "accepted", "labels": []}],
            candidates=candidates(),
        )
        self.assertEqual(counters["prsNeedingWorker"], 1)
        self.assertEqual(counters["prsNeedingReview"], 1)
        self.assertEqual(counters["prsNeedingQa"], 0)

    def test_open_blocker_parks_except_rebase_action(self):
        parked = pm_pipeline.compute_counters(
            worker_prs=[{"number": 4, "nextAction": "fix-ci", "openBlockers": [66]}],
            gatekeeper_entries=[{"number": 4, "status": "review", "openBlockers": [66]}],
            qa_entries=[{"number": 4, "status": "review", "openBlockers": [66]}],
        )
        self.assertEqual(parked["prsNeedingWorker"], 0)
        self.assertEqual(parked["prsNeedingReview"], 0)
        self.assertEqual(parked["prsNeedingQa"], 0)
        # #68 exception: rebase still needs a worker slot.
        rebase = pm_pipeline.compute_counters(
            worker_prs=[{"number": 4, "nextAction": "rebase", "openBlockers": [66],
                         "labels": [contract.NEEDS_OWNER]}],
        )
        # #68: rebase is in WORKER_PR_ACTIONS and is not parked, so it counts.
        self.assertEqual(rebase["prsNeedingWorker"], 1)
        self.assertFalse(pm_pipeline.pr_is_parked(
            {"number": 4, "nextAction": "rebase", "openBlockers": [66]}
        ))

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


class FetchPipelineTest(unittest.TestCase):
    def test_fetch_pipeline_feeds_classifiers_into_counters(self):
        worker = [{"number": 1, "nextAction": "address-gatekeeper", "labels": []}]
        gk = [{"number": 1, "status": "review", "labels": []}]
        qa = [{"number": 1, "status": "accepted", "labels": []}]
        cands = candidates(candidate(12, "ready"))

        with patch.object(pm_pipeline, "summarize_prs", return_value=worker), \
             patch.object(pm_pipeline, "_open_pr_numbers", side_effect=[[1], [1]]), \
             patch.object(pm_pipeline, "load_state", return_value={}), \
             patch.object(pm_pipeline, "get_default_state_file", return_value=Path(".")), \
             patch.object(pm_pipeline, "assess_readiness", return_value={}), \
             patch.object(pm_pipeline, "gatekeeper_summarize", return_value=gk[0]), \
             patch.object(pm_pipeline.qa_lib, "classify_pr", return_value=qa[0]), \
             patch.object(pm_pipeline, "fetch_candidates", return_value=cands), \
             patch.object(pm_pipeline, "gh", side_effect=["me", "{}"]):
            counters = pm_pipeline.fetch_pipeline(config={})
        self.assertEqual(counters["prsNeedingWorker"], 1)
        self.assertEqual(counters["prsNeedingReview"], 1)
        self.assertEqual(counters["prsNeedingQa"], 0)
        self.assertEqual(counters["approvedIssues"], 1)


if __name__ == "__main__":
    unittest.main()
