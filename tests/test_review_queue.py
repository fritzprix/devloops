#!/usr/bin/env python3
"""Tests for gatekeeper-loop review_queue status determination."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_script_path = REPO_ROOT / "skills" / "gatekeeper-loop" / "scripts" / "review_queue.py"
_spec = importlib.util.spec_from_file_location("gatekeeper_review_queue", _script_path)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

PROTECTED_SKILL_PREFIXES = _module.PROTECTED_SKILL_PREFIXES
START_MARKER = _module.START_MARKER
classify = _module.classify
determine_status = _module.determine_status
is_dev_base = _module.is_dev_base
is_rebased = _module.is_rebased
owner_comments_after = _module.owner_comments_after
parse_markers = _module.parse_markers
needs_human_guidance = _module.needs_human_guidance
sensitive_candidates = _module.sensitive_candidates
split_markers = _module.split_markers


def base_args(**over):
    args = dict(
        mergeable="MERGEABLE", head="head1", base="main", last_verdict=None,
        worker_response=None, total_verdicts=0, state_reviewed_head=None,
        state_pending_ci=False, has_open_findings=False, pending_checks=[], failing_checks=[],
    )
    args.update(over)
    return args


class DetermineStatusTests(unittest.TestCase):
    def test_conflict_is_wait_worker(self) -> None:
        status = determine_status(
            mergeable="CONFLICTING",
            head="head1",
            base="main",
            last_verdict=None,
            worker_response=None,
            total_verdicts=0,
            state_reviewed_head=None,
            state_pending_ci=False,
            has_open_findings=False,
            pending_checks=[],
            failing_checks=[],
        )
        self.assertEqual(status, "wait-worker")

    def test_approved_at_current_head(self) -> None:
        status = determine_status(
            mergeable="MERGEABLE",
            head="head1",
            base="main",
            last_verdict={"head": "head1", "verdict": "approve"},
            worker_response=None,
            total_verdicts=1,
            state_reviewed_head="head1",
            state_pending_ci=False,
            has_open_findings=False,
            pending_checks=[],
            failing_checks=[],
        )
        self.assertEqual(status, "approved")

    def test_changes_requested_without_response_is_wait_worker(self) -> None:
        status = determine_status(
            mergeable="MERGEABLE",
            head="head1",
            base="main",
            last_verdict={"head": "head1", "verdict": "changes-requested"},
            worker_response=None,
            total_verdicts=1,
            state_reviewed_head="head1",
            state_pending_ci=False,
            has_open_findings=True,
            pending_checks=[],
            failing_checks=[],
        )
        self.assertEqual(status, "wait-worker")

    def test_changes_requested_with_response_is_review(self) -> None:
        status = determine_status(
            mergeable="MERGEABLE",
            head="head1",
            base="main",
            last_verdict={"head": "head1", "verdict": "changes-requested"},
            worker_response={"head": "head1"},
            total_verdicts=1,
            state_reviewed_head="head1",
            state_pending_ci=False,
            has_open_findings=True,
            pending_checks=["Test"],
            failing_checks=[],
        )
        self.assertEqual(status, "review")

    def test_new_head_after_verdict_is_review(self) -> None:
        status = determine_status(
            mergeable="MERGEABLE",
            head="head2",
            base="main",
            last_verdict={"head": "head1", "verdict": "changes-requested"},
            worker_response=None,
            total_verdicts=1,
            state_reviewed_head="head1",
            state_pending_ci=False,
            has_open_findings=True,
            pending_checks=["Test"],
            failing_checks=[],
        )
        self.assertEqual(status, "review")

    def test_unreviewed_head_with_failing_ci_is_review(self) -> None:
        # Failing CI on an unreviewed head must be reviewed, NOT skipped to wait-worker.
        status = determine_status(
            mergeable="MERGEABLE",
            head="head1",
            base="main",
            last_verdict=None,
            worker_response=None,
            total_verdicts=0,
            state_reviewed_head=None,
            state_pending_ci=False,
            has_open_findings=False,
            pending_checks=[],
            failing_checks=["Build (ubuntu-latest)"],
        )
        self.assertEqual(status, "review")

    def test_reviewed_head_waiting_ci_is_await_ci(self) -> None:
        # Head was already reviewed with 0 findings; waiting on CI.
        status = determine_status(
            mergeable="MERGEABLE",
            head="head1",
            base="main",
            last_verdict=None,
            worker_response=None,
            total_verdicts=0,
            state_reviewed_head="head1",
            state_pending_ci=True,
            has_open_findings=False,
            pending_checks=["Test (ubuntu-latest)"],
            failing_checks=[],
        )
        self.assertEqual(status, "await-ci")

    def test_await_ci_turns_to_review_when_ci_green(self) -> None:
        # When pending checks clear with 0 failures, enters review to post approve verdict.
        status = determine_status(
            mergeable="MERGEABLE",
            head="head1",
            base="main",
            last_verdict=None,
            worker_response=None,
            total_verdicts=0,
            state_reviewed_head="head1",
            state_pending_ci=True,
            has_open_findings=False,
            pending_checks=[],
            failing_checks=[],
        )
        self.assertEqual(status, "review")

    def test_await_ci_turns_to_review_when_ci_fails(self) -> None:
        # When a pending check fails, enters review so gatekeeper diagnoses logs and requests changes.
        status = determine_status(
            mergeable="MERGEABLE",
            head="head1",
            base="main",
            last_verdict=None,
            worker_response=None,
            total_verdicts=0,
            state_reviewed_head="head1",
            state_pending_ci=True,
            has_open_findings=False,
            pending_checks=[],
            failing_checks=["Test (macos-15)"],
        )
        self.assertEqual(status, "review")

    def test_new_head_pushed_while_await_ci_is_review(self) -> None:
        # New commit pushed -> immediately review diff, do not stay in await-ci.
        status = determine_status(
            mergeable="MERGEABLE",
            head="head2",
            base="main",
            last_verdict=None,
            worker_response=None,
            total_verdicts=0,
            state_reviewed_head="head1",
            state_pending_ci=True,
            has_open_findings=False,
            pending_checks=["Test"],
            failing_checks=[],
        )
        self.assertEqual(status, "review")

    def test_stacked_pr_does_not_await_ci(self) -> None:
        # Base is a feature branch -> CI does not trigger, never block on await-ci.
        status = determine_status(
            mergeable="MERGEABLE",
            head="head1",
            base="fix/feature-stacked",
            last_verdict=None,
            worker_response=None,
            total_verdicts=0,
            state_reviewed_head="head1",
            state_pending_ci=True,
            has_open_findings=False,
            pending_checks=["Any"],
            failing_checks=[],
        )
        self.assertEqual(status, "review")

    def test_max_rounds_escalates(self) -> None:
        status = determine_status(
            mergeable="MERGEABLE",
            head="head6",
            base="main",
            last_verdict={"head": "head5", "verdict": "changes-requested"},
            worker_response=None,
            total_verdicts=5,
            state_reviewed_head="head5",
            state_pending_ci=False,
            has_open_findings=False,
            pending_checks=[],
            failing_checks=[],
        )
        self.assertEqual(status, "escalate")

    def test_is_dev_base(self) -> None:
        self.assertTrue(is_dev_base("main"))
        self.assertTrue(is_dev_base("master"))
        self.assertTrue(is_dev_base("dev/main"))
        self.assertFalse(is_dev_base("fix/feature-branch"))
        self.assertFalse(is_dev_base("feat/something"))


class GapTests(unittest.TestCase):
    approve = {"head": "head1", "verdict": "approve", "base": "main"}

    def test_approved_with_ci_failure_is_review(self) -> None:
        status, reason, _ = classify(**base_args(
            last_verdict=self.approve, total_verdicts=1, failing_checks=["Test"]))
        self.assertEqual(status, "review")
        self.assertIn("CI failing", reason)

    def test_approved_stacked_ignores_ci(self) -> None:
        verdict = {**self.approve, "base": "feat/x"}
        status = determine_status(**base_args(
            base="feat/x", last_verdict=verdict, total_verdicts=1, failing_checks=["Test"]))
        self.assertEqual(status, "approved")

    def test_base_change_after_approve_is_review(self) -> None:
        verdict = {**self.approve, "base": "feat/x"}
        status, reason, _ = classify(**base_args(
            base="main", last_verdict=verdict, total_verdicts=1, reviewed_base="feat/x"))
        self.assertEqual(status, "review")
        self.assertIn("base changed", reason)

    def test_missing_state_with_clean_marker_is_await_ci(self) -> None:
        status = determine_status(**base_args(
            clean_head="head1", state_reviewed_head=None, pending_checks=["Test"]))
        self.assertEqual(status, "await-ci")

    def test_clean_marker_for_old_head_is_review(self) -> None:
        status = determine_status(**base_args(clean_head="head0", pending_checks=["Test"]))
        self.assertEqual(status, "review")

    def test_clean_marker_with_green_ci_is_review(self) -> None:
        status = determine_status(**base_args(clean_head="head1"))
        self.assertEqual(status, "review")

    def test_rebased_head_is_review(self) -> None:
        last = {"head": "old", "verdict": "changes-requested"}
        status, reason, _ = classify(**base_args(
            last_verdict=last, total_verdicts=1, worker_response={"head": "head1"}, rebased=True))
        self.assertEqual(status, "review")
        self.assertIn("rebased", reason)

    def test_is_rebased(self) -> None:
        self.assertFalse(is_rebased(None, "h", lambda a, b: True))
        self.assertFalse(is_rebased("h", "h", lambda a, b: False))
        self.assertFalse(is_rebased("old", "new", lambda a, b: True))
        self.assertTrue(is_rebased("old", "new", lambda a, b: False))

    def test_owner_comment_after_verdict_is_review(self) -> None:
        last = {"head": "head1", "verdict": "changes-requested"}
        status, _, priority = classify(**base_args(
            last_verdict=last, total_verdicts=1, owner_comment=True))
        self.assertEqual(status, "review")
        self.assertEqual(priority, 0)

    def test_owner_comment_beats_approved(self) -> None:
        status = determine_status(**base_args(
            last_verdict=self.approve, total_verdicts=1, owner_comment=True))
        self.assertEqual(status, "review")

    def test_owner_comments_after_filters(self) -> None:
        comments = [
            {"author": {"login": "me"}, "body": "<!-- gatekeeper-loop: round=1 verdict=clean head=a -->",
             "createdAt": "2026-01-01T00:00:00Z"},
            {"author": {"login": "me"}, "body": "Update README for config", "createdAt": "2026-01-02T00:00:00Z"},
            {"author": {"login": "bot[bot]"}, "body": "ci", "createdAt": "2026-01-02T00:00:00Z"},
            {"author": {"login": "rando"}, "body": "hi", "createdAt": "2026-01-02T00:00:00Z",
             "authorAssociation": "NONE"},
            {"author": {"login": "me"}, "body": "old note", "createdAt": "2025-12-31T00:00:00Z"},
        ]
        found = owner_comments_after(comments, "me", "2026-01-01T00:00:00Z")
        self.assertEqual([c["body"] for c in found], ["Update README for config"])

    def test_clean_marker_parsed(self) -> None:
        comments = [{"author": {"login": "me"}, "createdAt": "t",
                     "body": "<!-- gatekeeper-loop: verdict=clean head=abc -->"}]
        verdicts, _ = parse_markers(comments, "me")
        self.assertEqual(verdicts[0]["verdict"], "clean")
        self.assertEqual(verdicts[0]["head"], "abc")

    def test_protected_skill_prefixes(self) -> None:
        self.assertIn(".agents/skills/gatekeeper-loop/", PROTECTED_SKILL_PREFIXES)
        self.assertIn(".agents/skills/worker-loop/", PROTECTED_SKILL_PREFIXES)
        self.assertIn("skills/gatekeeper-loop/", PROTECTED_SKILL_PREFIXES)
        self.assertIn("skills/worker-loop/", PROTECTED_SKILL_PREFIXES)

    def test_quoted_markers_ignored(self) -> None:
        comments = [
            {"author": {"login": "me"}, "createdAt": "t",
             "body": "> <!-- gatekeeper-loop: verdict=approve head=abc -->\nQuoting older verdict"}
        ]
        verdicts, responses = parse_markers(comments, "me")
        self.assertEqual(len(verdicts), 0)
        self.assertEqual(len(responses), 0)

    def test_bom_marker_parsed(self) -> None:
        self.assertTrue(START_MARKER.match("\ufeff<!-- gatekeeper-loop: verdict=approve -->"))
        comments = [
            {"author": {"login": "me"}, "createdAt": "t",
             "body": "\ufeff<!-- gatekeeper-loop: verdict=clean head=abc -->"}
        ]
        verdicts, _ = parse_markers(comments, "me")
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0]["verdict"], "clean")

    def test_owner_comment_quoting_marker_is_surfaced(self) -> None:
        comments = [
            {"author": {"login": "me"},
             "body": "> <!-- gatekeeper-loop: verdict=changes-requested -->\nOwner comment quoting feedback",
             "createdAt": "2026-01-02T00:00:00Z"}
        ]
        found = owner_comments_after(comments, "me", "2026-01-01T00:00:00Z")
        self.assertEqual(len(found), 1)

    def test_every_status_has_reason(self) -> None:
        for kwargs in (
            base_args(mergeable="CONFLICTING"),
            base_args(),
            base_args(total_verdicts=5, last_verdict={"head": "x", "verdict": "changes-requested"}),
        ):
            self.assertTrue(classify(**kwargs)[1])


class Issue14ClassificationTests(unittest.TestCase):
    """Classification matrix for issue #14: clean precedence and CI states."""

    cr_at_old = {"head": "head0", "verdict": "changes-requested", "round": "1"}

    def test_old_non_clean_verdict_loses_to_current_head_clean_await_ci(self) -> None:
        # Older changes-requested + clean marker at the *current* head -> not re-reviewed.
        status, reason, _ = classify(**base_args(
            last_verdict=self.cr_at_old, total_verdicts=1,
            clean_head="head1", pending_checks=["Test"], total_checks=1))
        self.assertEqual(status, "await-ci")
        self.assertNotIn("new head", reason)

    def test_old_non_clean_verdict_with_current_clean_issues_verdict(self) -> None:
        status, reason, _ = classify(**base_args(
            last_verdict=self.cr_at_old, total_verdicts=1, clean_head="head1", total_checks=2))
        self.assertEqual(status, "review")
        self.assertIn("issue the verdict", reason)

    def test_older_clean_verdict_with_new_head_is_review(self) -> None:
        # Clean belongs to an older head; the new head has no clean marker -> review.
        status, _, _ = classify(**base_args(
            clean_head="head0", pending_checks=["Test"], total_checks=1))
        self.assertEqual(status, "review")

    def test_clean_head_without_repo_ci_is_await_bootstrap(self) -> None:
        status, reason, _ = classify(
            **base_args(clean_head="head1", total_checks=0),
            repo_has_ci=False,
        )
        self.assertEqual(status, "await-bootstrap")
        self.assertIn("CI", reason)

    def test_clean_head_with_repo_ci_but_no_runs_is_await_ci(self) -> None:
        status, reason, _ = classify(
            **base_args(clean_head="head1", total_checks=0),
            repo_has_ci=True,
        )
        self.assertEqual(status, "await-ci")
        self.assertIn("waiting for CI", reason)

    def test_clean_head_pending_checks_is_await_ci(self) -> None:
        status, _, _ = classify(**base_args(
            clean_head="head1", pending_checks=["Test"], total_checks=1))
        self.assertEqual(status, "await-ci")

    def test_clean_head_green_ci_is_approve_path(self) -> None:
        status, reason, _ = classify(**base_args(clean_head="head1", total_checks=2))
        self.assertEqual(status, "review")
        self.assertIn("issue the verdict", reason)

    def test_clean_head_failing_ci_is_ci_repair(self) -> None:
        status, reason, _ = classify(**base_args(
            clean_head="head1", failing_checks=["Build"], total_checks=1))
        self.assertEqual(status, "review")
        self.assertIn("CI failing", reason)

    def test_stacked_clean_head_without_ci_is_not_needs_human(self) -> None:
        # CI does not apply to stacked PRs; zero checks must not block.
        status, _, _ = classify(**base_args(
            base="feat/stale", clean_head="head1", total_checks=0))
        self.assertEqual(status, "review")

    def test_total_checks_none_keeps_legacy_behavior(self) -> None:
        status, _, _ = classify(**base_args(clean_head="head1"))
        self.assertEqual(status, "review")

    def test_needs_human_status_has_reason(self) -> None:
        _, reason, _ = classify(**base_args(clean_head="head1", total_checks=0))
        self.assertTrue(reason)


