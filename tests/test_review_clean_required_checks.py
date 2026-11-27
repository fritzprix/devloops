#!/usr/bin/env python3
"""Tests for issue #101: gatekeeper:review-clean state and required_checks for approve."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from devloops_lib import config as cfg_mod
from devloops_lib import contract
from devloops_lib import gatekeeper_queue as gq
from devloops_lib.forge import cli as forge_cli
from devloops_lib.forge.gitea import GiteaForge
from devloops_lib.forge.github import GitHubForge
from devloops_lib.forge.models import CI_FAILURE, CI_PENDING, CI_SUCCESS, PullRequest


class _FakeForge:
    """In-memory Forge double for testing set_verdict_labels and head-change cleanup."""

    def __init__(self, *, existing_labels: set[str] | None = None, pr_labels: set[str] | None = None):
        self.repo_labels: set[str] = set(existing_labels or ())
        self.pr_labels: set[str] = set(pr_labels or ())
        self.ensure_calls: list[str] = []

    def ensure_label(self, name: str, *, color: str = "ededed", description: str = "") -> None:
        self.ensure_calls.append(name)
        self.repo_labels.add(name)

    def add_labels(self, number: int, names):
        for n in names:
            if n not in self.repo_labels:
                self.repo_labels.add(n)
            self.pr_labels.add(n)

    def remove_labels(self, number: int, names):
        for n in names:
            self.pr_labels.discard(n)


class ReviewCleanLabelContractTests(unittest.TestCase):
    def test_review_clean_is_not_a_verdict_label(self):
        self.assertEqual(contract.GATEKEEPER_REVIEW_CLEAN, "gatekeeper:review-clean")
        self.assertNotIn(contract.GATEKEEPER_REVIEW_CLEAN, contract.GATEKEEPER_VERDICT_LABELS)
        self.assertNotIn(contract.GATEKEEPER_REVIEW_CLEAN, contract.VERDICT_LABELS)

    def test_clean_verdict_ensures_and_sets_review_clean_and_removes_changes_requested(self):
        f = _FakeForge(pr_labels={contract.GATEKEEPER_CHANGES_REQUESTED})
        res = forge_cli.set_verdict_labels(f, 12, "clean")
        self.assertIn(contract.GATEKEEPER_REVIEW_CLEAN, f.ensure_calls)
        self.assertIn(contract.GATEKEEPER_REVIEW_CLEAN, f.pr_labels)
        self.assertNotIn(contract.GATEKEEPER_CHANGES_REQUESTED, f.pr_labels)
        self.assertEqual(res["added"], [contract.GATEKEEPER_REVIEW_CLEAN])
        # Idempotent second call
        forge_cli.set_verdict_labels(f, 12, "clean")
        self.assertIn(contract.GATEKEEPER_REVIEW_CLEAN, f.pr_labels)

    def test_changes_requested_and_escalated_remove_review_clean(self):
        for verdict, expected in (
            ("changes-requested", contract.GATEKEEPER_CHANGES_REQUESTED),
            ("escalated", contract.GATEKEEPER_ESCALATED),
        ):
            f = _FakeForge(pr_labels={contract.GATEKEEPER_REVIEW_CLEAN})
            res = forge_cli.set_verdict_labels(f, 12, verdict)
            self.assertIn(expected, f.pr_labels)
            self.assertNotIn(contract.GATEKEEPER_REVIEW_CLEAN, f.pr_labels)
            self.assertIn(contract.GATEKEEPER_REVIEW_CLEAN, res["removed"])

    def test_approve_keeps_review_clean(self):
        f = _FakeForge(pr_labels={contract.GATEKEEPER_REVIEW_CLEAN})
        res = forge_cli.set_verdict_labels(f, 12, "approved")
        self.assertIn(contract.GATEKEEPER_APPROVED, f.pr_labels)
        self.assertIn(contract.GATEKEEPER_REVIEW_CLEAN, f.pr_labels)
        self.assertNotIn(contract.GATEKEEPER_REVIEW_CLEAN, res["removed"])

    def test_head_change_removes_review_clean_before_review(self):
        f = _FakeForge(pr_labels={contract.GATEKEEPER_REVIEW_CLEAN})
        changed = gq.clear_review_clean_on_head_change(
            f, 12, list(f.pr_labels), reviewed_head="sha1", head="sha2"
        )
        self.assertTrue(changed)
        self.assertNotIn(contract.GATEKEEPER_REVIEW_CLEAN, f.pr_labels)
        # Same head is a no-op
        f2 = _FakeForge(pr_labels={contract.GATEKEEPER_REVIEW_CLEAN})
        self.assertFalse(
            gq.clear_review_clean_on_head_change(
                f2, 12, list(f2.pr_labels), reviewed_head="sha1", head="sha1"
            )
        )
        self.assertIn(contract.GATEKEEPER_REVIEW_CLEAN, f2.pr_labels)


class GiteaEnsureLabelTests(unittest.TestCase):
    def test_gitea_ensure_label_creates_when_missing_and_skips_when_present(self):
        g = GiteaForge(base_url="https://gitea.example", owner="o", name="r")
        calls = []

        def fake_http(method, path, **kwargs):
            calls.append((method, path, kwargs.get("body")))
            if method == "GET" and path.endswith("/labels"):
                if any(m == "POST" for m, _, _ in calls):
                    return 200, [{"id": 5, "name": contract.GATEKEEPER_REVIEW_CLEAN}]
                return 200, []
            if method == "POST" and path.endswith("/labels"):
                return 201, {"id": 5, "name": contract.GATEKEEPER_REVIEW_CLEAN}
            return 200, []

        with mock.patch.object(g, "_http_call", side_effect=fake_http):
            g.ensure_label(contract.GATEKEEPER_REVIEW_CLEAN, color="0e8a16", description="clean")
            g.ensure_label(contract.GATEKEEPER_REVIEW_CLEAN, color="0e8a16", description="clean")

        posts = [c for c in calls if c[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2]["name"], contract.GATEKEEPER_REVIEW_CLEAN)


class RequiredChecksTests(unittest.TestCase):
    def _pr(self, checks):
        return PullRequest(
            number=1, title="t", url="u", head_ref="b", head_sha="sha1", base_ref="main",
            is_draft=False, author="me", raw={"statusCheckRollup": checks},
        )

    def test_github_ci_status_missing_required_check_is_pending(self):
        gh_forge = GitHubForge()
        pr = self._pr([{"name": "ubuntu", "status": "COMPLETED", "conclusion": "SUCCESS"}])
        # Without required_checks -> success
        self.assertEqual(gh_forge.ci_status(pr).state, CI_SUCCESS)
        # With missing required check -> pending
        res = gh_forge.ci_status(pr, required_checks=["ubuntu", "macos"])
        self.assertEqual(res.state, CI_PENDING)
        self.assertIn("macos", res.pending)
        # With all required checks passing -> success
        pr_all = self._pr([
            {"name": "ubuntu", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "macos", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ])
        self.assertEqual(gh_forge.ci_status(pr_all, required_checks=["ubuntu", "macos"]).state, CI_SUCCESS)
        # With required check failing -> failure
        pr_fail = self._pr([
            {"name": "ubuntu", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "macos", "status": "COMPLETED", "conclusion": "FAILURE"},
        ])
        self.assertEqual(gh_forge.ci_status(pr_fail, required_checks=["ubuntu", "macos"]).state, CI_FAILURE)

    def test_gitea_ci_status_missing_required_check_is_pending(self):
        g = GiteaForge(base_url="https://gitea.example", owner="o", name="r")
        pr = PullRequest(
            number=1, title="t", url="u", head_ref="b", head_sha="sha1", base_ref="main",
            is_draft=False, author="me",
        )
        with mock.patch.object(
            g, "_http_call",
            return_value=(200, {"statuses": [{"context": "ubuntu", "status": "success"}]}),
        ):
            self.assertEqual(g.ci_status(pr).state, CI_SUCCESS)
            res = g.ci_status(pr, required_checks=["ubuntu", "macos"])
            self.assertEqual(res.state, CI_PENDING)
            self.assertIn("macos", res.pending)

    def test_classify_waits_when_required_check_missing(self):
        status, reason, _ = gq.classify(
            mergeable="MERGEABLE",
            head="sha1",
            base="main",
            last_verdict=None,
            worker_response=None,
            total_verdicts=0,
            state_reviewed_head=None,
            state_pending_ci=False,
            has_open_findings=False,
            pending_checks=[],
            failing_checks=[],
            clean_head="sha1",
            total_checks=1,
            missing_required_checks=["macos"],
        )
        self.assertEqual(status, "await-ci")
        self.assertIn("macos", reason)

    def test_config_validation_rejects_invalid_required_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "devloops.config.json"
            p.write_text(json.dumps({"gatekeeper": {"required_checks": "macos"}}), encoding="utf-8")
            with self.assertRaises(cfg_mod.ConfigError):
                cfg_mod.load_config(tmp)
            p.write_text(json.dumps({"gatekeeper": {"required_checks": ["ubuntu", "macos"]}}), encoding="utf-8")
            loaded = cfg_mod.load_config(tmp)
            self.assertEqual(loaded["gatekeeper"]["required_checks"], ["ubuntu", "macos"])


if __name__ == "__main__":
    unittest.main()
