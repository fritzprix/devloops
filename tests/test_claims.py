#!/usr/bin/env python3
"""Tests for per-item PR and issue claims."""
import concurrent.futures
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloops_lib import claims, tick_gate


def git(*args, cwd):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout.strip()


class PrClaimTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "claims"

    def test_two_agents_claim_one_pr_and_one_wins(self):
        self.assertTrue(claims.claim_pr(self.dir, 7, "a"))
        self.assertFalse(claims.claim_pr(self.dir, 7, "b"))
        self.assertEqual(json.loads((self.dir / "pr-7").read_text())["agent"], "a")

    def test_concurrent_agents_have_exactly_one_winner(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda agent: claims.claim_pr(self.dir, 7, agent), "abcd"))
        self.assertEqual(results.count(True), 1)

    def test_owner_can_reclaim_and_other_pr_is_free(self):
        self.assertTrue(claims.claim_pr(self.dir, 7, "a"))
        self.assertTrue(claims.claim_pr(self.dir, 7, "a"))
        self.assertTrue(claims.claim_pr(self.dir, 8, "b"))

    def test_only_the_owner_can_release(self):
        claims.claim_pr(self.dir, 7, "a")
        self.assertFalse(claims.release_pr(self.dir, 7, "b"))
        self.assertTrue((self.dir / "pr-7").exists())
        self.assertTrue(claims.release_pr(self.dir, 7, "a"))
        self.assertFalse((self.dir / "pr-7").exists())
        self.assertTrue(claims.claim_pr(self.dir, 7, "b"))

    def test_expired_claim_is_reclaimed(self):
        self.dir.mkdir(parents=True)
        (self.dir / "pr-7").write_text(json.dumps(
            {"agent": "a", "token": "t", "time": time.time() - (tick_gate.LOCK_TTL_SECONDS + 10)}
        ), encoding="utf-8")
        self.assertTrue(claims.claim_pr(self.dir, 7, "b"))
        self.assertEqual(json.loads((self.dir / "pr-7").read_text())["agent"], "b")


class IssueClaimTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.remote = root / "remote.git"
        git("init", "--bare", str(self.remote), cwd=root)
        seed = root / "seed"
        git("clone", str(self.remote), str(seed), cwd=root)
        (seed / "file.txt").write_text("base\n", encoding="utf-8")
        git("add", "file.txt", cwd=seed)
        git("commit", "-m", "base", cwd=seed)
        git("push", "origin", "HEAD:refs/heads/main", cwd=seed)
        self.a = root / "a"
        self.b = root / "b"
        for clone in (self.a, self.b):
            git("clone", "--branch", "main", str(self.remote), str(clone), cwd=root)

    def test_two_agents_from_the_same_base_commit_have_one_winner(self):
        # Both clones are at the same commit. Pushing that commit itself would
        # succeed twice, as "up to date"; the claim must reject the second push.
        self.assertEqual(git("rev-parse", "origin/main", cwd=self.a), git("rev-parse", "origin/main", cwd=self.b))
        self.assertTrue(claims.claim_issue(self.a, 5, "origin/main", "a"))
        self.assertFalse(claims.claim_issue(self.b, 5, "origin/main", "b"))
        tip = git("rev-parse", "refs/heads/wip/issue-5", cwd=self.remote)
        self.assertIn("Devloops-Claim-Agent: a", git("log", "-1", "--format=%B", tip, cwd=self.remote))

    def test_owner_can_claim_again_and_other_issues_are_free(self):
        self.assertTrue(claims.claim_issue(self.a, 5, "origin/main", "a"))
        self.assertTrue(claims.claim_issue(self.a, 5, "origin/main", "a"))
        self.assertTrue(claims.claim_issue(self.b, 6, "origin/main", "b"))

    def test_claim_does_not_change_the_worktree_or_base(self):
        head = git("rev-parse", "HEAD", cwd=self.a)
        claims.claim_issue(self.a, 5, "origin/main", "a")
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.a), head)
        self.assertEqual(git("status", "--porcelain", cwd=self.a), "")
        self.assertEqual(git("rev-parse", "refs/heads/main", cwd=self.remote), head)

    def test_unreachable_remote_is_an_error_not_busy(self):
        git("remote", "set-url", "origin", str(self.remote.parent / "missing.git"), cwd=self.b)
        with self.assertRaises(claims.ClaimError):
            claims.claim_issue(self.b, 5, "origin/main", "b")

    def _run_issue(self, repo, agent, open_work):
        out = io.StringIO()
        with patch.object(claims, "_fetch_open_work", return_value=open_work), \
                patch.dict(os.environ, {"LOOP_STATE_DIR": self._tmp.name}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = claims.main(["issue", "5", "--base", "origin/main", "--repo", str(repo), "--agent-id", agent])
        return code, out.getvalue()

    def test_cli_exit_codes(self):
        self.assertEqual(self._run_issue(self.a, "a", 1)[0], tick_gate.EXIT_RUN)
        self.assertEqual(self._run_issue(self.b, "b", 1)[0], tick_gate.EXIT_BUSY)
        # Over the limit: the owner must not start, and the branch stays.
        code, text = self._run_issue(self.a, "a", 6)
        self.assertEqual(code, tick_gate.EXIT_NOOP)
        self.assertIn("OVER LIMIT", text)
        self.assertTrue(git("rev-parse", "refs/heads/wip/issue-5", cwd=self.remote))
        git("remote", "set-url", "origin", str(self.remote.parent / "missing.git"), cwd=self.b)
        self.assertEqual(self._run_issue(self.b, "b", 1)[0], tick_gate.EXIT_ERROR)


class CountOpenWorkTest(unittest.TestCase):
    def test_counts_unapproved_prs_and_claims_without_a_pr(self):
        prs = [
            {"headRefName": "feat/x-1"},
            {"headRefName": "feat/y", "closingIssuesReferences": [{"number": 2}]},
            {"headRefName": "feat/z-3", "reviewDecision": "APPROVED"},
            {"headRefName": "feat/w-4", "labels": [{"name": claims.APPROVED_LABEL}]},
        ]
        # 1 and 2 are claims that have a PR, 3 and 4 have approved PRs,
        # 5 has no PR, and 9 is a leftover claim on a closed issue.
        count = claims.count_open_work(prs, {1, 2, 3, 4, 5}, {1, 2, 3, 4, 5, 9})
        self.assertEqual(count, 3)


class PrCliTest(unittest.TestCase):
    def test_pr_commands_share_one_claims_dir(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"LOOP_STATE_DIR": tmp}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(claims.main(["pr", "7", "--agent-id", "a"]), tick_gate.EXIT_RUN)
            self.assertEqual(claims.main(["pr", "7", "--agent-id", "b"]), tick_gate.EXIT_BUSY)
            self.assertTrue((Path(tmp) / "claims" / "pr-7").exists())
            self.assertEqual(claims.main(["release-pr", "7", "--agent-id", "a"]), tick_gate.EXIT_RUN)
            self.assertFalse((Path(tmp) / "claims" / "pr-7").exists())


if __name__ == "__main__":
    unittest.main()