class Issue14NeedsHumanTests(unittest.TestCase):
    """Needs-human marker parsing, idempotency, and label guidance (issue #14)."""

    def test_needs_human_marker_is_not_a_verdict(self) -> None:
        comments = [
            {"author": {"login": "me"}, "createdAt": "2026-01-01T00:00:00Z",
             "body": "<!-- gatekeeper-loop: kind=needs-human reason=no-ci-on-head head=abc -->"}]
        markers, _ = parse_markers(comments, "me")
        verdicts, cleans, needs = split_markers(markers)
        self.assertEqual((verdicts, cleans), ([], []))
        self.assertEqual(needs[0]["reason"], "no-ci-on-head")

    def test_running_same_head_twice_does_not_inflate_rounds(self) -> None:
        body = "<!-- gatekeeper-loop: kind=needs-human reason=no-ci-on-head head=abc -->"
        comments = [{"author": {"login": "me"}, "createdAt": f"2026-01-0{n}T00:00:00Z", "body": body}
                    for n in (1, 2)]
        markers, _ = parse_markers(comments, "me")
        verdicts, _, needs = split_markers(markers)
        self.assertEqual(verdicts, [])
        self.assertEqual(len(needs), 2)  # tick sees commentPresent and posts nothing new

    def test_human_only_guidance(self) -> None:
        guidance = needs_human_guidance(status="human-only", own_skill=True,
                                        labels=["gatekeeper:needs-human"], needs_comment_present=True)
        self.assertEqual(guidance["reason"], "changes loop skill files")
        self.assertTrue(guidance["labelPresent"])
        self.assertTrue(guidance["commentPresent"])
        self.assertFalse(guidance["staleLabel"])

    def test_ambiguous_rubric_guidance(self) -> None:
        guidance = needs_human_guidance(status="needs-human", own_skill=False,
                                        labels=[], needs_comment_present=False)
        self.assertEqual(guidance["reason"], "ambiguous review criteria")
        self.assertFalse(guidance["labelPresent"])
        self.assertFalse(guidance["commentPresent"])

    def test_resolved_needs_human_label_is_stale(self) -> None:
        guidance = needs_human_guidance(
            status="review", own_skill=False,
            labels=["gatekeeper:needs-human", "gatekeeper:changes-requested"],
            needs_comment_present=True)
        self.assertTrue(guidance["staleLabel"])
        self.assertIsNone(guidance["reason"])

    def test_no_needs_human_when_clean_and_unlabelled(self) -> None:
        self.assertIsNone(needs_human_guidance(
            status="approved", own_skill=False,
            labels=["gatekeeper:approved"], needs_comment_present=False))

    def test_path_name_match_is_candidate_not_needs_human(self) -> None:
        paths = ["docs/research/permissions-notes.md", "src/app.py"]
        self.assertEqual(sensitive_candidates(paths), ["docs/research/permissions-notes.md"])
        # A reviewable PR whose only signal is a path-name match gets no human gate.
        self.assertIsNone(needs_human_guidance(
            status="review", own_skill=False, labels=[], needs_comment_present=False))

    def test_path_only_needs_human_label_is_stale(self) -> None:
        guidance = needs_human_guidance(
            status="review", own_skill=False,
            labels=["gatekeeper:needs-human"], needs_comment_present=True)
        self.assertTrue(guidance["staleLabel"])

    def test_real_sensitive_files_still_surface_as_candidates(self) -> None:
        paths = ["src/shell_policy.rs", "config/credentials.yaml", "src/sandbox/mod.rs",
                 "secure/keys.toml", "README.md"]
        self.assertEqual(
            sensitive_candidates(paths, ("secure/",)),
            ["config/credentials.yaml", "secure/keys.toml", "src/sandbox/mod.rs", "src/shell_policy.rs"])


if __name__ == "__main__":
    unittest.main()
