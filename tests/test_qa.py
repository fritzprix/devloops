"""Tests for devloops_lib.qa: qa verdict status, two-party sign-off, merged-PR tracking."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import sys
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloops_lib import qa  # noqa: E402
from devloops_lib.forge.models import Blocker  # noqa: E402

ME = "bot"
HEAD = "a" * 40
OLD = "b" * 40


def c(body: str, at: str, author: str = ME, assoc: str = "OWNER") -> dict:
    return {"author": {"login": author}, "body": body, "createdAt": at, "authorAssociation": assoc}


def qa_marker(verdict: str, head: str = HEAD, rnd: int = 1) -> str:
    return f"<!-- qa-loop: kind=verdict round={rnd} verdict={verdict} head={head} -->\n## QA"


def gk_marker(verdict: str, head: str = HEAD, rnd: int = 1) -> str:
    return f"<!-- gatekeeper-loop: round={rnd} verdict={verdict} head={head} base=main -->\n## GK"


def pr(comments: list[dict], labels: tuple[str, ...] = (), head: str = HEAD, **extra) -> dict:
    out = {"number": 7, "headRefOid": head, "comments": comments,
           "labels": [{"name": n} for n in labels], "isDraft": False, "mergeable": "MERGEABLE"}
    out.update(extra)
    return out


class ClassifyTest(unittest.TestCase):
    def test_never_reviewed(self):
        self.assertEqual(qa.classify_pr(pr([]), ME)["status"], "review")

    def test_accepted_at_head(self):
        r = qa.classify_pr(pr([c(qa_marker("accepted"), "2026-01-01T00:00:00Z")], ("qa:accepted",)), ME)
        self.assertEqual(r["status"], "accepted")
        self.assertEqual(r["staleLabels"], [])
        self.assertIsNone(r["missingLabel"])

    def test_new_head_needs_review_and_label_is_stale(self):
        r = qa.classify_pr(pr([c(qa_marker("accepted", OLD), "2026-01-01T00:00:00Z")], ("qa:accepted",)), ME)
        self.assertEqual(r["status"], "review")
        self.assertEqual(r["staleLabels"], ["qa:accepted"])

    def test_changes_requested_waits_for_worker(self):
        r = qa.classify_pr(pr([c(qa_marker("changes-requested"), "2026-01-01T00:00:00Z")]), ME)
        self.assertEqual(r["status"], "wait-worker")
        self.assertEqual(r["missingLabel"], "qa:changes-requested")

    def test_worker_qa_response_triggers_review(self):
        comments = [c(qa_marker("changes-requested"), "2026-01-01T00:00:00Z"),
                    c(f"<!-- worker-loop: qa-response round=1 head={HEAD} -->", "2026-01-01T01:00:00Z")]
        self.assertEqual(qa.classify_pr(pr(comments), ME)["status"], "review")

    def test_gatekeeper_response_does_not_count_as_qa_response(self):
        comments = [c(qa_marker("changes-requested"), "2026-01-01T00:00:00Z"),
                    c(f"<!-- worker-loop: response round=1 head={HEAD} -->", "2026-01-01T01:00:00Z")]
        self.assertEqual(qa.classify_pr(pr(comments), ME)["status"], "wait-worker")

    def test_owner_comment_reopens_needs_human(self):
        comments = [c(qa_marker("needs-human"), "2026-01-01T00:00:00Z"),
                    c("accept without test", "2026-01-01T02:00:00Z", author="owner")]
        self.assertEqual(qa.classify_pr(pr(comments), ME)["status"], "review")

    def test_markers_from_other_authors_are_ignored(self):
        r = qa.classify_pr(pr([c(qa_marker("accepted"), "2026-01-01T00:00:00Z", author="x", assoc="NONE")]), ME)
        self.assertEqual(r["status"], "review")


class OwnerCommentsTest(unittest.TestCase):
    def test_loop_marker_comments_are_never_owner_comments(self):
        # Issue #65 AC: a qa-loop (or gatekeeper/worker) marker comment is never
        # an owner comment for any loop, from any author.
        comments = [
            c(qa_marker("accepted"), "2026-01-01T00:00:00Z"),
            c("<!-- gatekeeper-loop: kind=needs-owner reason=x -->", "2026-01-02T00:00:00Z", author="owner"),
            c("<!-- worker-loop: note round=1 -->", "2026-01-02T00:00:00Z"),
            c("<!-- qa-loop: kind=post-merge pr=7 build=x check=y -->", "2026-01-02T00:00:00Z"),
            c("accept without test", "2026-01-02T00:00:00Z", author="owner"),
        ]
        found = qa.owner_comments_after(comments, ME, "2026-01-01T12:00:00Z")
        self.assertEqual([f["author"] for f in found], ["owner"])
        self.assertEqual(len(found), 1)


class SignoffTest(unittest.TestCase):
    def both(self, qa_head=HEAD, gk_head=HEAD, labels=("gatekeeper:approved", "qa:accepted"), **extra):
        comments = [c(gk_marker("approve", gk_head), "2026-01-01T00:00:00Z"),
                    c(qa_marker("accepted", qa_head), "2026-01-01T00:10:00Z")]
        return qa.signoff(pr(comments, labels, **extra), ME)

    def test_both_at_head_is_merge_ready(self):
        r = self.both()
        self.assertTrue(r["mergeReady"], r["blockers"])

    def test_gatekeeper_only_is_blocked(self):
        r = self.both(labels=("gatekeeper:approved",))
        self.assertFalse(r["mergeReady"])
        self.assertIn("qa: label qa:accepted missing", r["blockers"])

    def test_old_qa_head_is_blocked_even_with_label(self):
        r = self.both(qa_head=OLD)
        self.assertIn("qa: accepted is for an older head", r["blockers"])

    def test_old_gatekeeper_head_is_blocked(self):
        r = self.both(gk_head=OLD)
        self.assertIn("gatekeeper: approve is for an older head", r["blockers"])

    def test_clean_marker_is_not_an_approve(self):
        comments = [c(f"<!-- gatekeeper-loop: verdict=clean head={HEAD} base=main -->", "2026-01-01T00:00:00Z"),
                    c(qa_marker("accepted"), "2026-01-01T00:10:00Z")]
        r = qa.signoff(pr(comments, ("gatekeeper:approved", "qa:accepted")), ME)
        self.assertIn("gatekeeper: latest verdict is not approve", r["blockers"])

    def test_draft_and_conflict_block(self):
        r = self.both(isDraft=True, mergeable="CONFLICTING")
        self.assertIn("PR is a draft", r["blockers"])
        self.assertIn("PR has merge conflicts", r["blockers"])


class MergedTrackingTest(unittest.TestCase):
    merged = [
        {"number": 1, "mergedAt": "2026-01-01T00:00:00Z", "mergeCommit": {"oid": "c1"}},
        {"number": 2, "mergedAt": "2026-01-02T00:00:00Z", "mergeCommit": {"oid": "c2"}},
    ]

    def test_first_run_sets_cursor_and_queues_nothing(self):
        s = qa.advance_merged({}, self.merged)
        self.assertEqual(s["mergedCursor"], "2026-01-02T00:00:00Z")
        self.assertEqual(s["pendingTests"], {})

    def test_first_run_without_merges_uses_now(self):
        s = qa.advance_merged({}, [], now=lambda: "2026-05-05T00:00:00Z")
        self.assertEqual(s["mergedCursor"], "2026-05-05T00:00:00Z")

    def test_queues_only_after_cursor(self):
        s = qa.advance_merged({"mergedCursor": "2026-01-01T00:00:00Z"}, self.merged)
        self.assertEqual(list(s["pendingTests"]), ["2"])
        self.assertEqual(s["pendingTests"]["2"]["mergeCommit"], "c2")
        self.assertEqual(s["mergedCursor"], "2026-01-02T00:00:00Z")

    def test_since_backfill_and_no_double_queue(self):
        s = qa.advance_merged({"mergedCursor": "2025-12-31"}, self.merged)
        self.assertEqual(sorted(s["pendingTests"]), ["1", "2"])
        s = qa.record_test(s, 1, "pass", "sha", [], now=lambda: "t")
        s["mergedCursor"] = "2025-12-31"
        s = qa.advance_merged(s, self.merged)
        self.assertEqual(sorted(s["pendingTests"]), ["2"])

    def test_record_fail_and_blocked(self):
        s = qa.advance_merged({"mergedCursor": "2025-12-31"}, self.merged)
        s = qa.record_test(s, 1, "fail", "sha", [99], now=lambda: "t")
        self.assertEqual(s["tested"]["1"]["issues"], [99])
        s = qa.record_test(s, 2, "blocked", "sha", [], now=lambda: "t")
        s = qa.record_test(s, 2, "blocked", "sha", [], now=lambda: "t")
        self.assertEqual(s["pendingTests"]["2"]["attempts"], 2)
        with self.assertRaises(ValueError):
            qa.record_test(s, 2, "maybe", "sha", [])

    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "qa-loop" / "state.json"
            self.assertEqual(qa.load_state(p), {})
            qa.save_state(p, {"mergedCursor": "x"})
            self.assertEqual(qa.load_state(p), {"mergedCursor": "x"})


class BlockedByTest(unittest.TestCase):
    """issue #65: dependency linking is routed through the Forge protocol."""

    class FakeDepsForge:
        def __init__(self):
            self.deps = {20: [Blocker(5, "open")]}
            self.added = []

        def blocked_by(self, number):
            return list(self.deps.get(number, []))

        def add_blocker(self, number, blocker_number):
            self.added.append((number, blocker_number))
            self.deps.setdefault(number, []).append(Blocker(blocker_number, "open"))

    def test_link_then_verify(self):
        forge = self.FakeDepsForge()
        self.assertEqual(qa.link_blocked_by(forge, 20, 12), [5, 12])
        self.assertEqual(forge.added, [(20, 12)])

    def test_closed_blockers_are_not_reported(self):
        forge = self.FakeDepsForge()
        forge.deps[20].append(Blocker(9, "closed"))
        self.assertEqual(qa.link_blocked_by(forge, 20, 12), [5, 12])


class ConfigTest(unittest.TestCase):
    def test_string_braces_become_dicts(self):
        cfg = qa.qa_config({"qa": {"tools": "{}", "post_merge": {"build": "make"}}})
        self.assertEqual(cfg["tools"], {})
        self.assertEqual(cfg["post_merge"], {"build": "make"})


if __name__ == "__main__":
    unittest.main()
