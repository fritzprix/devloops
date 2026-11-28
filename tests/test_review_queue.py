#!/usr/bin/env python3
"""Tests for gatekeeper-loop review_queue status determination.

The implementation was promoted to devloops_lib.gatekeeper_queue (issue #71);
the skill script is a CLI shim, so tests import the canonical module directly
(module-global patches must hit the code that runs).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloops_lib import gatekeeper_queue as _module

PROTECTED_SKILL_PREFIXES = _module.PROTECTED_SKILL_PREFIXES
START_MARKER = _module.START_MARKER
classify = _module.classify
determine_status = _module.determine_status
is_dev_base = _module.is_dev_base
is_rebased = _module.is_rebased
owner_comments_after = _module.owner_comments_after
parse_markers = _module.parse_markers
needs_human_guidance = _module.needs_human_guidance
round_restart = _module.round_restart
queued_not_started = _module.queued_not_started
merge_queued_since = _module.merge_queued_since
stalled_checks = _module.stalled_checks
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
    def test_needs_owner_is_wait_owner(self) -> None:
        # G1.2: parked needs-owner items get no gatekeeper work.
        status, reason, _prio = classify(**base_args(needs_owner=True))
        self.assertEqual(status, "wait-owner")
        self.assertIn("needs-owner", reason)


    def test_open_blocker_is_blocked(self) -> None:
        status, reason, prio = classify(**base_args(open_blocker_numbers=[12, 3]))
        self.assertEqual(status, "blocked")
        self.assertIn("#3", reason)
        self.assertIn("#12", reason)
        self.assertEqual(prio, 9)


    def test_forge_error_is_blocked(self) -> None:
        status, reason, prio = classify(**base_args(forge_error="boom"))
        self.assertEqual(status, "blocked")
        self.assertIn("forge unavailable", reason)
        self.assertIn("boom", reason)
        self.assertEqual(prio, 9)


    def test_needs_owner_outranks_open_blocker(self) -> None:
        status, _, _ = classify(**base_args(needs_owner=True, open_blocker_numbers=[12]))
        self.assertEqual(status, "wait-owner")

    def test_open_blocker_outranks_merge_conflict(self) -> None:
        status, _, _ = classify(**base_args(
            mergeable="CONFLICTING", open_blocker_numbers=[9]))
        self.assertEqual(status, "blocked")


    def test_loop_skill_notice_flag_false_still_reviews(self) -> None:
        status = determine_status(**base_args(needs_owner=False))
        self.assertEqual(status, "review")

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

    def test_max_rounds_reads_from_config(self) -> None:
        # G2.1 / #102: gatekeeper.max_rounds overrides default 5; invalid values fall back to 5.
        args = base_args(
            total_verdicts=3,
            last_verdict={"round": "3", "verdict": "changes-requested", "head": "h0"},
        )
        self.assertEqual(determine_status(**args), "review")
        status, reason, _ = classify(**args, config={"gatekeeper": {"max_rounds": 3}})
        self.assertEqual(status, "escalate")
        self.assertIn("3", reason)
        self.assertEqual(determine_status(**args, config={"gatekeeper": {"max_rounds": 0}}), "review")
        self.assertEqual(determine_status(**args, config={"gatekeeper": {"max_rounds": True}}), "review")

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

    def test_qa_loop_marker_comment_is_not_an_owner_comment(self) -> None:
        # Issue #65 AC: qa-loop markers never read as owner comments.
        comments = [
            {"author": {"login": "owner"}, "createdAt": "2026-01-02T00:00:00Z",
             "body": "<!-- qa-loop: kind=verdict round=1 verdict=accepted head=a -->\n## QA",
             "authorAssociation": "OWNER"},
        ]
        self.assertEqual(owner_comments_after(comments, "me", "2026-01-01T00:00:00Z"), [])

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

    def test_clean_head_without_repo_ci_is_await_ci_not_bootstrap(self):
        # Issue #81: a repo without PR CI parks the PR in await-ci with the cause
        # named; the gatekeeper files a gap issue. No bootstrap status exists.
        status, reason, _ = classify(
            **base_args(clean_head="head1", total_checks=0),
            repo_has_ci=False,
        )
        self.assertEqual(status, "await-ci")
        self.assertIn("no PR CI", reason)

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

    def test_needs_owner_marker_is_not_a_verdict(self) -> None:
        comments = [
            {"author": {"login": "me"}, "createdAt": "2026-01-01T00:00:00Z",
             "body": "<!-- gatekeeper-loop: kind=needs-owner reason=no-ci-on-head head=abc -->"}]
        markers, _ = parse_markers(comments, "me")
        verdicts, cleans, needs = split_markers(markers)
        self.assertEqual((verdicts, cleans), ([], []))
        self.assertEqual(needs[0]["reason"], "no-ci-on-head")

    def test_old_needs_human_marker_is_ignored(self) -> None:
        # kind=needs-human no longer exists in the vocabulary: the marker is
        # ignored entirely, not a verdict and not a needs-owner signal.
        comments = [
            {"author": {"login": "me"}, "createdAt": "2026-01-01T00:00:00Z",
             "body": "<!-- gatekeeper-loop: kind=needs-human reason=no-ci-on-head head=abc -->"}]
        markers, _ = parse_markers(comments, "me")
        self.assertEqual(markers, [])

    def test_running_same_head_twice_does_not_inflate_rounds(self) -> None:
        body = "<!-- gatekeeper-loop: kind=needs-owner reason=no-ci-on-head head=abc -->"
        comments = [{"author": {"login": "me"}, "createdAt": f"2026-01-0{n}T00:00:00Z", "body": body}
                    for n in (1, 2)]
        markers, _ = parse_markers(comments, "me")
        verdicts, _, needs = split_markers(markers)
        self.assertEqual(verdicts, [])
        self.assertEqual(len(needs), 2)  # tick sees commentPresent and posts nothing new

    def test_own_skill_pr_keeps_needs_human_reason_but_normal_status(self) -> None:
        # Issue #62/#81: loop-skill PRs get needsHuman guidance, and the guidance
        # no longer even looks at status — the status itself is never overridden.
        guidance = needs_human_guidance(own_skill=True,
                                        labels=["gatekeeper:needs-human"], needs_comment_present=True)
        self.assertEqual(guidance["reason"], "changes-loop-skill-files")
        self.assertTrue(guidance["labelPresent"])
        self.assertTrue(guidance["commentPresent"])
        self.assertFalse(guidance["staleLabel"])

    def test_own_skill_without_label_asks_to_add(self) -> None:
        guidance = needs_human_guidance(own_skill=True,
                                        labels=[], needs_comment_present=False)
        self.assertEqual(guidance["reason"], "changes-loop-skill-files")
        self.assertFalse(guidance["labelPresent"])
        self.assertFalse(guidance["commentPresent"])

    def test_human_only_status_is_gone(self) -> None:
        # The `human-only` status must not exist in the module anymore.
        import inspect
        source = inspect.getsource(_module)
        self.assertNotIn("human-only", source)
        self.assertNotIn("human_only", source)

    def test_ambiguous_criteria_is_not_a_status_guidance(self) -> None:
        # Issue #81: ambiguous review criteria becomes a needs-owner *issue*,
        # never a needs-human label reason or a review skip.
        self.assertIsNone(needs_human_guidance(own_skill=False, labels=[], needs_comment_present=False))
        # A leftover ambiguous-criteria label is stale and should be removed.
        guidance = needs_human_guidance(own_skill=False, labels=["gatekeeper:needs-human"],
                                        needs_comment_present=True)
        self.assertTrue(guidance["staleLabel"])
        self.assertIsNone(guidance["reason"])

    def test_resolved_needs_human_label_is_stale(self) -> None:
        guidance = needs_human_guidance(
            own_skill=False,
            labels=["gatekeeper:needs-human", "gatekeeper:changes-requested"],
            needs_comment_present=True)
        self.assertTrue(guidance["staleLabel"])
        self.assertIsNone(guidance["reason"])

    def test_no_needs_human_when_clean_and_unlabelled(self) -> None:
        self.assertIsNone(needs_human_guidance(
            own_skill=False,
            labels=["gatekeeper:approved"], needs_comment_present=False))

    def test_path_name_match_is_candidate_not_needs_human(self) -> None:
        paths = ["docs/research/permissions-notes.md", "src/app.py"]
        self.assertEqual(sensitive_candidates(paths), ["docs/research/permissions-notes.md"])
        # A reviewable PR whose only signal is a path-name match gets no human gate.
        self.assertIsNone(needs_human_guidance(
            own_skill=False, labels=[], needs_comment_present=False))

    def test_path_only_needs_human_label_is_stale(self) -> None:
        guidance = needs_human_guidance(
            own_skill=False,
            labels=["gatekeeper:needs-human"], needs_comment_present=True)
        self.assertTrue(guidance["staleLabel"])

    def test_real_sensitive_files_still_surface_as_candidates(self) -> None:
        paths = ["src/shell_policy.rs", "config/credentials.yaml", "src/sandbox/mod.rs",
                 "secure/keys.toml", "README.md"]
        self.assertEqual(
            sensitive_candidates(paths, ("secure/",)),
            ["config/credentials.yaml", "secure/keys.toml", "src/sandbox/mod.rs", "src/shell_policy.rs"])


class Issue81AlwaysReviewTests(unittest.TestCase):
    """Issue #81: the queue never emits a skip-review status; stalled CI is reportable."""

    def test_no_skip_review_status_exists(self) -> None:
        import inspect
        source = inspect.getsource(_module)
        self.assertNotIn("await-" + "bootstrap", source)
        self.assertNotIn('status == "needs-human"', source)  # no needs-human status branch
        self.assertNotIn('"needs-human",', source)           # no needs-human status return
        # classify never returns a skip status; every path is one of the known ones.
        allowed = {"review", "await-ci", "wait-worker", "approved", "escalate"}
        for kwargs in (
            base_args(mergeable="CONFLICTING"),
            base_args(),
            base_args(clean_head="head1", total_checks=0),
            base_args(clean_head="head1", total_checks=0, repo_has_ci=False),
            base_args(clean_head="head1", pending_checks=["t"], total_checks=1),
            base_args(clean_head="head1", stalled=["t"]),
            base_args(total_verdicts=5, last_verdict={"head": "x", "verdict": "changes-requested"}),
        ):
            status, reason, _ = classify(**kwargs)
            self.assertIn(status, allowed)
            self.assertTrue(reason)

    def test_loop_skill_path_keeps_reviewable_status(self) -> None:
        # A loop-skill file change is never parked: guidance only signals the merge owner.
        for status in ("review", "await-ci", "approved", "wait-worker"):
            guidance = needs_human_guidance(own_skill=True, labels=["gatekeeper:needs-human"],
                                            needs_comment_present=True)
            self.assertEqual(guidance["reason"], "changes-loop-skill-files", status)
            self.assertFalse(guidance["staleLabel"], status)

    def test_queued_not_started_excludes_in_progress(self) -> None:
        checks = [
            {"name": "Build", "state": "QUEUED"},
            {"name": "Test", "status": "IN_PROGRESS"},
            {"context": "Lint", "state": "REQUESTED"},
            {"name": "Pass", "state": "SUCCESS"},
        ]
        self.assertEqual(queued_not_started(checks), ["Build", "Lint"])

    def test_merge_queued_since_keeps_first_seen_and_drops_finished(self) -> None:
        merged = merge_queued_since(
            ["Build", "Lint"],
            {"head": "abc", "checks": {"Build": "2026-11-27T00:00:00Z", "Old": "x"}},
            "2026-11-27T01:00:00Z",
            head="abc",
        )
        self.assertEqual(merged["head"], "abc")
        self.assertEqual(merged["checks"]["Build"], "2026-11-27T00:00:00Z")
        self.assertEqual(merged["checks"]["Lint"], "2026-11-27T01:00:00Z")
        self.assertNotIn("Old", merged["checks"])

    def test_merge_queued_since_resets_when_head_changes(self) -> None:
        # G1.1: clocks from an earlier head must not stall a new push immediately.
        prev = {"head": "old", "checks": {"Build": "2026-11-01T00:00:00Z"}}
        merged = merge_queued_since(["Build"], prev, "2026-11-27T01:00:00Z", head="new")
        self.assertEqual(merged["head"], "new")
        self.assertEqual(merged["checks"]["Build"], "2026-11-27T01:00:00Z")
        self.assertEqual(
            stalled_checks(["Build"], prev, "2026-11-27T01:00:00Z", 45, head="new"),
            [],
        )

    def test_stalled_after_limit_but_not_before(self) -> None:
        since = {"head": "h", "checks": {"Build": "2026-11-27T00:00:00Z"}}
        at_40m = stalled_checks(["Build"], since, "2026-11-27T00:40:00Z", limit_minutes=45, head="h")
        self.assertEqual(at_40m, [])
        at_46m = stalled_checks(["Build"], since, "2026-11-27T00:46:00Z", limit_minutes=45, head="h")
        self.assertEqual(at_46m, ["Build"])
        # A check first seen this tick (no recorded since) is never stalled yet.
        self.assertEqual(stalled_checks(["Fresh"], since, "2026-11-27T05:00:00Z", 45, head="h"), [])

    def test_stalled_check_reports_on_await_ci_with_cause(self) -> None:
        status, reason, priority = classify(
            **base_args(clean_head="head1", pending_checks=["Build"], total_checks=1),
            stalled=["Build"],
        )
        self.assertEqual(status, "await-ci")
        self.assertIn("Build", reason)
        self.assertIn("not started", reason)
        self.assertLess(priority, 9)  # reportable now, not parked

    def test_stalled_ignored_when_ci_does_not_apply(self) -> None:
        # G1.2: stacked/non-dev base still gets "issue the verdict".
        status, reason, _ = classify(
            **base_args(
                clean_head="head1",
                base="feat/parent",
                pending_checks=["Build"],
                total_checks=1,
                stalled=["Build"],
            ),
        )
        self.assertEqual(status, "review")
        self.assertIn("issue the verdict", reason)

    def test_clean_verdict_valid_while_ci_pending(self) -> None:
        # reviewed clean + pending CI -> await-ci (clean stands; no re-review, no verdict held).
        status, reason, _ = classify(**base_args(
            clean_head="head1", pending_checks=["Test"], total_checks=1))
        self.assertEqual(status, "await-ci")
        self.assertIn("clean", reason)

    def test_summarize_surfaces_ci_fields(self) -> None:
        import json
        from unittest.mock import patch
        pr_payload = {
            "number": 7, "title": "t", "url": "u", "headRefName": "b", "headRefOid": "abc",
            "baseRefName": "main", "isDraft": False, "mergeable": "MERGEABLE",
            "createdAt": "2026-01-01T00:00:00Z",
            "statusCheckRollup": [{"name": "Build", "state": "QUEUED"}],
            "comments": [], "labels": [], "files": [{"path": "a.py"}],
        }
        readiness = {"ci": {"present": True}, "rubric": {"status": "present"}}
        with patch.object(_module, "gh", return_value=json.dumps(pr_payload)):
            summary = _module.summarize(7, "me", state={}, config={"project": {"base_branch": "main"}},
                                        readiness=readiness)
        self.assertEqual(summary["ciQueued"], ["Build"])
        self.assertEqual(summary["ciStalled"], [])
        self.assertEqual(summary["queuedSince"]["head"], "abc")
        self.assertIn("Build", summary["queuedSince"]["checks"])


