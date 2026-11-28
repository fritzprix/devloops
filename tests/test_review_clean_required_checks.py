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
from devloops_lib.forge import ForgeError
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


class GitHubEnsureLabelTests(unittest.TestCase):
    def test_github_ensure_label_creates_review_clean(self):
        gh_forge = GitHubForge()
        calls = []
        with mock.patch.object(gh_forge, "_call", side_effect=lambda *a: calls.append(a) or ""):
            gh_forge.ensure_label(
                contract.GATEKEEPER_REVIEW_CLEAN, color="0e8a16", description="clean"
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0],
            ("label", "create", contract.GATEKEEPER_REVIEW_CLEAN, "--color", "0e8a16", "--force", "--description", "clean"),
        )


class GitHubRemoveLabelsRepoFilterTests(unittest.TestCase):
    """G5.1: removing a label the repository does not have must not fail on GitHub."""

    class _RepoLabelsGh:
        """gh double that rejects removal of labels the repository does not have."""

        def __init__(self, repo_labels):
            self.repo_labels = set(repo_labels)
            self.calls: list[list[str]] = []

        def run(self, args):
            self.calls.append(list(args))
            if args[:2] == ["label", "list"]:
                return json.dumps([{"name": n} for n in sorted(self.repo_labels)])
            if args[:2] == ["label", "create"]:
                self.repo_labels.add(args[2])
                return ""
            if args[:2] == ["issue", "edit"]:
                for name in self._opts(args, "--remove-label"):
                    if name not in self.repo_labels:
                        raise ForgeError(f"label '{name}' does not exist in repository")
                return json.dumps({"url": "https://github.com/acme/repo/issues/12"})
            raise ForgeError(f"unexpected gh args: {args}")

        @staticmethod
        def _opts(args, flag):
            return [args[i + 1] for i, a in enumerate(args) if a == flag and i + 1 < len(args)]

    def _forge(self, repo_labels):
        self.gh = self._RepoLabelsGh(repo_labels)
        return GitHubForge(run=self.gh.run)

    def test_double_rejects_unknown_label_removal(self):
        forge = self._forge(["gatekeeper:changes-requested"])
        with self.assertRaises(ForgeError):
            forge._call("issue", "edit", "12", "--remove-label", contract.GATEKEEPER_REVIEW_CLEAN)

    def test_remove_labels_skips_label_missing_from_repo(self):
        forge = self._forge(["gatekeeper:changes-requested"])
        forge.remove_labels(12, [contract.GATEKEEPER_REVIEW_CLEAN])
        self.assertEqual([c for c in self.gh.calls if "--remove-label" in c], [])

    def test_remove_labels_keeps_known_labels(self):
        forge = self._forge(["gatekeeper:changes-requested"])
        forge.remove_labels(12, [contract.GATEKEEPER_CHANGES_REQUESTED, contract.GATEKEEPER_REVIEW_CLEAN])
        remove_cmds = [c for c in self.gh.calls if "--remove-label" in c]
        self.assertEqual(len(remove_cmds), 1)
        self.assertIn(contract.GATEKEEPER_CHANGES_REQUESTED, remove_cmds[0])
        self.assertNotIn(contract.GATEKEEPER_REVIEW_CLEAN, remove_cmds[0])

    def test_set_verdict_escalated_survives_repo_without_review_clean(self):
        forge = self._forge(list(contract.VERDICT_LABELS))
        res = forge_cli.set_verdict_labels(forge, 12, "escalated")
        self.assertIn(contract.GATEKEEPER_REVIEW_CLEAN, res["removed"])
        remove_cmds = [c for c in self.gh.calls if "--remove-label" in c]
        self.assertEqual(len(remove_cmds), 1)
        self.assertNotIn(contract.GATEKEEPER_REVIEW_CLEAN, remove_cmds[0])


