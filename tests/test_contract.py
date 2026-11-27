"""Tests for devloops_lib.contract: the shared label, overlap-order, and marker
grammar contract (issue #65)."""
from __future__ import annotations

import unittest
from pathlib import Path

import sys
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloops_lib import contract  # noqa: E402
from devloops_lib.forge.models import Blocker  # noqa: E402


def comment(body: str, at: str = "2026-01-01T00:00:00Z", author: str = "me") -> dict:
    return {"author": {"login": author}, "body": body, "createdAt": at, "url": "http://x/c"}


# Each contract state and the trigger that makes it apply to a PR.
STATE_TRIGGERS = {
    "needs-owner": ({"labels": [contract.NEEDS_OWNER]}, {}),
    "gatekeeper:escalated": ({"labels": [contract.GATEKEEPER_ESCALATED]}, {}),
    "open-blocker": ({"labels": []}, {"blockers": [Blocker(7, "open")]}),
    "merge-conflict": ({"labels": []}, {"mergeable": "CONFLICTING"}),
    "verdict": ({"labels": [contract.GATEKEEPER_APPROVED]}, {}),
}


class OverlapTest(unittest.TestCase):
    def test_no_state(self):
        self.assertIsNone(contract.first_state([]))
        self.assertIsNone(contract.first_state(["bug"], "MERGEABLE"))

    def test_blocked_base_label_is_gone(self):
        # issue #65: base blocking is derived from open blockers, never a label.
        self.assertFalse(hasattr(contract, "BLOCKED_BASE"))
        self.assertNotIn("blocked:base", contract.STATE_ORDER)
        self.assertIsNone(contract.first_state(["blocked:base"]))

    def test_single_states(self):
        for state, (labels, extra) in STATE_TRIGGERS.items():
            self.assertEqual(contract.first_state(**labels, **extra), state, state)

    def test_closed_blockers_do_not_apply(self):
        self.assertIsNone(contract.first_state([], blockers=[Blocker(7, "closed")]))
        self.assertIsNone(contract.first_state([], "MERGEABLE", [Blocker(7, "CLOSED")]))

    def test_every_pair_resolves_to_the_earlier_state(self):
        for i, a in enumerate(contract.STATE_ORDER):
            for b in contract.STATE_ORDER[i + 1:]:
                labels_a, extra_a = STATE_TRIGGERS[a]
                labels_b, extra_b = STATE_TRIGGERS[b]
                labels = labels_a["labels"] + labels_b["labels"]
                extra = {**extra_a, **extra_b}
                self.assertEqual(
                    contract.first_state(labels, extra.get("mergeable"), extra.get("blockers")), a,
                    f"{a} must outrank {b}")


class OpenBlockersTest(unittest.TestCase):
    class Forge:
        def __init__(self, blockers):
            self._blockers = blockers

        def blocked_by(self, number):
            return list(self._blockers)

    def test_sorted_open_numbers_only(self):
        forge = self.Forge([Blocker(12, "open"), Blocker(5, "closed"), Blocker(3, "open", True)])
        self.assertEqual(contract.open_blockers(forge, 20), [3, 12])

    def test_no_blockers(self):
        self.assertEqual(contract.open_blockers(self.Forge([]), 20), [])


