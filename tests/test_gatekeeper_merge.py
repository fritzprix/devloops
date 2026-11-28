#!/usr/bin/env python3
"""Tests for gatekeeper PR merge (issue #109)."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from devloops_lib import gatekeeper_queue
from devloops_lib.forge.base import ForgeError

REPO_ROOT = Path(__file__).resolve().parent.parent


class FakeMergeForge:
    """Minimal fake forge recording merge_pr calls and enforcing expected_head_sha."""

    def __init__(self, current_head: str):
        self.current_head = current_head
        self.calls: list[tuple[int, str]] = []

    def merge_pr(self, number: int, expected_head_sha: str) -> None:
        self.calls.append((number, expected_head_sha))
        if expected_head_sha != self.current_head:
            raise ForgeError(
                f"head mismatch on PR #{number}: expected {expected_head_sha}, actual {self.current_head}"
            )


def _mergeable_pr(
    *,
    head: str = "shaX",
    gk_head: str = "shaX",
    qa_head: str | None = "shaX",
    labels: list[str] | None = None,
    checks: list[dict] | None = None,
    is_draft: bool = False,
    mergeable: str = "MERGEABLE",
) -> dict:
    if labels is None:
        labels = ["gatekeeper:approved", "qa:accepted"]
    if checks is None:
        checks = [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS", "headSha": head}]
    comments = [
        {
            "author": {"login": "me"},
            "createdAt": "2026-11-28T01:00:00Z",
            "body": f"<!-- gatekeeper-loop: round=1 verdict=approve head={gk_head} base=main -->\nLGTM",
        },
    ]
    if qa_head is not None:
        comments.append({
            "author": {"login": "me"},
            "createdAt": "2026-11-28T01:05:00Z",
            "body": f"<!-- qa-loop: round=1 verdict=accepted head={qa_head} base=main -->\nQA pass",
        })
    return {
        "number": 42,
        "headRefOid": head,
        "isDraft": is_draft,
        "mergeable": mergeable,
        "labels": [{"name": name} for name in labels],
        "comments": comments,
        "statusCheckRollup": checks,
    }


class GatekeeperMergeTest(unittest.TestCase):
    """Issue #109 acceptance criteria for gatekeeper-driven merge."""

    def test_merge_ready_pr_merges_once_with_expected_head(self):
        cfg = {"gatekeeper": {"required_checks": ["ci"]}}
        pr = _mergeable_pr(head="shaX")
        forge = FakeMergeForge(current_head="shaX")

        result = gatekeeper_queue.try_merge_pr(pr, "me", forge, config=cfg)

        self.assertTrue(result["merged"])
        self.assertEqual(result["headSha"], "shaX")
        self.assertEqual(forge.calls, [(42, "shaX")])

    def test_head_change_between_check_and_merge_refuses_and_requeues(self):
        cfg = {"gatekeeper": {"required_checks": ["ci"]}}
        pr = _mergeable_pr(head="shaX")
        # Head moved to shaY after the PR snapshot was read.
        forge = FakeMergeForge(current_head="shaY")

        result = gatekeeper_queue.try_merge_pr(pr, "me", forge, config=cfg)

        self.assertFalse(result["merged"])
        self.assertEqual(result["reason"], "merge-failed")
        self.assertEqual(forge.calls, [(42, "shaX")])
        # On the next tick at shaY, gatekeeper_queue.classify puts the PR back in the review queue.
        status, _, _ = gatekeeper_queue.classify(
            mergeable="MERGEABLE",
            head="shaY",
            base="main",
            last_verdict={"verdict": "approve", "head": "shaX", "base": "main", "round": 1},
            worker_response=None,
            total_verdicts=1,
            state_reviewed_head="shaX",
            state_pending_ci=False,
            has_open_findings=False,
            pending_checks=[],
            failing_checks=[],
            reviewed_base="main",
            rebased=False,
        )
        self.assertEqual(status, "review")

    def test_no_merge_when_any_condition_is_unmet(self):
        cfg = {"gatekeeper": {"required_checks": ["ci"]}}
        cases = [
            ("approve for older head", _mergeable_pr(gk_head="old-sha")),
            ("qa verdict missing", _mergeable_pr(qa_head=None)),
            ("qa verdict for older head", _mergeable_pr(qa_head="old-sha")),
            ("required check pending", _mergeable_pr(checks=[{"name": "ci", "status": "IN_PROGRESS", "conclusion": None, "headSha": "shaX"}])),
            ("required check failed", _mergeable_pr(checks=[{"name": "ci", "status": "COMPLETED", "conclusion": "FAILURE", "headSha": "shaX"}])),
            ("required check skipped", _mergeable_pr(checks=[{"name": "ci", "status": "COMPLETED", "conclusion": "SKIPPED", "headSha": "shaX"}])),
            ("required check absent", _mergeable_pr(checks=[])),
            ("PR is draft", _mergeable_pr(is_draft=True)),
            ("PR has conflict", _mergeable_pr(mergeable="CONFLICTING")),
            ("PR has blocked:owner", _mergeable_pr(labels=["gatekeeper:approved", "qa:accepted", "blocked:owner"])),
        ]
        for label, pr in cases:
            with self.subTest(case=label):
                forge = FakeMergeForge(current_head="shaX")
                result = gatekeeper_queue.try_merge_pr(pr, "me", forge, config=cfg)
                self.assertFalse(result["merged"], msg=f"unexpected merge for {label}")
                self.assertEqual(forge.calls, [], msg=f"merge_pr called for {label}")

    def test_only_gatekeeper_calls_merge_pr(self):
        callers: list[str] = []
        for path in sorted((REPO_ROOT / "devloops_lib").rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel.startswith("devloops_lib/forge/"):
                continue
            text = path.read_text(encoding="utf-8")
            if re.search(r"\.merge_pr\s*\(", text):
                callers.append(rel)
        self.assertEqual(callers, ["devloops_lib/gatekeeper_queue.py"])

    def test_no_skill_text_says_human_merges(self):
        pattern = re.compile(r"human merges|ready for a human merge|human user's call", re.IGNORECASE)
        hits: list[str] = []
        for path in sorted((REPO_ROOT / "skills").rglob("*.md")):
            text = path.read_text(encoding="utf-8")
            if pattern.search(text):
                hits.append(path.relative_to(REPO_ROOT).as_posix())
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
