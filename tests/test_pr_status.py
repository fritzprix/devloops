#!/usr/bin/env python3
"""Tests for pr_status.duplicate_commits."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloops_lib import pr_status


def git(cwd: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def commit(cwd: str, name: str) -> str:
    Path(cwd, name).write_text(name)
    git(cwd, "add", name)
    git(cwd, "commit", "-m", name)
    return git(cwd, "rev-parse", "HEAD")


class DuplicateCommitsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = self._tmp.name
        git(self.repo, "init", "-q", "-b", "main")
        commit(self.repo, "root")

    def push_ref(self, branch: str):
        git(self.repo, "update-ref", f"refs/remotes/origin/{branch}", branch)

    def test_clean_stack_has_no_duplicates(self):
        git(self.repo, "switch", "-q", "-c", "lower")
        commit(self.repo, "lower1")
        git(self.repo, "switch", "-q", "-c", "upper")
        commit(self.repo, "upper1")
        self.push_ref("lower")
        self.push_ref("upper")
        self.assertEqual(pr_status.duplicate_commits("lower", "upper", self.repo), [])

    def test_base_behind_without_overlap_is_not_a_duplicate(self):
        git(self.repo, "switch", "-q", "-c", "feature")
        commit(self.repo, "feat1")
        git(self.repo, "switch", "-q", "main")
        commit(self.repo, "main2")
        self.push_ref("main")
        self.push_ref("feature")
        self.assertEqual(pr_status.duplicate_commits("main", "feature", self.repo), [])

    def test_rebased_base_makes_old_copies_duplicates(self):
        git(self.repo, "switch", "-q", "-c", "lower")
        old_lower = commit(self.repo, "lower1")
        git(self.repo, "switch", "-q", "-c", "upper")
        own = commit(self.repo, "upper1")
        # lower is rebased onto a new main commit: same patch, new SHA
        git(self.repo, "switch", "-q", "main")
        commit(self.repo, "main2")
        git(self.repo, "switch", "-q", "lower")
        git(self.repo, "rebase", "-q", "main")
        self.assertNotEqual(git(self.repo, "rev-parse", "lower"), old_lower)
        for b in ("lower", "upper"):
            self.push_ref(b)
        dups = pr_status.duplicate_commits("lower", "upper", self.repo)
        self.assertEqual(dups, [old_lower])
        self.assertNotIn(own, dups)

    def test_missing_ref_returns_empty(self):
        self.assertEqual(pr_status.duplicate_commits("nope", "also-nope", self.repo), [])


class SelectNextActionTest(unittest.TestCase):
    base = dict(
        retarget=False, conflicting=False, duplicates=[], gatekeeper=None, qa=None,
        has_comments=False, failing=[], pending=[],
    )

    def action(self, **overrides):
        return pr_status.select_next_action(**{**self.base, **overrides})

    def test_priority_order(self):
        everything = dict(
            retarget=True, conflicting=True, duplicates=["abc"], gatekeeper={"awaitingWorker": True},
            qa={"awaitingWorker": True},
            has_comments=True, failing=["ci"], pending=["ci"],
        )
        expected = [
            ("retarget", "retarget"),
            ("conflicting", "resolve-conflict"),
            ("duplicates", "restack"),
            ("gatekeeper", "address-gatekeeper"),
            ("qa", "address-qa"),
            ("has_comments", "triage-comments"),
            ("failing", "fix-ci"),
            ("pending", "wait-ci"),
        ]
        for key, action in expected:
            self.assertEqual(self.action(**everything), action)
            everything[key] = self.base[key]
        self.assertEqual(self.action(**everything), "none")

    def test_escalated_pr_yields_no_worker_action(self):
        # Issue #70 / G1.3: gatekeeper:escalated parks the PR for the owner.
        for overrides in (
            {"escalated": True, "conflicting": True},
            {"escalated": True, "duplicates": ["abc"]},
            {"escalated": True, "qa": {"awaitingWorker": True}},
            {"escalated": True, "has_comments": True},
            {"escalated": True, "failing": ["ci"]},
            {"escalated": True, "retarget": True},
        ):
            self.assertEqual(self.action(**overrides), "none", overrides)


class StackedPrLifecycleTest(unittest.TestCase):
    def test_parent_pr_merged_triggers_retarget(self):
        merged = [
            {"number": 10, "headRefName": "feat/parent-1", "baseRefName": "main", "url": "https://example.com/pr/10"},
        ]
        info = pr_status.parent_pr_merged("feat/parent-1", "main", merged)
        self.assertEqual(info, {"number": 10, "base": "main", "url": "https://example.com/pr/10"})
        # Non-stacked PR targeting default base never retargets.
        self.assertIsNone(pr_status.parent_pr_merged("main", "main", merged))
        # Stacked PR whose parent is still open does not retarget yet.
        self.assertIsNone(pr_status.parent_pr_merged("feat/still-open-2", "main", merged))
        # Branch name reused by a new open PR after an earlier merge: not retarget.
        self.assertIsNone(
            pr_status.parent_pr_merged(
                "feat/parent-1",
                "main",
                merged,
                open_head_refs={"feat/parent-1"},
            )
        )
        # Draft heads are included in open_head_refs (same rule as summarize_prs; #66 G1.2).
        draft_listed = [
            {"headRefName": "feat/parent-1", "isDraft": True},
            {"headRefName": "feat/child", "isDraft": False},
        ]
        open_heads = {p["headRefName"] for p in draft_listed if p.get("headRefName")}
        self.assertIn("feat/parent-1", open_heads)
        self.assertIsNone(
            pr_status.parent_pr_merged("feat/parent-1", "main", merged, open_head_refs=open_heads)
        )
    def test_find_merged_via_parent_issues(self):
        open_issues = [{"number": 20}, {"number": 30}]
        merged = [
            {
                "number": 100,
                "title": "feat: parent",
                "body": "Fixes #10",
                "headRefName": "feat/parent-10",
                "baseRefName": "main",
                "url": "https://example.com/pr/100",
            },
            {
                "number": 101,
                "title": "feat: child",
                "body": "Fixes #20",
                "headRefName": "feat/child-20",
                "baseRefName": "feat/parent-10",
                "url": "https://example.com/pr/101",
            },
            # Merged into an unmerged parent branch: not on main yet.
            {
                "number": 102,
                "title": "feat: orphan child",
                "body": "Fixes #30",
                "headRefName": "feat/child-30",
                "baseRefName": "feat/unmerged-parent",
                "url": "https://example.com/pr/102",
            },
        ]
        found = pr_status.find_merged_via_parent_issues(open_issues, merged, default_base="main")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["issue"], 20)
        self.assertEqual(found[0]["childPr"], 101)
        self.assertEqual(found[0]["parentPr"], 100)
        self.assertIn("close #20", found[0]["reason"])


class MarkerAndGatekeeperStateTest(unittest.TestCase):
    def test_marker_must_be_at_comment_start(self):
        self.assertTrue(pr_status.START_MARKER.match("<!-- gatekeeper-loop: verdict=approve -->"))
        self.assertTrue(pr_status.START_MARKER.match("\n  <!-- gatekeeper-loop: verdict=approve -->"))
        # Leading BOM support
        self.assertTrue(pr_status.START_MARKER.match("\ufeff<!-- gatekeeper-loop: verdict=approve -->"))
        self.assertTrue(pr_status.START_MARKER.match("\ufeff  \n<!-- gatekeeper-loop: verdict=approve -->"))
        # Quoted marker must not match
        self.assertIsNone(pr_status.START_MARKER.match("> <!-- gatekeeper-loop: verdict=approve -->"))
        # Marker later in body must not match
        self.assertIsNone(pr_status.START_MARKER.match("Previous verdict:\n<!-- gatekeeper-loop: verdict=approve -->"))

    def test_gatekeeper_state_ignores_quoted_verdicts(self):
        comments = [
            {
                "author": {"login": "me"},
                "createdAt": "2026-10-31T08:00:00Z",
                "body": "> <!-- gatekeeper-loop: verdict=changes-requested round=1 head=sha1 -->\nquoted text",
                "url": "http://example.com/c1",
            }
        ]
        state = pr_status.gatekeeper_state(comments, "me", "sha1")
        self.assertIsNone(state)

    def test_gatekeeper_state_valid_verdict(self):
        comments = [
            {
                "author": {"login": "me"},
                "createdAt": "2026-10-31T08:00:00Z",
                "body": "<!-- gatekeeper-loop: verdict=changes-requested round=1 head=sha1 -->\nReal verdict",
                "url": "http://example.com/c1",
            }
        ]
        state = pr_status.gatekeeper_state(comments, "me", "sha1")
        self.assertIsNotNone(state)
        self.assertEqual(state["verdict"], "changes-requested")
        self.assertTrue(state["awaitingWorker"])

    def test_needs_owner_marker_after_changes_requested_keeps_it_visible(self):
        # Issue #65 AC: a changes-requested verdict stays visible to the worker
        # whatever markers follow it.
        comments = [
            {"author": {"login": "me"}, "createdAt": "2026-10-31T08:00:00Z",
             "body": "<!-- gatekeeper-loop: round=1 verdict=changes-requested head=sha1 -->",
             "url": "http://example.com/c1"},
            {"author": {"login": "me"}, "createdAt": "2026-10-31T09:00:00Z",
             "body": "<!-- gatekeeper-loop: kind=needs-owner reason=ambiguous-review-criteria head=sha1 -->",
             "url": "http://example.com/c2"},
        ]
        state = pr_status.gatekeeper_state(comments, "me", "sha1")
        self.assertEqual(state["verdict"], "changes-requested")
        self.assertTrue(state["awaitingWorker"])

    def test_note_marker_does_not_settle_a_round(self):
        # Only kind=response worker markers answer a changes-requested verdict.
        comments = [
            {"author": {"login": "me"}, "createdAt": "2026-10-31T08:00:00Z",
             "body": "<!-- gatekeeper-loop: round=1 verdict=changes-requested head=sha1 -->",
             "url": "http://example.com/c1"},
            {"author": {"login": "me"}, "createdAt": "2026-10-31T09:00:00Z",
             "body": "<!-- worker-loop: note round=1 head=sha1 -->",
             "url": "http://example.com/c2"},
        ]
        state = pr_status.gatekeeper_state(comments, "me", "sha1")
        self.assertTrue(state["awaitingWorker"])

    def test_response_marker_settles_the_round(self):
        comments = [
            {"author": {"login": "me"}, "createdAt": "2026-10-31T08:00:00Z",
             "body": "<!-- gatekeeper-loop: round=1 verdict=changes-requested head=sha1 -->",
             "url": "http://example.com/c1"},
            {"author": {"login": "me"}, "createdAt": "2026-10-31T09:00:00Z",
             "body": "<!-- worker-loop: response round=1 head=sha2 -->",
             "url": "http://example.com/c2"},
        ]
        state = pr_status.gatekeeper_state(comments, "me", "sha1")
        self.assertFalse(state["awaitingWorker"])


class QaStateTest(unittest.TestCase):
    """Issue #69: the worker answers a qa changes-requested like a gatekeeper one."""

    @staticmethod
    def comment(body: str, created: str) -> dict:
        return {"author": {"login": "me"}, "createdAt": created, "body": body,
                "url": f"http://example.com/{created}"}

    def qa_verdict(self, created: str = "2026-10-31T08:00:00Z", round_: str = "1", head: str = "sha1") -> dict:
        return self.comment(
            f"<!-- qa-loop: kind=verdict round={round_} verdict=changes-requested head={head} -->",
            created,
        )

    def action(self, qa):
        return pr_status.select_next_action(
            retarget=False, conflicting=False, duplicates=[], gatekeeper=None,
            qa=qa, has_comments=False, failing=[], pending=[],
        )

    def test_changes_requested_at_head_yields_address_qa(self):
        state = pr_status.qa_state([self.qa_verdict()], "me", "sha1")
        self.assertEqual(state["verdict"], "changes-requested")
        self.assertTrue(state["awaitingWorker"])
        self.assertEqual(self.action(state), "address-qa")

    def test_qa_response_settles_the_round(self):
        comments = [
            self.qa_verdict(),
            self.comment("<!-- worker-loop: qa-response round=1 head=sha2 -->", "2026-10-31T09:00:00Z"),
        ]
        state = pr_status.qa_state(comments, "me", "sha1")
        self.assertFalse(state["awaitingWorker"])
        self.assertEqual(self.action(state), "none")

    def test_gatekeeper_response_does_not_settle_qa_round(self):
        comments = [
            self.qa_verdict(),
            self.comment("<!-- worker-loop: response round=1 head=sha2 -->", "2026-10-31T09:00:00Z"),
        ]
        self.assertTrue(pr_status.qa_state(comments, "me", "sha1")["awaitingWorker"])

    def test_verdict_for_older_head_does_not_wait(self):
        self.assertFalse(pr_status.qa_state([self.qa_verdict(head="old-sha")], "me", "sha1")["awaitingWorker"])

    def test_needs_owner_marker_after_verdict_keeps_it_visible(self):
        comments = [
            self.qa_verdict(),
            self.comment("<!-- qa-loop: kind=needs-owner reason=tool-blocked head=sha1 -->", "2026-10-31T09:00:00Z"),
        ]
        self.assertTrue(pr_status.qa_state(comments, "me", "sha1")["awaitingWorker"])

    def test_gatekeeper_answered_first_when_both_await(self):
        comments = [
            self.qa_verdict(),
            self.comment("<!-- gatekeeper-loop: round=1 verdict=changes-requested head=sha1 -->", "2026-10-31T08:30:00Z"),
        ]
        qa = pr_status.qa_state(comments, "me", "sha1")
        gatekeeper = pr_status.gatekeeper_state(comments, "me", "sha1")
        self.assertTrue(qa["awaitingWorker"])
        self.assertTrue(gatekeeper["awaitingWorker"])
        action = pr_status.select_next_action(
            retarget=False, conflicting=False, duplicates=[], gatekeeper=gatekeeper,
            qa=qa, has_comments=False, failing=[], pending=[],
        )
        self.assertEqual(action, "address-gatekeeper")


