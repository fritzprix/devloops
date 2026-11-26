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


if __name__ == "__main__":
    unittest.main()