class ForgeCliChecksRequiredTests(unittest.TestCase):
    def test_forge_cli_pr_checks_respects_required_checks(self):
        # G1.1: `forge.py pr checks` passes gatekeeper.required_checks into ci_status.
        import io
        from contextlib import redirect_stdout

        gh_forge = GitHubForge()
        pr = PullRequest(
            number=12, title="t", url="u", head_ref="b", head_sha="sha1", base_ref="main",
            is_draft=False, author="me",
            raw={"statusCheckRollup": [{"name": "ubuntu", "status": "COMPLETED", "conclusion": "SUCCESS"}]},
        )
        buf = io.StringIO()
        with mock.patch.object(gh_forge, "get_pr", return_value=pr), \
             mock.patch("devloops_lib.config.load_config", return_value={"gatekeeper": {"required_checks": ["ubuntu", "macos"]}}), \
             redirect_stdout(buf):
            rc = forge_cli.forge_main(["pr", "checks", "12"], forge=gh_forge)
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["state"], CI_PENDING)
        self.assertIn("macos", payload["pending"])


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

    def test_github_ci_status_skipped_required_check_stays_pending(self):
        gh_forge = GitHubForge()
        # Skipped required check -> pending (not success)
        pr_skipped = self._pr([
            {"name": "ubuntu", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "macos", "status": "COMPLETED", "conclusion": "SKIPPED"},
        ])
        res = gh_forge.ci_status(pr_skipped, required_checks=["ubuntu", "macos"])
        self.assertEqual(res.state, CI_PENDING)
        self.assertIn("macos", res.pending)
        # Older SKIPPED run followed by newer SUCCESS run -> passes (G3.1)
        pr_rerun_ok = self._pr([
            {"name": "ubuntu", "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": "2026-11-27T01:00:00Z"},
            {"name": "macos", "status": "COMPLETED", "conclusion": "SKIPPED", "completedAt": "2026-11-27T01:00:00Z"},
            {"name": "macos", "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": "2026-11-27T01:05:00Z"},
        ])
        res_ok = gh_forge.ci_status(pr_rerun_ok, required_checks=["ubuntu", "macos"])
        self.assertEqual(res_ok.state, CI_SUCCESS)
        # Older SUCCESS run followed by newer SKIPPED run -> still pending
        pr_latest_skipped = self._pr([
            {"name": "ubuntu", "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": "2026-11-27T01:00:00Z"},
            {"name": "macos", "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": "2026-11-27T01:00:00Z"},
            {"name": "macos", "status": "COMPLETED", "conclusion": "SKIPPED", "completedAt": "2026-11-27T01:05:00Z"},
        ])
        res_mixed = gh_forge.ci_status(pr_latest_skipped, required_checks=["ubuntu", "macos"])
        self.assertEqual(res_mixed.state, CI_PENDING)
        self.assertIn("macos", res_mixed.pending)
        # Failing latest required check stays failure (not downgraded to pending)
        pr_fail = self._pr([
            {"name": "ubuntu", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "macos", "status": "COMPLETED", "conclusion": "SKIPPED"},
            {"name": "macos", "status": "COMPLETED", "conclusion": "FAILURE"},
        ])
        res_fail = gh_forge.ci_status(pr_fail, required_checks=["ubuntu", "macos"])
        self.assertEqual(res_fail.state, CI_FAILURE)
        self.assertIn("macos", res_fail.failing)

    def test_gitea_ci_status_non_success_required_check_stays_pending(self):
        g = GiteaForge(base_url="https://gitea.example", owner="o", name="r")
        pr = PullRequest(
            number=1, title="t", url="u", head_ref="b", head_sha="sha1", base_ref="main",
            is_draft=False, author="me",
        )
        with mock.patch.object(
            g, "_http_call",
            return_value=(200, {"statuses": [
                {"context": "ubuntu", "status": "success", "updated_at": "2026-11-27T01:00:00Z"},
                {"context": "macos", "status": "warning", "updated_at": "2026-11-27T01:00:00Z"},
            ]}),
        ):
            res = g.ci_status(pr, required_checks=["ubuntu", "macos"])
            self.assertEqual(res.state, CI_PENDING)
            self.assertIn("macos", res.pending)
        # Older non-success run followed by newer success run -> passes (G3.1)
        with mock.patch.object(
            g, "_http_call",
            return_value=(200, {"statuses": [
                {"context": "ubuntu", "status": "success", "updated_at": "2026-11-27T01:00:00Z"},
                {"context": "macos", "status": "warning", "updated_at": "2026-11-27T01:00:00Z"},
                {"context": "macos", "status": "success", "updated_at": "2026-11-27T01:05:00Z"},
            ]}),
        ):
            res_ok = g.ci_status(pr, required_checks=["ubuntu", "macos"])
            self.assertEqual(res_ok.state, CI_SUCCESS)

    def test_missing_required_checks_uses_latest_run(self):
        cfg = {"gatekeeper": {"required_checks": ["ubuntu", "macos"]}}
        checks_skipped = [
            {"name": "ubuntu", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "macos", "status": "COMPLETED", "conclusion": "SKIPPED"},
        ]
        self.assertEqual(gq.missing_required_checks(checks_skipped, cfg), ["macos"])
        # Older SKIPPED followed by newer SUCCESS -> not missing (G3.1)
        checks_rerun_ok = [
            {"name": "ubuntu", "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": "2026-11-27T01:00:00Z"},
            {"name": "macos", "status": "COMPLETED", "conclusion": "SKIPPED", "completedAt": "2026-11-27T01:00:00Z"},
            {"name": "macos", "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": "2026-11-27T01:05:00Z"},
        ]
        self.assertEqual(gq.missing_required_checks(checks_rerun_ok, cfg), [])
        # Older SUCCESS followed by newer SKIPPED -> missing
        checks_latest_skipped = [
            {"name": "ubuntu", "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": "2026-11-27T01:00:00Z"},
            {"name": "macos", "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": "2026-11-27T01:00:00Z"},
            {"name": "macos", "status": "COMPLETED", "conclusion": "SKIPPED", "completedAt": "2026-11-27T01:05:00Z"},
        ]
        self.assertEqual(gq.missing_required_checks(checks_latest_skipped, cfg), ["macos"])
        checks_failed = [
            {"name": "ubuntu", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "macos", "status": "COMPLETED", "conclusion": "FAILURE"},
        ]
        self.assertEqual(gq.missing_required_checks(checks_failed, cfg), [])

    def test_github_ci_status_superseded_failure_and_running_rerun(self):
        gh_forge = GitHubForge()
        # Older CANCELLED / FAILURE followed by newer SUCCESS -> passes (G3.1)
        pr_ok = self._pr([
            {"name": "ubuntu", "status": "COMPLETED", "conclusion": "SUCCESS", "startedAt": "2026-11-27T01:00:00Z", "completedAt": "2026-11-27T01:02:00Z"},
            {"name": "macos", "status": "COMPLETED", "conclusion": "CANCELLED", "startedAt": "2026-11-27T01:00:00Z", "completedAt": "2026-11-27T01:01:00Z"},
            {"name": "macos", "status": "COMPLETED", "conclusion": "FAILURE", "startedAt": "2026-11-27T01:02:00Z", "completedAt": "2026-11-27T01:03:00Z"},
            {"name": "macos", "status": "COMPLETED", "conclusion": "SUCCESS", "startedAt": "2026-11-27T01:05:00Z", "completedAt": "2026-11-27T01:07:00Z"},
        ])
        res_ok = gh_forge.ci_status(pr_ok, required_checks=["ubuntu", "macos"])
        self.assertEqual(res_ok.state, CI_SUCCESS)
        self.assertEqual(res_ok.failing, [])
        self.assertEqual(res_ok.pending, [])
        # Older SUCCESS followed by newer IN_PROGRESS with zero completedAt -> pending (G3.1)
        pr_running = self._pr([
            {"name": "ubuntu", "status": "COMPLETED", "conclusion": "SUCCESS", "startedAt": "2026-11-27T01:00:00Z", "completedAt": "2026-11-27T01:02:00Z"},
            {"name": "macos", "status": "COMPLETED", "conclusion": "SUCCESS", "startedAt": "2026-11-27T01:00:00Z", "completedAt": "2026-11-27T01:02:00Z"},
            {"name": "macos", "status": "IN_PROGRESS", "conclusion": "", "startedAt": "2026-11-27T01:05:00Z", "completedAt": "0001-01-01T00:00:00Z"},
        ])
        res_running = gh_forge.ci_status(pr_running, required_checks=["ubuntu", "macos"])
        self.assertEqual(res_running.state, CI_PENDING)
        self.assertIn("macos", res_running.pending)

    def test_gitea_ci_status_superseded_failure_and_running_rerun(self):
        g = GiteaForge(base_url="https://gitea.example", owner="o", name="r")
        pr = PullRequest(
            number=1, title="t", url="u", head_ref="b", head_sha="sha1", base_ref="main",
            is_draft=False, author="me",
        )
        # Older failure followed by newer success -> passes (G3.1)
        with mock.patch.object(
            g, "_http_call",
            return_value=(200, {"statuses": [
                {"context": "ubuntu", "status": "success", "created_at": "2026-11-27T01:00:00Z"},
                {"context": "macos", "status": "failure", "created_at": "2026-11-27T01:00:00Z"},
                {"context": "macos", "status": "success", "created_at": "2026-11-27T01:05:00Z"},
            ]}),
        ):
            res_ok = g.ci_status(pr, required_checks=["ubuntu", "macos"])
            self.assertEqual(res_ok.state, CI_SUCCESS)
            self.assertEqual(res_ok.failing, [])
        # Older success followed by newer pending -> pending (G3.1)
        with mock.patch.object(
            g, "_http_call",
            return_value=(200, {"statuses": [
                {"context": "ubuntu", "status": "success", "created_at": "2026-11-27T01:00:00Z"},
                {"context": "macos", "status": "success", "created_at": "2026-11-27T01:00:00Z"},
                {"context": "macos", "status": "pending", "created_at": "2026-11-27T01:05:00Z"},
            ]}),
        ):
            res_pending = g.ci_status(pr, required_checks=["ubuntu", "macos"])
            self.assertEqual(res_pending.state, CI_PENDING)
            self.assertIn("macos", res_pending.pending)

    def test_queue_effective_checks_ignores_superseded_required_runs(self):
        cfg = {"gatekeeper": {"required_checks": ["ubuntu", "macos"]}}
        checks_ok = [
            {"name": "ubuntu", "status": "COMPLETED", "conclusion": "SUCCESS", "startedAt": "2026-11-27T01:00:00Z"},
            {"name": "macos", "status": "COMPLETED", "conclusion": "CANCELLED", "startedAt": "2026-11-27T01:00:00Z"},
            {"name": "macos", "status": "COMPLETED", "conclusion": "FAILURE", "startedAt": "2026-11-27T01:02:00Z"},
            {"name": "macos", "status": "COMPLETED", "conclusion": "SUCCESS", "startedAt": "2026-11-27T01:05:00Z"},
        ]
        eff_ok = gq.effective_checks(checks_ok, cfg)
        self.assertEqual([c["name"] for c in eff_ok if gq.check_state(c) in gq.FAILED], [])
        self.assertEqual(gq.missing_required_checks(checks_ok, cfg), [])
        checks_running = [
            {"name": "ubuntu", "status": "COMPLETED", "conclusion": "SUCCESS", "startedAt": "2026-11-27T01:00:00Z", "completedAt": "2026-11-27T01:02:00Z"},
            {"name": "macos", "status": "COMPLETED", "conclusion": "SUCCESS", "startedAt": "2026-11-27T01:00:00Z", "completedAt": "2026-11-27T01:02:00Z"},
            {"name": "macos", "status": "IN_PROGRESS", "conclusion": "", "startedAt": "2026-11-27T01:05:00Z", "completedAt": "0001-01-01T00:00:00Z"},
        ]
        eff_run = gq.effective_checks(checks_running, cfg)
        self.assertEqual([c["name"] for c in eff_run if gq.check_state(c) in gq.PENDING], ["macos"])
        self.assertEqual(gq.missing_required_checks(checks_running, cfg), ["macos"])

    def test_config_validation_rejects_invalid_required_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "devloops.config.json"
            p.write_text(json.dumps({"gatekeeper": {"required_checks": "macos"}}), encoding="utf-8")
            with self.assertRaises(cfg_mod.ConfigError):
                cfg_mod.load_config(tmp)
            p.write_text(json.dumps({"gatekeeper": {"required_checks": ["ubuntu", "macos"]}}), encoding="utf-8")
            loaded = cfg_mod.load_config(tmp)
            self.assertEqual(loaded["gatekeeper"]["required_checks"], ["ubuntu", "macos"])