class PendingPrCommentAckTest(unittest.TestCase):
    """Issue #129: owner PR comments stay pending across rebases until acknowledged by id/URL."""

    def _summarize(self, comments, *, last_commit_at="2026-11-28T10:00:00Z"):
        import json
        from unittest import mock

        pr_payload = {
            "number": 96,
            "title": "feat: example",
            "body": "Summary",
            "url": "https://example.invalid/pr/96",
            "headRefName": "feat/example-96",
            "headRefOid": "sha2",
            "baseRefName": "main",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "reviewDecision": "",
            "statusCheckRollup": [],
            "commits": [{"committedDate": last_commit_at}],
            "comments": comments,
            "labels": [],
        }

        def fake_gh(*args, **kwargs):
            if args and args[0] == "api":
                return json.dumps({"data": {}})
            return json.dumps(pr_payload)

        with mock.patch.object(pr_status, "gh", side_effect=fake_gh), \
             mock.patch.object(pr_status, "duplicate_commits", return_value=[]), \
             mock.patch.object(pr_status, "parent_pr_merged", return_value=None):
            return pr_status.summarize(96, "owner", "repo", "me", default_base="main", merged_prs=[])

    def _comment(self, cid, body, created_at, author="owner"):
        return {
            "id": str(cid),
            "author": {"login": author},
            "createdAt": created_at,
            "body": body,
            "url": f"https://example.invalid/pr/96#issuecomment-{cid}",
        }

    def test_rebase_push_does_not_clear_owner_comment(self):
        # Owner comment posted before a rebase push (last_commit_at is newer) stays pending.
        summary = self._summarize(
            [self._comment(1001, "Please fix edge case X.", "2026-11-28T09:00:00Z")],
            last_commit_at="2026-11-28T10:00:00Z",
        )
        self.assertEqual(len(summary["newComments"]), 1)
        self.assertEqual(summary["newComments"][0]["id"], "1001")
        self.assertEqual(summary["nextAction"], "triage-comments")

    def test_worker_marker_referencing_comment_id_clears_it(self):
        summary = self._summarize([
            self._comment(1001, "Please fix edge case X.", "2026-11-28T09:00:00Z"),
            self._comment(
                1002,
                "<!-- worker-loop: response acks=1001 -->\nAddressed comment 1001.",
                "2026-11-28T09:05:00Z",
                author="me",
            ),
        ])
        self.assertEqual(summary["newComments"], [])
        self.assertEqual(summary["nextAction"], "none")

    def test_worker_marker_without_reference_does_not_clear_comment(self):
        summary = self._summarize([
            self._comment(1001, "Please fix edge case X.", "2026-11-28T09:00:00Z"),
            self._comment(
                1002,
                "<!-- worker-loop: response round=1 head=sha2 -->\nAddressed gatekeeper round 1.",
                "2026-11-28T09:05:00Z",
                author="me",
            ),
        ])
        self.assertEqual(len(summary["newComments"]), 1)
        self.assertEqual(summary["nextAction"], "triage-comments")

    def test_second_owner_comment_after_marker_is_pending(self):
        summary = self._summarize([
            self._comment(1001, "First ask.", "2026-11-28T09:00:00Z"),
            self._comment(
                1002,
                "<!-- worker-loop: note acks=1001 -->\nHandled 1001.",
                "2026-11-28T09:05:00Z",
                author="me",
            ),
            self._comment(1003, "Second ask.", "2026-11-28T09:10:00Z"),
        ])
        self.assertEqual([c["id"] for c in summary["newComments"]], ["1003"])
        self.assertEqual(summary["nextAction"], "triage-comments")

    def test_pr_status_does_not_read_committed_date(self):
        src = Path(pr_status.__file__).read_text(encoding="utf-8")
        self.assertNotIn("committedDate", src)

    def test_worker_skill_documents_comment_ack_reference_and_reply_from_new_comments(self):
        # G1.1: SKILL.md tells the worker to include the id/url from newComments in its reply.
        skill_path = Path(__file__).resolve().parent.parent / "skills" / "worker-loop" / "SKILL.md"
        text = skill_path.read_text(encoding="utf-8")
        self.assertIn("newComments", text)
        self.assertIn("acks=", text)

        pending = self._summarize([
            self._comment(2001, "Please clarify X.", "2026-11-28T09:00:00Z"),
        ])
        self.assertEqual(pending["nextAction"], "triage-comments")
        ack_id = pending["newComments"][0]["id"]
        reply = self._comment(
            2002,
            f"<!-- worker-loop: response acks={ack_id} -->\nClarified X.",
            "2026-11-28T09:05:00Z",
            author="me",
        )
        cleared = self._summarize([
            self._comment(2001, "Please clarify X.", "2026-11-28T09:00:00Z"),
            reply,
        ])
        self.assertEqual(cleared["newComments"], [])
        self.assertEqual(cleared["nextAction"], "none")


