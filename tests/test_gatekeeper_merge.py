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
    base: str = "main",
    gk_head: str = "shaX",
    gk_base: str = "main",
    qa_head: str | None = "shaX",
    qa_base: str = "main",
    labels: list[str] | None = None,
    checks: list[dict] | None = None,
    is_draft: bool = False,
    mergeable: str = "MERGEABLE",
) -> dict:
    if labels is None:
        labels = ["gatekeeper:approved", "qa:accepted"]
    if checks is None:
        checks = [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS", "headSha": head}]
    gk_base_tok = f" base={gk_base}" if gk_base else ""
    qa_base_tok = f" base={qa_base}" if qa_base else ""
    comments = [
        {
            "author": {"login": "me"},
            "createdAt": "2026-11-28T01:00:00Z",
            "body": f"<!-- gatekeeper-loop: round=1 verdict=approve head={gk_head}{gk_base_tok} -->\nLGTM",
        },
    ]
    if qa_head is not None:
        comments.append({
            "author": {"login": "me"},
            "createdAt": "2026-11-28T01:05:00Z",
            "body": f"<!-- qa-loop: kind=verdict round=1 verdict=accepted head={qa_head}{qa_base_tok} -->\nQA pass",
        })
    return {
        "number": 42,
        "headRefOid": head,
        "baseRefName": base,
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
            ("stacked PR targeting parent branch", _mergeable_pr(base="feat/parent", gk_base="feat/parent", qa_base="feat/parent")),
            ("retargeted PR with old approve base", _mergeable_pr(base="main", gk_base="feat/parent", qa_base="main")),
            ("retargeted PR with old qa base", _mergeable_pr(base="main", gk_base="main", qa_base="feat/parent")),
            ("gatekeeper approve marker without base=", _mergeable_pr(gk_base=None)),
            ("qa accepted marker without base=", _mergeable_pr(qa_base=None)),
        ]
        for label, pr in cases:
            with self.subTest(case=label):
                forge = FakeMergeForge(current_head="shaX")
                result = gatekeeper_queue.try_merge_pr(pr, "me", forge, config=cfg)
                self.assertFalse(result["merged"], msg=f"unexpected merge for {label}")
                self.assertEqual(forge.calls, [], msg=f"merge_pr called for {label}")

    def test_no_merge_without_green_ci_when_repo_has_ci_and_no_required_checks(self):
        # G1.2: even when gatekeeper.required_checks is empty, a repo with CI must not
        # merge a head with no checks or only skipped checks.
        cfg = {"project": {"base_branch": "main"}, "gatekeeper": {}}
        for label, checks in [
            ("no checks at all", []),
            ("only skipped check", [{"name": "ci", "status": "COMPLETED", "conclusion": "SKIPPED", "headSha": "shaX"}]),
        ]:
            with self.subTest(case=label):
                forge = FakeMergeForge(current_head="shaX")
                result = gatekeeper_queue.try_merge_pr(_mergeable_pr(checks=checks), "me", forge, config=cfg, repo_has_ci=True)
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
        pattern = re.compile(
            r"human merges|ready for a human merge|human user's call|"
            r"human always merges|agents never merge|no loop merges|merge stays with a human|"
            r"always human|never merging|merge itself stays with a human|"
            r"human[\s─►\-]{2,}merge",
            re.IGNORECASE,
        )
        hits: list[str] = []
        targets = (
            list(sorted((REPO_ROOT / "skills").rglob("*.md")))
            + [REPO_ROOT / "README.md", REPO_ROOT / "devloops_lib" / "pm_induction.py"]
        )
        for path in targets:
            text = path.read_text(encoding="utf-8")
            if pattern.search(text):
                hits.append(path.relative_to(REPO_ROOT).as_posix())
        self.assertEqual(hits, [])

    def test_pm_induction_does_not_reserve_merge_for_humans(self):
        text = (REPO_ROOT / "devloops_lib" / "pm_induction.py").read_text(encoding="utf-8")
        self.assertNotIn("humanOnly", text)
        self.assertIn("neverPm", text)


if __name__ == "__main__":
    unittest.main()