class ApproveRequiresGreenChecksTest(unittest.TestCase):
    """Issue #111: gatekeeper classify and qa signoff require green required_checks at current head."""

    CFG = {"gatekeeper": {"required_checks": ["macos"]}}

    def _classify_approved(self, checks, head="sha2"):
        missing = gq.missing_required_checks(checks, self.CFG, head=head)
        return gq.classify(
            mergeable="MERGEABLE",
            head=head,
            base="main",
            last_verdict={"verdict": "approve", "head": head, "base": "main", "round": 1},
            worker_response=None,
            total_verdicts=1,
            state_reviewed_head=head,
            state_pending_ci=False,
            has_open_findings=False,
            pending_checks=[],
            failing_checks=[],
            reviewed_base="main",
            missing_required_checks=missing,
            total_checks=len(checks),
        )

    def _signoff_pr(self, checks, head="sha2"):
        from devloops_lib import qa

        pr = {
            "number": 10,
            "headRefOid": head,
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "labels": [{"name": "gatekeeper:approved"}, {"name": "qa:accepted"}],
            "comments": [
                {"author": {"login": "me"}, "createdAt": "2026-11-27T01:00:00Z",
                 "body": f"<!-- gatekeeper-loop: round=1 verdict=approve head={head} base=main -->"},
                {"author": {"login": "me"}, "createdAt": "2026-11-27T01:01:00Z",
                 "body": f"<!-- qa-loop: kind=verdict verdict=accepted head={head} base=main -->"},
            ],
            "statusCheckRollup": checks,
        }
        return qa.signoff(pr, "me", config=self.CFG)

    def test_skipped_required_check_blocks_approved_and_signoff(self):
        checks = [{"name": "macos", "status": "COMPLETED", "conclusion": "SKIPPED", "headSha": "sha2"}]
        self.assertNotEqual(self._classify_approved(checks)[0], "approved")
        self.assertEqual(self._classify_approved(checks)[0], "await-ci")
        self.assertFalse(self._signoff_pr(checks)["mergeReady"])

    def test_absent_required_check_blocks_approved_and_signoff(self):
        checks = [{"name": "ubuntu", "status": "COMPLETED", "conclusion": "SUCCESS", "headSha": "sha2"}]
        self.assertNotEqual(self._classify_approved(checks)[0], "approved")
        self.assertEqual(self._classify_approved(checks)[0], "await-ci")
        self.assertFalse(self._signoff_pr(checks)["mergeReady"])

    def test_green_required_check_at_current_head_allows_approved_and_signoff(self):
        checks = [{"name": "macos", "status": "COMPLETED", "conclusion": "SUCCESS", "headSha": "sha2"}]
        self.assertEqual(self._classify_approved(checks)[0], "approved")
        self.assertTrue(self._signoff_pr(checks)["mergeReady"])

    def test_green_required_check_at_older_head_blocks_approved_and_signoff(self):
        checks = [{"name": "macos", "status": "COMPLETED", "conclusion": "SUCCESS", "headSha": "sha1"}]
        self.assertNotEqual(self._classify_approved(checks, head="sha2")[0], "approved")
        self.assertFalse(self._signoff_pr(checks, head="sha2")["mergeReady"])

    def test_qa_queue_signoff_cli_enforces_required_checks(self):
        # G1.1: qa_queue.main(["signoff", N]) fetches statusCheckRollup and passes config to qa.signoff.
        import contextlib
        import importlib.util
        import io
        import json

        qa_queue_path = Path(__file__).resolve().parent.parent / "skills" / "qa-loop" / "scripts" / "qa_queue.py"
        spec = importlib.util.spec_from_file_location("qa_queue_cli_111", qa_queue_path)
        qa_queue = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(qa_queue)
        self.assertIn("statusCheckRollup", qa_queue.PR_FIELDS)

        for conclusion, expected_exit, expected_ready in (
            ("SUCCESS", 0, True),
            ("SKIPPED", 1, False),
        ):
            with self.subTest(conclusion=conclusion):
                pr_payload = {
                    "number": 10,
                    "headRefOid": "sha2",
                    "baseRefName": "main",
                    "isDraft": False,
                    "mergeable": "MERGEABLE",
                    "state": "OPEN",
                    "labels": [{"name": "gatekeeper:approved"}, {"name": "qa:accepted"}],
                    "comments": [
                        {"author": {"login": "me"}, "createdAt": "2026-11-27T01:00:00Z",
                         "body": "<!-- gatekeeper-loop: round=1 verdict=approve head=sha2 base=main -->"},
                        {"author": {"login": "me"}, "createdAt": "2026-11-27T01:01:00Z",
                         "body": "<!-- qa-loop: kind=verdict verdict=accepted head=sha2 base=main -->"},
                    ],
                    "statusCheckRollup": [{"name": "macos", "status": "COMPLETED", "conclusion": conclusion}],
                }
                buf = io.StringIO()
                with mock.patch.object(qa_queue, "load_config", return_value=self.CFG), \
                     mock.patch.object(qa_queue, "_view", return_value=pr_payload), \
                     mock.patch.object(qa_queue, "_me", return_value="me"), \
                     contextlib.redirect_stdout(buf):
                    with self.assertRaises(SystemExit) as cm:
                        qa_queue.main(["signoff", "10"])
                self.assertEqual(cm.exception.code, expected_exit)
                data = json.loads(buf.getvalue())
                self.assertEqual(data["mergeReady"], expected_ready)

    def test_summarize_exposes_missing_required_checks_on_first_review(self):
        # G1.2: summarize returns missingRequiredChecks on a never-reviewed PR (and [] on stacked PRs).
        import json

        for base, checks, expected_missing in (
            ("main", [{"name": "ubuntu", "status": "COMPLETED", "conclusion": "SUCCESS"}], ["macos"]),
            ("main", [{"name": "macos", "status": "COMPLETED", "conclusion": "SKIPPED"}], ["macos"]),
            ("feat/parent", [{"name": "ubuntu", "status": "COMPLETED", "conclusion": "SUCCESS"}], []),
        ):
            with self.subTest(base=base, checks=checks):
                pr_payload = {
                    "number": 11,
                    "title": "feat: first review",
                    "body": "Summary",
                    "url": "https://example.invalid/pr/11",
                    "headRefName": "feat/first-11",
                    "headRefOid": "sha1",
                    "baseRefName": base,
                    "isDraft": False,
                    "mergeable": "MERGEABLE",
                    "createdAt": "2026-11-27T01:00:00Z",
                    "statusCheckRollup": checks,
                    "comments": [],
                    "labels": [],
                    "files": [],
                }
                cfg = {"project": {"base_branch": "main"}, "gatekeeper": {"required_checks": ["macos"]}}
                with mock.patch.object(gq, "gh", return_value=json.dumps(pr_payload)):
                    entry = gq.summarize(11, "me", state={}, config=cfg, readiness={"ci": {"present": True}})
                self.assertEqual(entry["status"], "review")
                self.assertEqual(entry["missingRequiredChecks"], expected_missing)


if __name__ == "__main__":
    unittest.main()