class RetargetBeforeMergeTests(unittest.TestCase):
    """Verify summarize() flags stacked PRs with retargetBeforeMerge (issue #54)."""

    def _summarize_with_base(self, base: str) -> dict:
        import json
        from unittest.mock import patch

        pr_payload = {
            "number": 42,
            "title": "feat: stacked child",
            "url": "https://example.com/pr/42",
            "headRefName": "feat/child-2",
            "headRefOid": "abc123",
            "baseRefName": base,
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "createdAt": "2026-01-01T00:00:00Z",
            "statusCheckRollup": [],
            "comments": [],
            "labels": [],
            "files": [{"path": "devloops_lib/pr_status.py"}],
        }
        readiness = {"ci": {"present": True}, "rubric": {"status": "present"}}
        with patch.object(_module, "gh", return_value=json.dumps(pr_payload)):
            return _module.summarize(42, "me", state={}, config={"project": {"base_branch": "main"}}, readiness=readiness)

    def test_non_primary_base_sets_retarget_before_merge_true(self) -> None:
        summary = self._summarize_with_base("feat/parent-1")
        self.assertTrue(summary["retargetBeforeMerge"])

    def test_primary_base_sets_retarget_before_merge_false(self) -> None:
        summary = self._summarize_with_base("main")
        self.assertFalse(summary["retargetBeforeMerge"])