class MarkerVocabularyTest(unittest.TestCase):
    def test_gatekeeper_verdicts_parse(self):
        for v in contract.GATEKEEPER_VERDICTS:
            parsed = contract.parse_marker(f"<!-- gatekeeper-loop: round=1 verdict={v} head=sha -->")
            self.assertIsNotNone(parsed, v)
            self.assertEqual(parsed[0], "gatekeeper-loop")
            self.assertEqual(parsed[1]["verdict"], v)

    def test_gatekeeper_needs_owner_kind_parses(self):
        parsed = contract.parse_marker("<!-- gatekeeper-loop: kind=needs-owner reason=x head=sha -->")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[1]["kind"], "needs-owner")

    def test_gatekeeper_needs_human_kind_is_gone(self):
        self.assertIsNone(contract.parse_marker("<!-- gatekeeper-loop: kind=needs-human reason=x head=sha -->"))

    def test_gatekeeper_clean_rejects_blocked_by(self):
        # Owner decision G1.3: dependencies live only on issues, never on clean markers.
        self.assertIsNone(contract.parse_marker("<!-- gatekeeper-loop: verdict=clean head=sha base=main blocked-by=#42 -->"))
        self.assertIsNone(contract.parse_marker("<!-- gatekeeper-loop: verdict=clean head=sha blocked-by=soon -->"))

    def test_pr_blockers_read_from_closed_issues_only(self):
        from devloops_lib.forge.models import Blocker
        queried = []
        class Forge:
            def blocked_by(self, number):
                queried.append(number)
                if number == 10:
                    return [Blocker(7, "open"), Blocker(8, "closed")]
                return []
        self.assertEqual(contract.closed_issue_numbers("Fixes #10, closes #11"), [10, 11])
        self.assertEqual(contract.pr_blocker_numbers(Forge(), "Fixes #10"), [7, 8])
        self.assertEqual(contract.pr_open_blockers(Forge(), "Fixes #10"), [7])
        self.assertEqual(queried, [10, 10])
        self.assertEqual(contract.pr_open_blockers(Forge(), "no closing keyword"), [])

    def test_worker_kinds_parse(self):
        for k in contract.WORKER_KINDS:
            parsed = contract.parse_marker(f"<!-- worker-loop: {k} round=1 head=sha -->")
            self.assertIsNotNone(parsed, k)
            self.assertEqual(parsed[1]["kind"], k)

    def test_worker_unknown_kind_ignored(self):
        self.assertIsNone(contract.parse_marker("<!-- worker-loop: merge round=1 -->"))
        self.assertIsNone(contract.parse_marker("<!-- worker-loop: response=2 round=1 -->"))

    def test_qa_markers_parse(self):
        parsed = contract.parse_marker("<!-- qa-loop: kind=verdict round=1 verdict=accepted head=sha -->")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[1]["kind"], "verdict")
        parsed = contract.parse_marker("<!-- qa-loop: kind=post-merge pr=7 build=sha check=t -->")
        self.assertIsNotNone(parsed)
        parsed = contract.parse_marker("<!-- qa-loop: kind=needs-owner reason=x head=sha -->")
        self.assertIsNotNone(parsed)

    def test_qa_unknown_verdict_or_kind_ignored(self):
        self.assertIsNone(contract.parse_marker("<!-- qa-loop: kind=verdict verdict=maybe head=sha -->"))
        self.assertIsNone(contract.parse_marker("<!-- qa-loop: kind=approve head=sha -->"))

    def test_anchor_bom_and_quote_rules(self):
        self.assertIsNotNone(contract.parse_marker("<!-- gatekeeper-loop: verdict=approve -->"))
        self.assertIsNotNone(contract.parse_marker("\ufeff\n  <!-- gatekeeper-loop: verdict=approve -->"))
        self.assertIsNone(contract.parse_marker("> <!-- gatekeeper-loop: verdict=approve -->"))
        self.assertIsNone(contract.parse_marker("Previous:\n<!-- gatekeeper-loop: verdict=approve -->"))
        self.assertIsNone(contract.parse_marker("plain comment"))

    def test_marker_comment_never_an_owner_comment_including_unknown_kind(self):
        self.assertTrue(contract.is_marker("<!-- qa-loop: kind=verdict verdict=accepted head=sha -->"))
        self.assertTrue(contract.is_marker("<!-- gatekeeper-loop: kind=needs-human head=sha -->"))
        self.assertFalse(contract.is_marker("just text"))