class WorkerRebaseNoteAndRecheckTest(unittest.TestCase):
    """Issue #132: rebase actions post a note marker and re-check pr_status in the same tick."""

    def _summarize(self, *, head="sha2", mergeable="MERGEABLE", comments=None):
        import json
        from unittest import mock

        pr_payload = {
            "number": 96,
            "title": "feat: example",
            "body": "Summary",
            "url": "https://example.invalid/pr/96",
            "headRefName": "feat/example-96",
            "headRefOid": head,
            "baseRefName": "main",
            "isDraft": False,
            "mergeable": mergeable,
            "mergeStateStatus": "CLEAN" if mergeable == "MERGEABLE" else "DIRTY",
            "reviewDecision": "",
            "statusCheckRollup": [],
            "commits": [{"committedDate": "2026-11-28T10:00:00Z"}],
            "comments": comments or [],
            "labels": [],
        }

        def fake_gh(*args, **kwargs):
            if args and args[0] == "api":
                return json.dumps({"data": {}})
            return json.dumps(pr_payload)

        with mock.patch.object(pr_status, "gh", side_effect=fake_gh), \
             mock.patch.object(pr_status, "duplicate_commits", return_value=[]), \
             mock.patch.object(pr_status, "parent_pr_merged", return_value=None):
            return pr_status.summarize(96, "owner", "repo", "me", default_base="main", merged_prs=[])

    def test_conflict_plus_owner_comment_becomes_triage_comments_after_rebase_note(self):
        owner_comment = {
            "id": "5001",
            "author": {"login": "owner"},
            "createdAt": "2026-11-28T09:00:00Z",
            "body": "Please fix edge case Z.",
            "url": "https://example.invalid/pr/96#issuecomment-5001",
        }
        before = self._summarize(head="sha1", mergeable="CONFLICTING", comments=[owner_comment])
        self.assertEqual(before["nextAction"], "resolve-conflict")

        note_body = pr_status.format_rebase_note("resolve-conflict", "sha1", "sha2", "main")
        note_comment = {
            "id": "5002",
            "author": {"login": "me"},
            "createdAt": "2026-11-28T09:05:00Z",
            "body": note_body,
            "url": "https://example.invalid/pr/96#issuecomment-5002",
        }
        after = self._summarize(head="sha2", mergeable="MERGEABLE", comments=[owner_comment, note_comment])
        self.assertEqual(after["nextAction"], "triage-comments")
        self.assertEqual([c["id"] for c in after["newComments"]], ["5001"])

    def test_conflict_plus_changes_requested_at_old_head_requeues_gatekeeper_on_new_head(self):
        from devloops_lib import gatekeeper_queue

        verdict = {
            "id": "6001",
            "author": {"login": "me"},
            "createdAt": "2026-11-28T09:00:00Z",
            "body": "<!-- gatekeeper-loop: round=1 verdict=changes-requested head=sha1 base=main -->\nFix G1.1.",
            "url": "https://example.invalid/pr/96#issuecomment-6001",
        }
        note_body = pr_status.format_rebase_note("resolve-conflict", "sha1", "sha2", "main")
        note_comment = {
            "id": "6002",
            "author": {"login": "me"},
            "createdAt": "2026-11-28T09:05:00Z",
            "body": note_body,
            "url": "https://example.invalid/pr/96#issuecomment-6002",
        }
        status, _, _ = gatekeeper_queue.classify(
            mergeable="MERGEABLE",
            head="sha2",
            base="main",
            last_verdict={"verdict": "changes-requested", "head": "sha1", "base": "main", "round": 1},
            worker_response=None,
            total_verdicts=1,
            state_reviewed_head="sha1",
            state_pending_ci=False,
            has_open_findings=True,
            pending_checks=[],
            failing_checks=[],
            reviewed_base="main",
            rebased=True,
        )
        self.assertEqual(status, "review")
        after = self._summarize(head="sha2", mergeable="MERGEABLE", comments=[verdict, note_comment])
        self.assertEqual(after["nextAction"], "none")

    def test_resolve_conflict_alone_posts_note_with_both_heads_and_ends_none(self):
        from devloops_lib import contract

        note_body = pr_status.format_rebase_note("resolve-conflict", "aaaa111", "bbbb222", "main")
        parsed = contract.parse_marker(note_body)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[0], "worker-loop")
        self.assertEqual(parsed[1]["kind"], "note")
        self.assertEqual(parsed[1]["oldHead"], "aaaa111")
        self.assertEqual(parsed[1]["newHead"], "bbbb222")
        self.assertEqual(parsed[1]["base"], "main")
        after = self._summarize(
            head="bbbb222",
            mergeable="MERGEABLE",
            comments=[{"id": "7001", "author": {"login": "me"}, "createdAt": "2026-11-28T09:05:00Z", "body": note_body}],
        )
        self.assertEqual(after["nextAction"], "none")

    def test_worker_skill_names_note_and_recheck_for_rebase_actions(self):
        skill_path = Path(__file__).resolve().parent.parent / "skills" / "worker-loop" / "SKILL.md"
        text = skill_path.read_text(encoding="utf-8")
        for action in ("retarget", "resolve-conflict", "restack"):
            self.assertIn(f"action={action}", text)
        self.assertIn("pr_status.py --pr N", text)