class EscalationExitTests(unittest.TestCase):
    """Issue #70: label-parked escalation, approve at the last round, round restart."""

    def test_escalated_label_parks_pr_even_with_new_head_and_owner_comment(self) -> None:
        status, reason, priority = classify(
            **base_args(
                head="head9",
                last_verdict={"head": "head5", "verdict": "escalate"},
                total_verdicts=3,
                owner_comment=True,
                escalated=True,
            )
        )
        self.assertEqual(status, "escalate")
        self.assertEqual(priority, 9)
        self.assertIn("gatekeeper:escalated", reason)

    def test_owner_comment_alone_does_not_end_escalation(self) -> None:
        # With the label still on, an owner comment must not unpark the PR (#70).
        status, reason, priority = classify(
            **base_args(
                head="head1",
                last_verdict={"head": "head1", "verdict": "escalate"},
                total_verdicts=3,
                owner_comment=True,
                escalated=True,
            )
        )
        self.assertEqual(status, "escalate")
        self.assertEqual(priority, 9)
        self.assertIn("gatekeeper:escalated", reason)
        # Removing the label restarts the count; a bare comment is then normal review.
        verdicts = [{"head": "head1", "verdict": "escalate", "createdAt": "2026-01-01T00:00:00Z"}]
        counted, restart_at = round_restart(verdicts, escalated=False)
        self.assertEqual(counted, [])
        self.assertEqual(restart_at, "2026-01-01T00:00:00Z")

    def test_summarize_ignores_clean_older_than_restart(self) -> None:
        # G1.1: after label removal, a pre-escalation clean at the same head must
        # not skip re-review of the escalated question.
        import json
        from unittest.mock import patch

        from devloops_lib import contract

        head = "abc123"
        comments = [
            {
                "author": {"login": "me"},
                "createdAt": "2026-01-01T00:00:00Z",
                "body": f"<!-- gatekeeper-loop: round=1 verdict=clean head={head} -->\nclean",
                "url": "https://example.com/c1",
            },
            {
                "author": {"login": "me"},
                "createdAt": "2026-01-02T00:00:00Z",
                "body": f"<!-- gatekeeper-loop: round=5 verdict=escalate head={head} -->\nescalate",
                "url": "https://example.com/c2",
            },
        ]
        pr_payload = {
            "number": 70,
            "title": "feat: escalate exit",
            "url": "https://example.com/pr/70",
            "headRefName": "feat/escalate-exit-70",
            "headRefOid": head,
            "baseRefName": "main",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "createdAt": "2026-01-01T00:00:00Z",
            "statusCheckRollup": [{"name": "ci", "conclusion": "SUCCESS"}],
            "comments": comments,
            "labels": [],  # owner removed gatekeeper:escalated
            "files": [{"path": "devloops_lib/gatekeeper_queue.py"}],
        }
        state = {
            "prs": {
                "70": {
                    "reviewedHead": head,
                    "pendingApproveWaitCi": True,
                    "findings": {},
                }
            }
        }
        readiness = {"ci": {"present": True}, "rubric": {"status": "present"}}
        with patch.object(_module, "gh", return_value=json.dumps(pr_payload)):
            summary = _module.summarize(
                70, "me", state=state,
                config={"project": {"base_branch": "main"}},
                readiness=readiness,
            )
        self.assertEqual(summary["status"], "review")
        self.assertNotIn("reviewed clean", summary["reason"])
        self.assertIsNone(summary.get("cleanMarker"))
        self.assertNotIn(contract.GATEKEEPER_ESCALATED, summary["labels"])

    def test_round_limit_without_label_still_reports_escalate(self) -> None:
        # G1.2: status escalate on round limit tells the skill to add the label
        # and post the escalate verdict (exit remains label removal).
        status, reason, _priority = classify(
            **base_args(
                head="head6",
                last_verdict={"head": "head5", "verdict": "changes-requested"},
                total_verdicts=5,
                escalated=False,
            )
        )
        self.assertEqual(status, "escalate")
        self.assertIn("round limit", reason)

    def test_round_count_restarts_after_label_removal(self) -> None:
        verdicts = [
            {"head": "h1", "verdict": "changes-requested", "createdAt": "2026-01-01T00:00:00Z"},
            {"head": "h1", "verdict": "escalate", "createdAt": "2026-01-02T00:00:00Z"},
            {"head": "h2", "verdict": "changes-requested", "createdAt": "2026-01-03T00:00:00Z"},
        ]
        counted, restart_at = round_restart(verdicts, escalated=False)
        self.assertEqual(len(counted), 1)
        self.assertEqual(restart_at, "2026-01-02T00:00:00Z")
        # While parked, nothing restarts; all verdicts keep counting.
        self.assertEqual(round_restart(verdicts, escalated=True), (verdicts, None))

    def test_approve_as_last_allowed_verdict_stands(self) -> None:
        status = determine_status(
            **base_args(
                head="head5",
                last_verdict={"head": "head5", "verdict": "approve"},
                total_verdicts=5,
                reviewed_base="main",
            )
        )
        self.assertEqual(status, "approved")

    def test_round_limit_still_escalates_without_approve(self) -> None:
        status = determine_status(
            **base_args(
                head="head6",
                last_verdict={"head": "head5", "verdict": "changes-requested"},
                total_verdicts=5,
            )
        )
        self.assertEqual(status, "escalate")


if __name__ == "__main__":
    unittest.main()