class ParseMarkersTest(unittest.TestCase):
    def test_author_filter_and_order(self):
        comments = [
            comment("<!-- qa-loop: kind=verdict round=2 verdict=accepted head=sha -->", "2026-01-03T00:00:00Z"),
            comment("<!-- gatekeeper-loop: round=1 verdict=changes-requested head=sha -->", "2026-01-01T00:00:00Z"),
            comment("<!-- gatekeeper-loop: round=2 verdict=clean head=sha -->", "2026-01-02T00:00:00Z", author="x"),
        ]
        markers = contract.parse_markers(comments, "me")
        self.assertEqual([m["createdAt"] for m in markers], ["2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z"])
        self.assertEqual([m["loop"] for m in markers], ["gatekeeper-loop", "qa-loop"])
        self.assertEqual(contract.parse_markers(comments, "me", loop="qa-loop")[0]["kind"], "verdict")


class SingleDefinitionTest(unittest.TestCase):
    def test_consumers_use_the_contract_pattern(self):
        from devloops_lib import pr_status
        self.assertIs(pr_status.START_MARKER, contract.MARKER)

    def test_config_and_forge_labels_are_the_contract_labels(self):
        from devloops_lib import config
        from devloops_lib.forge import cli
        self.assertEqual(config.GATEKEEPER_APPROVED_LABEL, contract.GATEKEEPER_APPROVED)
        self.assertEqual(cli.VERDICT_LABELS, contract.GATEKEEPER_VERDICT_LABELS)
        self.assertEqual(config.DEFAULT_CONFIG["labels"],
                         {"producer": contract.PRODUCER, "validated": contract.VALIDATED,
                          "rejected": contract.REJECTED, "approved": contract.APPROVED})

    def test_issue_label_honours_config_override(self):
        cfg = {"labels": {"producer": "bot:found"}}
        self.assertEqual(contract.issue_label(cfg, "producer"), "bot:found")
        self.assertEqual(contract.issue_label({}, "producer"), contract.PRODUCER)