class SummarizeLabeledPrTest(unittest.TestCase):
    """Issue #131: pr_status.summarize must not overwrite repo `name` with a label name."""

    def _run_summarize(self, labels):
        import json
        from unittest import mock

        pr_payload = {
            "number": 55,
            "title": "feat: labeled pr",
            "body": "Summary",
            "url": "https://example.invalid/pr/55",
            "headRefName": "feat/labeled-55",
            "headRefOid": "sha1",
            "baseRefName": "main",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "reviewDecision": "",
            "statusCheckRollup": [],
            "commits": [{"committedDate": "2026-11-28T00:00:00Z"}],
            "comments": [],
            "labels": labels,
        }
        graphql_calls = []

        def fake_gh(*args, **kwargs):
            if args and args[0] == "api" and "graphql" in args:
                graphql_calls.append(args)
                if "name=my-repo" not in args:
                    raise AssertionError(f"expected name=my-repo in graphql args, got {args!r}")
                return json.dumps({"data": {}})
            return json.dumps(pr_payload)

        with mock.patch.object(pr_status, "gh", side_effect=fake_gh), \
             mock.patch.object(pr_status, "duplicate_commits", return_value=[]), \
             mock.patch.object(pr_status, "parent_pr_merged", return_value=None):
            summary = pr_status.summarize(55, "my-owner", "my-repo", "me", default_base="main", merged_prs=[])
        self.assertEqual(len(graphql_calls), 1)
        return summary

    def test_pr_with_two_labels_passes_repo_name_to_graphql(self):
        summary = self._run_summarize([
            {"name": "gatekeeper:changes-requested"},
            {"name": "enhancement"},
        ])
        self.assertEqual(summary["labels"], ["enhancement", "gatekeeper:changes-requested"])
        self.assertEqual(summary["nextAction"], "none")

    def test_pr_with_no_labels_passes_repo_name_to_graphql(self):
        summary = self._run_summarize([])
        self.assertEqual(summary["labels"], [])
        self.assertEqual(summary["nextAction"], "none")


if __name__ == "__main__":
    unittest.main()
