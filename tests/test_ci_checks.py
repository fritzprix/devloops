"""Tests for shared CI check deduplication (issue #116)."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloops_lib import ci_checks, gatekeeper_queue, pr_status  # noqa: E402


def _run(
    name: str,
    *,
    workflow: str = "CI",
    run_id: int = 100,
    started: str = "2026-11-27T10:00:00Z",
    status: str = "COMPLETED",
    conclusion: str = "SUCCESS",
) -> dict:
    return {
        "__typename": "CheckRun",
        "name": name,
        "workflowName": workflow,
        "status": status,
        "conclusion": conclusion,
        "startedAt": started,
        "detailsUrl": f"https://github.com/acme/repo/actions/runs/{run_id}/job/1",
    }


class EffectiveChecksTest(unittest.TestCase):
    def test_cancelled_older_run_superseded_by_later_success(self):
        checks = [
            _run("test", run_id=100, started="2026-11-27T10:00:00Z", conclusion="CANCELLED"),
            _run("test", run_id=101, started="2026-11-27T10:05:00Z", conclusion="SUCCESS"),
        ]
        failing, pending = ci_checks.summarize_checks(checks)
        self.assertEqual(failing, [])
        self.assertEqual(pending, [])

    def test_older_success_superseded_by_in_progress_run(self):
        checks = [
            _run("test", run_id=100, started="2026-11-27T10:00:00Z", conclusion="SUCCESS"),
            _run("test", run_id=101, started="2026-11-27T10:05:00Z", status="IN_PROGRESS", conclusion=""),
        ]
        failing, pending = ci_checks.summarize_checks(checks)
        self.assertEqual(failing, [])
        self.assertEqual(pending, ["test"])

    def test_all_skipped_later_run_is_ignored(self):
        checks = [
            _run("test", run_id=100, started="2026-11-27T10:00:00Z", conclusion="SUCCESS"),
            _run("test", run_id=101, started="2026-11-27T10:05:00Z", conclusion="SKIPPED"),
            _run("lint", run_id=101, started="2026-11-27T10:05:00Z", conclusion="SKIPPED"),
        ]
        eff = ci_checks.effective_checks(checks)
        self.assertEqual(len(eff), 1)
        self.assertEqual(eff[0]["conclusion"], "SUCCESS")

    def test_two_workflows_with_same_job_name_keep_their_own_latest(self):
        checks = [
            _run("test", workflow="CI", run_id=100, started="2026-11-27T10:00:00Z", conclusion="FAILURE"),
            _run("test", workflow="CI", run_id=101, started="2026-11-27T10:05:00Z", conclusion="SUCCESS"),
            _run("test", workflow="Nightly", run_id=200, started="2026-11-27T10:00:00Z", conclusion="SUCCESS"),
            _run("test", workflow="Nightly", run_id=201, started="2026-11-27T10:05:00Z", conclusion="FAILURE"),
        ]
        failing, pending = ci_checks.summarize_checks(checks)
        self.assertEqual(failing, ["test"])
        self.assertEqual(pending, [])

    def test_only_cancelled_run_counts_as_failure(self):
        checks = [
            _run("test", run_id=100, started="2026-11-27T10:00:00Z", conclusion="CANCELLED"),
        ]
        failing, pending = ci_checks.summarize_checks(checks)
        self.assertEqual(failing, ["test"])
        self.assertEqual(pending, [])

    def test_worker_and_gatekeeper_return_same_failing_and_pending(self):
        checks = [
            _run("test", workflow="CI", run_id=100, started="2026-11-27T10:00:00Z", conclusion="CANCELLED"),
            _run("test", workflow="CI", run_id=101, started="2026-11-27T10:05:00Z", conclusion="SUCCESS"),
            _run("lint", workflow="Lint", run_id=200, started="2026-11-27T10:00:00Z", status="IN_PROGRESS", conclusion=""),
            {"__typename": "StatusContext", "context": "deploy/check", "state": "FAILURE", "createdAt": "2026-11-27T10:00:00Z"},
            {"__typename": "StatusContext", "context": "deploy/check", "state": "SUCCESS", "createdAt": "2026-11-27T10:05:00Z"},
        ]
        w_fail, w_pend = ci_checks.summarize_checks(checks)
        gk_eff = gatekeeper_queue.effective_checks(checks)
        gk_fail = [c.get("name") or c.get("context") for c in gk_eff if gatekeeper_queue.check_state(c) in gatekeeper_queue.FAILED]
        gk_pend = [c.get("name") or c.get("context") for c in gk_eff if gatekeeper_queue.check_state(c) in gatekeeper_queue.PENDING]
        self.assertEqual((w_fail, w_pend), (gk_fail, gk_pend))
        self.assertEqual(w_fail, [])
        self.assertEqual(w_pend, ["lint"])

    def test_scenario_superseded_cancelled_run_is_not_fix_ci(self):
        pr_payload = {
            "number": 77,
            "title": "feat: example",
            "body": "Summary",
            "url": "https://example.invalid/pr/77",
            "headRefName": "feat/example-77",
            "headRefOid": "sha1",
            "baseRefName": "main",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "reviewDecision": "",
            "statusCheckRollup": [
                _run("test", run_id=100, started="2026-11-27T10:00:00Z", conclusion="CANCELLED"),
                _run("test", run_id=101, started="2026-11-27T10:05:00Z", conclusion="SUCCESS"),
            ],
            "commits": [{"committedDate": "2026-11-27T09:55:00Z"}],
            "comments": [],
            "labels": [],
        }

        def fake_gh(*args, **kwargs):
            if args and args[0] == "api":
                return json.dumps({"data": {}})
            return json.dumps(pr_payload)

        with mock.patch.object(pr_status, "gh", side_effect=fake_gh), \
             mock.patch.object(pr_status, "duplicate_commits", return_value=[]), \
             mock.patch.object(pr_status, "parent_pr_merged", return_value=None):
            summary = pr_status.summarize(77, "owner", "repo", "me", default_base="main", merged_prs=[])

        self.assertEqual(summary["failingChecks"], [])
        self.assertNotEqual(summary["nextAction"], "fix-ci")
        self.assertEqual(summary["nextAction"], "none")

    def test_newer_queued_run_without_start_time_supersedes_older_success(self):
        # G1.1: queued runs have 0001-01-01, empty, or missing startedAt.
        for started_val in ("0001-01-01T00:00:00Z", "", None):
            with self.subTest(startedAt=started_val):
                queued = _run("test", run_id=101, status="QUEUED", conclusion="")
                if started_val is None:
                    queued.pop("startedAt", None)
                else:
                    queued["startedAt"] = started_val
                checks = [
                    _run("test", run_id=100, started="2026-11-27T10:00:00Z", conclusion="SUCCESS"),
                    queued,
                ]
                w_fail, w_pend = ci_checks.summarize_checks(checks)
                gk_eff = gatekeeper_queue.effective_checks(checks)
                gk_fail = [c.get("name") for c in gk_eff if gatekeeper_queue.check_state(c) in gatekeeper_queue.FAILED]
                gk_pend = [c.get("name") for c in gk_eff if gatekeeper_queue.check_state(c) in gatekeeper_queue.PENDING]
                self.assertEqual(w_fail, [])
                self.assertEqual(w_pend, ["test"])
                self.assertEqual((gk_fail, gk_pend), ([], ["test"]))

    def test_same_name_jobs_within_one_run_keep_failing_and_pending(self):
        # G2.1: separate jobs sharing a name in one run (e.g. matrix legs) must not be deduplicated.
        checks_fail = [
            _run("test", run_id=100, started="2026-11-27T10:00:00Z", conclusion="FAILURE"),
            _run("test", run_id=100, started="2026-11-27T10:05:00Z", conclusion="SUCCESS"),
        ]
        failing, pending = ci_checks.summarize_checks(checks_fail)
        self.assertEqual(failing, ["test"])
        self.assertEqual(pending, [])

        queued = _run("test", run_id=100, status="QUEUED", conclusion="")
        queued["startedAt"] = "0001-01-01T00:00:00Z"
        checks_pending = [
            _run("test", run_id=100, started="2026-11-27T10:00:00Z", conclusion="SUCCESS"),
            queued,
        ]
        failing2, pending2 = ci_checks.summarize_checks(checks_pending)
        self.assertEqual(failing2, [])
        self.assertEqual(pending2, ["test"])


if __name__ == "__main__":
    unittest.main()