class NeedsOwnerProtocolTest(unittest.TestCase):
    """Issue #67: set, wait, answer, clear for the needs-owner ask."""

    ME = "ci-bot"

    def comment(self, body, at, author="someone", assoc="NONE"):
        return {"author": {"login": author}, "body": body, "createdAt": at,
                "authorAssociation": assoc, "url": "https://x/c/1"}

    def ask(self, at):
        return self.comment("<!-- worker-loop: kind=needs-owner ref=issue-42 -->\nQ?", at)

    def test_vocabulary_has_no_needs_human_qa_verdict(self):
        self.assertNotIn("needs-human", contract.QA_VERDICTS)
        self.assertNotIn("needs-human", getattr(contract, "QA_VERDICT_LABELS", ()))

    def test_wait_while_no_answer(self):
        # Set: one needs-owner marker parks the item.
        comments = [self.ask("2026-01-01T00:00:00Z")]
        self.assertTrue(contract.needs_owner_parked(comments, self.ME))

    def test_marker_comments_never_answer(self):
        comments = [self.ask("2026-01-01T00:00:00Z"),
                    self.comment("<!-- gatekeeper-loop: round=1 verdict=clean head=h -->",
                                 "2026-01-02T00:00:00Z", author=self.ME, assoc="OWNER")]
        self.assertTrue(contract.needs_owner_parked(comments, self.ME))

    def test_third_party_comment_never_answers(self):
        comments = [self.ask("2026-01-01T00:00:00Z"),
                    self.comment("I think option 2", "2026-01-02T00:00:00Z",
                                 author="random", assoc="NONE")]
        self.assertTrue(contract.needs_owner_parked(comments, self.ME))

    def test_answer_by_owner_member_or_collaborator_clears(self):
        for assoc in ("OWNER", "MEMBER", "COLLABORATOR"):
            comments = [self.ask("2026-01-01T00:00:00Z"),
                        self.comment("take option 1", "2026-01-02T00:00:00Z",
                                     author="owner", assoc=assoc)]
            self.assertFalse(contract.needs_owner_parked(comments, self.ME), assoc)

    def test_answer_by_shared_account_clears(self):
        comments = [self.ask("2026-01-01T00:00:00Z"),
                    self.comment("answer via shared account", "2026-01-02T00:00:00Z",
                                 author=self.ME, assoc="")]
        self.assertFalse(contract.needs_owner_parked(comments, self.ME))

    def test_comment_before_the_ask_is_not_an_answer(self):
        comments = [self.comment("unrelated", "2026-01-01T00:00:00Z",
                                 author="owner", assoc="OWNER"),
                    self.ask("2026-01-02T00:00:00Z")]
        self.assertTrue(contract.needs_owner_parked(comments, self.ME))

    def test_label_without_ask_stays_parked(self):
        self.assertTrue(contract.needs_owner_parked([], self.ME))

    def test_loop_skill_notice_is_not_an_ask(self):
        for reason in ("changes-loop-skill-files", "changes loop skill files", "loop-skill"):
            fields = {"kind": "needs-owner", "reason": reason, "head": "abc"}
            self.assertFalse(contract.is_needs_owner_ask(fields), reason)

    def test_real_ask_reasons_still_count(self):
        self.assertTrue(contract.is_needs_owner_ask(
            {"kind": "needs-owner", "reason": "tool-blocked"}
        ))
        self.assertTrue(contract.is_needs_owner_ask({"kind": "needs-owner"}))

    def test_loop_skill_notice_does_not_park_without_label(self):
        notice = self.comment(
            "<!-- qa-loop: kind=needs-owner reason=changes-loop-skill-files head=h -->",
            "2026-01-01T00:00:00Z", author=self.ME,
        )
        self.assertFalse(contract.needs_owner_parked([notice], self.ME, labeled=False))

    def test_notice_after_answered_ask_does_not_repark(self):
        comments = [
            self.ask("2026-01-01T00:00:00Z"),
            self.comment("take option 1", "2026-01-02T00:00:00Z",
                         author="owner", assoc="OWNER"),
            self.comment(
                "<!-- gatekeeper-loop: kind=needs-owner reason=changes-loop-skill-files head=h -->",
                "2026-01-03T00:00:00Z", author=self.ME,
            ),
        ]
        self.assertFalse(contract.needs_owner_parked(comments, self.ME, labeled=False))
        self.assertFalse(contract.needs_owner_parked(comments, self.ME, labeled=True))

    def test_first_owner_reply_returns_the_answer(self):
        comments = [self.ask("2026-01-01T00:00:00Z"),
                    self.comment("take option 1", "2026-01-02T00:00:00Z",
                                 author="owner", assoc="MEMBER")]
        reply = contract.first_owner_reply(comments, self.ME, "2026-01-01T12:00:00Z")
        self.assertEqual(reply["author"], "owner")
        self.assertEqual(reply["createdAt"], "2026-01-02T00:00:00Z")


class GatekeeperNoteMarkerTest(unittest.TestCase):
    """Issue #114: gatekeeper note marker is never counted as an owner comment."""

    def test_gatekeeper_note_parses_and_is_not_owner_reply(self):
        self.assertEqual(
            contract.parse_marker("<!-- gatekeeper-loop: note -->\nBase CI is red."),
            ("gatekeeper-loop", {"kind": "note"}),
        )
        self.assertEqual(
            contract.parse_marker("<!-- gatekeeper-loop: kind=note -->\nBase CI is red."),
            ("gatekeeper-loop", {"kind": "note"}),
        )
        comments = [
            comment("<!-- gatekeeper-loop: kind=needs-owner reason=ambiguity -->", "2026-01-01T01:00:00Z"),
            comment("<!-- gatekeeper-loop: note -->\nBase CI is red.", "2026-01-01T02:00:00Z"),
        ]
        self.assertIsNone(contract.first_owner_reply(comments, "me", "2026-01-01T01:00:00Z"))

    def test_marked_note_does_not_trigger_any_classifier_while_unmarked_does(self):
        import json
        from unittest import mock
        from devloops_lib import gatekeeper_queue, pr_status, qa

        base_comments = [
            comment("<!-- gatekeeper-loop: round=1 verdict=approve head=sha1 base=main -->", "2026-01-01T01:00:00Z"),
            comment("<!-- qa-loop: kind=verdict verdict=accepted head=sha1 base=main -->", "2026-01-01T01:05:00Z"),
        ]
        note_comments = base_comments + [
            comment("<!-- gatekeeper-loop: note -->\nExternal CI note.", "2026-01-01T02:00:00Z"),
        ]
        unmarked_comments = base_comments + [
            comment("Owner follow-up on shared account.", "2026-01-01T02:00:00Z"),
        ]

        def classify_gk(comments):
            verdicts = [
                m for m in contract.parse_markers(comments, "me", loop="gatekeeper-loop")
                if m.get("verdict")
            ]
            last_v = verdicts[-1]
            owner_c = gatekeeper_queue.owner_comments_after(comments, "me", last_v["createdAt"])
            return gatekeeper_queue.classify(
                mergeable="MERGEABLE",
                head="sha1",
                base="main",
                last_verdict=last_v,
                worker_response=None,
                total_verdicts=len(verdicts),
                state_reviewed_head="sha1",
                state_pending_ci=False,
                has_open_findings=False,
                pending_checks=[],
                failing_checks=[],
                reviewed_base="main",
                owner_comment=bool(owner_c),
            )

        def classify_qa(comments):
            return qa.classify_pr(
                {"number": 50, "headRefOid": "sha1", "comments": comments, "labels": []},
                "me",
            )

        def summarize_worker(comments):
            pr = {
                "number": 50,
                "title": "feat: test",
                "body": "Summary",
                "url": "https://example.invalid/pr/50",
                "headRefName": "feat/test-50",
                "headRefOid": "sha1",
                "baseRefName": "main",
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "reviewDecision": "",
                "statusCheckRollup": [],
                "commits": [{"committedDate": "2026-01-01T00:30:00Z"}],
                "comments": comments,
                "labels": [],
            }
            def fake_gh(*args, **kwargs):
                if args and args[0] == "api":
                    return json.dumps({"data": {}})
                return json.dumps(pr)
            with mock.patch.object(pr_status, "gh", side_effect=fake_gh), \
                 mock.patch.object(pr_status, "duplicate_commits", return_value=[]), \
                 mock.patch.object(pr_status, "parent_pr_merged", return_value=None):
                return pr_status.summarize(50, "owner", "repo", "me", default_base="main", merged_prs=[])

        # Gatekeeper queue: status unchanged after marked note, changes to review after unmarked owner comment.
        before_gk = classify_gk(base_comments)
        after_note_gk = classify_gk(note_comments)
        after_owner_gk = classify_gk(unmarked_comments)
        self.assertEqual(after_note_gk[0], before_gk[0])
        self.assertEqual(after_owner_gk[0], "review")

        # QA classifier: status unchanged after marked note, changes to review after unmarked owner comment.
        before_qa = classify_qa(base_comments)
        after_note_qa = classify_qa(note_comments)
        after_owner_qa = classify_qa(unmarked_comments)
        self.assertEqual(after_note_qa["status"], before_qa["status"])
        self.assertEqual(after_owner_qa["status"], "review")

        # Worker classifier: marked note does not trigger triage-comments; unmarked comment does.
        self.assertEqual(summarize_worker(note_comments)["nextAction"], "none")
        self.assertEqual(summarize_worker(unmarked_comments)["nextAction"], "triage-comments")

    def test_base_break_scenario_rebases_after_blocker_closes(self):
        import json
        from unittest import mock
        from devloops_lib import gatekeeper_queue, pr_status, qa

        comments = [
            comment("<!-- gatekeeper-loop: verdict=clean head=sha1 base=main -->", "2026-01-01T01:00:00Z"),
            comment("<!-- gatekeeper-loop: note -->\nCI failed due to base break #99.", "2026-01-01T01:05:00Z"),
        ]
        pr = {
            "number": 51,
            "title": "feat: feature (Fixes #10)",
            "body": "Fixes #10",
            "url": "https://example.invalid/pr/51",
            "headRefName": "feat/feature-10",
            "headRefOid": "sha1",
            "baseRefName": "main",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "UNSTABLE",
            "reviewDecision": "",
            "statusCheckRollup": [],
            "commits": [{"committedDate": "2026-01-01T00:30:00Z"}],
            "comments": comments,
            "labels": [],
        }

        class ClosedBlockerForge:
            def blocked_by(self, number):
                return [Blocker(99, "closed")]

        def fake_gh(*args, **kwargs):
            if args and args[0] == "api":
                return json.dumps({"data": {}})
            return json.dumps(pr)

        with mock.patch.object(pr_status, "gh", side_effect=fake_gh), \
             mock.patch.object(pr_status, "duplicate_commits", return_value=[]), \
             mock.patch.object(pr_status, "parent_pr_merged", return_value=None), \
             mock.patch("devloops_lib.forge.detect.get_forge", return_value=ClosedBlockerForge()):
            summary = pr_status.summarize(51, "owner", "repo", "me", default_base="main", merged_prs=[])

        self.assertEqual(summary["newComments"], [])
        self.assertEqual(summary["nextAction"], "rebase")
        self.assertEqual(gatekeeper_queue.owner_comments_after(comments, "me", "2026-01-01T01:00:00Z"), [])
        self.assertEqual(qa.owner_comments_after(comments, "me", "2026-01-01T01:00:00Z"), [])

    def test_summarize_note_after_owner_comment_preserves_review_status(self):
        # G1.1: a gatekeeper note posted after an unhandled owner comment must not
        # advance last_marker_at and hide the owner comment.
        import json
        from unittest import mock
        from devloops_lib import gatekeeper_queue

        pr_payload = {
            "number": 52,
            "title": "feat: test",
            "body": "Summary",
            "url": "https://example.invalid/pr/52",
            "headRefName": "feat/test-52",
            "headRefOid": "sha1",
            "baseRefName": "main",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "createdAt": "2026-01-01T00:00:00Z",
            "statusCheckRollup": [],
            "labels": [{"name": "gatekeeper:approved"}],
            "files": [],
            "comments": [
                comment("<!-- gatekeeper-loop: round=1 verdict=approve head=sha1 base=main -->", "2026-01-01T01:00:00Z"),
                comment("Please check edge case X.", "2026-01-01T02:00:00Z"),
                comment("<!-- gatekeeper-loop: note -->\nCI runner stalled.", "2026-01-01T02:05:00Z"),
            ],
        }
        with mock.patch.object(gatekeeper_queue, "gh", return_value=json.dumps(pr_payload)):
            summary = gatekeeper_queue.summarize(
                52,
                "me",
                state={},
                config={"project": {"base_branch": "main"}},
                readiness={"ci": {"present": True}},
            )
        self.assertEqual(summary["status"], "review")
        self.assertEqual(len(summary["ownerComments"]), 1)
        self.assertIn("edge case X", summary["ownerComments"][0]["body"])


if __name__ == "__main__":
    unittest.main()


class BlockerRebaseTest(unittest.TestCase):
    """Issue #68: wait while open, rebase when closed + clean at head."""

    class Forge:
        def __init__(self, blockers):
            self._blockers = blockers

        def blocked_by(self, number):
            return list(self._blockers)

    def test_blocker_numbers_includes_closed(self):
        forge = self.Forge([Blocker(12, "open"), Blocker(5, "closed")])
        self.assertEqual(contract.blocker_numbers(forge, 20), [5, 12])

    def test_needs_rebase_only_when_links_closed_and_clean_matches(self):
        self.assertFalse(contract.needs_blocker_rebase(
            blocker_links=[], open_blocker_numbers=[], head="abc", clean_head="abc"))
        self.assertFalse(contract.needs_blocker_rebase(
            blocker_links=[5], open_blocker_numbers=[5], head="abc", clean_head="abc"))
        self.assertFalse(contract.needs_blocker_rebase(
            blocker_links=[5], open_blocker_numbers=[], head="abc", clean_head="old"))
        self.assertTrue(contract.needs_blocker_rebase(
            blocker_links=[5], open_blocker_numbers=[], head="abc", clean_head="abc"))
