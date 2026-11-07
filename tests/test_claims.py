#!/usr/bin/env python3
"""Tests for per-item PR and issue claims (single process; see test_concurrency for races)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import socket
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
from devloops_lib.config import AGENT_ID_ENV, get_lock_path


def git(*args, cwd):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout.strip()


def make_remote(root: Path, *clones: str) -> tuple[Path, list[Path]]:
    """Create a bare remote with one commit on main, and clones of it."""
    remote = root / "remote.git"
    git("init", "--bare", str(remote), cwd=root)
    seed = root / "seed"
    git("clone", str(remote), str(seed), cwd=root)
    (seed / "file.txt").write_text("base\n", encoding="utf-8")
    git("add", "file.txt", cwd=seed)
    git("commit", "-m", "base", cwd=seed)
    git("push", "origin", "HEAD:refs/heads/main", cwd=seed)
    paths = []
    for name in clones:
        git("clone", "--branch", "main", str(remote), str(root / name), cwd=root)
        paths.append(root / name)
    return remote, paths


def push_old_claim(repo: Path, number: int, agent: str, age_seconds: int, branch: str = "",
                   host: str = "other-host") -> None:
    """Push a claim commit whose time trailer is age_seconds in the past."""
    lines = [
        f"devloops claim: issue #{number} active", "",
        f"{claims.OWNER_TRAILER}: {agent}",
        f"{claims.HOST_TRAILER}: {host}",
        f"{claims.STATE_TRAILER}: {claims.ACTIVE}",
        f"{claims.TIME_TRAILER}: {int(time.time()) - age_seconds}",
    ]
    if branch:
        lines.append(f"{claims.BRANCH_TRAILER}: {branch}")
    commit = git("commit-tree", "origin/main^{tree}", "-p", "origin/main", "-m", "\n".join(lines), cwd=repo)
    git("push", "origin", f"{commit}:refs/heads/wip/issue-{number}", cwd=repo)


class PrClaimTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.dir = root / "claims"
        self.lock_a = root / "a" / "tick.lock"
        self.lock_b = root / "b" / "tick.lock"
        self.token_a = tick_gate.acquire_lock_token(self.lock_a)
        self.token_b = tick_gate.acquire_lock_token(self.lock_b)

    def claim(self, number, slot):
        lock, token = (self.lock_a, self.token_a) if slot == "a" else (self.lock_b, self.token_b)
        return claims.claim_pr(self.dir, number, slot, lock, token)

    def test_two_slots_claim_one_pr_and_one_wins(self):
        self.assertTrue(self.claim(7, "a"))
        self.assertFalse(self.claim(7, "b"))
        payload = json.loads((self.dir / "pr-7").read_text())
        self.assertEqual((payload["agent"], payload["ownerToken"]), ("a", self.token_a))
        self.assertIn("agent a", claims.pr_claim_holder(self.dir, 7))

    def test_owner_can_reclaim_and_other_pr_is_free(self):
        self.assertTrue(self.claim(7, "a"))
        self.assertTrue(self.claim(7, "a"))
        self.assertTrue(self.claim(8, "b"))

    def test_claim_of_a_live_tick_never_expires(self):
        self.assertTrue(self.claim(7, "a"))
        path = self.dir / "pr-7"
        payload = json.loads(path.read_text())
        payload["time"] = time.time() - 100 * tick_gate.LOCK_TTL_SECONDS
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertFalse(self.claim(7, "b"))

    def test_claim_frees_as_soon_as_the_owner_tick_releases_its_lock(self):
        self.assertTrue(self.claim(7, "a"))
        tick_gate.release_lock(self.lock_a, token=self.token_a)
        self.assertTrue(self.claim(7, "b"))
        self.assertFalse(claims.owns_pr(self.dir, 7, self.token_a))

    def test_claim_frees_when_the_owner_tick_loses_its_lock(self):
        self.assertTrue(self.claim(7, "a"))
        # Another tick of slot a reclaimed the stale lock: the old token no longer owns it.
        tick_gate.release_lock(self.lock_a, token=self.token_a)
        self.assertIsNotNone(tick_gate.acquire_lock_token(self.lock_a))
        self.assertTrue(self.claim(7, "b"))

    def test_only_the_owner_tick_can_release(self):
        self.claim(7, "a")
        self.assertFalse(claims.release_pr(self.dir, 7, self.token_b))
        self.assertFalse(claims.release_pr(self.dir, 7, ""))
        self.assertTrue((self.dir / "pr-7").exists())
        self.assertTrue(claims.release_pr(self.dir, 7, self.token_a))
        self.assertFalse((self.dir / "pr-7").exists())
        self.assertTrue(self.claim(7, "b"))

    def test_release_tick_claims_releases_only_that_tick(self):
        self.claim(7, "a")
        self.claim(8, "a")
        self.claim(9, "b")
        self.assertEqual(claims.release_tick_claims(self.dir, self.token_a), ["PR #7", "PR #8"])
        self.assertEqual(sorted(p.name for p in self.dir.iterdir() if claims.PR_CLAIM_NAME.fullmatch(p.name)), ["pr-9"])
        self.assertEqual(claims.release_tick_claims(self.dir / "missing", self.token_a), [])

    def test_legacy_claim_without_owner_lock_expires_by_ttl(self):
        self.dir.mkdir(parents=True)
        (self.dir / "pr-7").write_text(json.dumps(
            {"agent": "a", "token": "t", "time": time.time() - (tick_gate.LOCK_TTL_SECONDS + 10)}
        ), encoding="utf-8")
        self.assertTrue(self.claim(7, "b"))
        self.assertEqual(json.loads((self.dir / "pr-7").read_text())["agent"], "b")


class IssueClaimTest(unittest.TestCase):
    TTL = 3600

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.remote, (self.a, self.b) = make_remote(self.root, "a", "b")

    def claim(self, repo, agent, branch=None, ttl=None):
        return claims.claim_issue(repo, 5, "origin/main", agent, self.TTL if ttl is None else ttl, branch)

    def test_two_slots_from_the_same_base_commit_have_one_winner(self):
        # Both clones are at the same commit. Pushing that commit itself would
        # succeed twice, as "up to date"; the claim must reject the second push.
        self.assertEqual(git("rev-parse", "origin/main", cwd=self.a), git("rev-parse", "origin/main", cwd=self.b))
        self.assertEqual(self.claim(self.a, "a")["status"], "claimed")
        busy = self.claim(self.b, "b")
        self.assertEqual((busy["status"], busy["owner"], busy["host"]), ("busy", "a", socket.gethostname()))
        tip = git("rev-parse", "refs/heads/wip/issue-5", cwd=self.remote)
        self.assertIn("Devloops-Claim-Agent: a", git("log", "-1", "--format=%B", tip, cwd=self.remote))

    def test_owner_refreshes_and_keeps_the_recorded_branch(self):
        self.assertEqual(self.claim(self.a, "a", branch="feat/x-5")["status"], "claimed")
        before = git("rev-parse", "refs/heads/wip/issue-5", cwd=self.remote)
        self.assertEqual(self.claim(self.a, "a")["status"], "refreshed")
        self.assertNotEqual(git("rev-parse", "refs/heads/wip/issue-5", cwd=self.remote), before)
        self.assertEqual(claims.read_issue_claim(self.a, 5).branch, "feat/x-5")
        self.assertTrue(claims.refresh_issue_claim(self.a, 5, "a"))
        self.assertFalse(claims.refresh_issue_claim(self.b, 5, "b"))
        self.assertEqual(self.claim(self.b, "b", ttl=self.TTL)["status"], "busy")

    def test_other_issues_are_free(self):
        self.claim(self.a, "a")
        self.assertEqual(claims.claim_issue(self.b, 6, "origin/main", "b", self.TTL)["status"], "claimed")

    def test_release_lets_another_slot_claim_and_continue_the_branch(self):
        self.claim(self.a, "a", branch="feat/x-5")
        self.assertFalse(claims.release_issue(self.b, 5, "b"))
        self.assertTrue(claims.release_issue(self.a, 5, "a"))
        self.assertEqual(claims.read_issue_claim(self.b, 5).state, claims.RELEASED)
        self.assertFalse(claims.release_issue(self.a, 5, "a"))
        result = self.claim(self.b, "b")
        self.assertEqual(result, {"status": "claimed", "previousAgent": "a", "previousBranch": "feat/x-5"})

    def test_stale_claim_is_taken_over_with_the_previous_branch(self):
        push_old_claim(self.a, 5, "gone", age_seconds=self.TTL + 60, branch="feat/old-5")
        result = self.claim(self.b, "b")
        self.assertEqual((result["status"], result["previousAgent"], result["previousBranch"]),
                         ("took-over", "gone", "feat/old-5"))
        self.assertGreater(result["idleSeconds"], self.TTL)
        self.assertTrue(claims.read_issue_claim(self.a, 5).owned_by("b", socket.gethostname()))

    def test_recent_claim_of_another_slot_is_not_taken_over(self):
        push_old_claim(self.a, 5, "alive", age_seconds=60)
        self.assertEqual(self.claim(self.b, "b")["status"], "busy")

    def test_live_issue_claims_skips_released_and_stale_claims(self):
        self.claim(self.a, "a")
        claims.claim_issue(self.a, 6, "origin/main", "a", self.TTL)
        claims.release_issue(self.a, 6, "a")
        push_old_claim(self.a, 7, "gone", age_seconds=self.TTL + 60)
        self.assertEqual(claims.live_issue_claims(self.b, self.TTL), {5})

    def test_claim_does_not_change_the_worktree_or_base(self):
        head = git("rev-parse", "HEAD", cwd=self.a)
        self.claim(self.a, "a")
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.a), head)
        self.assertEqual(git("status", "--porcelain", cwd=self.a), "")
        self.assertEqual(git("rev-parse", "refs/heads/main", cwd=self.remote), head)

    def test_unreachable_remote_is_an_error_not_busy(self):
        git("remote", "set-url", "origin", str(self.root / "missing.git"), cwd=self.b)
        with self.assertRaises(claims.ClaimError):
            self.claim(self.b, "b")

    def test_claims_written_before_the_state_trailers_count_as_active(self):
        claim = claims.parse_claim("sha", "claim issue #5\n\nDevloops-Claim-Agent: a\n", 100)
        self.assertEqual((claim.agent, claim.state, claim.time, claim.host), ("a", claims.ACTIVE, 100, None))
        self.assertTrue(claim.owned_by("a", "any-host"))


class CliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.state = self.root / "state"
        env = patch.dict(os.environ, {"LOOP_STATE_DIR": str(self.state)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(tick_gate.TOKEN_ENV, None)
        os.environ.pop(AGENT_ID_ENV, None)

    def tick_token(self, agent):
        with patch.dict(os.environ, {AGENT_ID_ENV: agent}):
            return tick_gate.acquire_lock_token(get_lock_path("worker-loop"))

    def run_cli(self, *argv, open_work=1):
        out = io.StringIO()
        with patch.object(claims, "_fetch_open_work", return_value=open_work), \
                patch.dict(os.environ, {}), contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = claims.main(list(argv))
        return code, out.getvalue()

    def test_pr_commands_need_the_tick_token(self):
        token_a, token_b = self.tick_token("a"), self.tick_token("b")
        self.assertEqual(self.run_cli("pr", "7", "--agent-id", "a")[0], tick_gate.EXIT_ERROR)
        self.assertEqual(self.run_cli("pr", "7", "--agent-id", "a", "--token", token_a)[0], tick_gate.EXIT_RUN)
        code, text = self.run_cli("pr", "7", "--agent-id", "b", "--token", token_b)
        self.assertEqual(code, tick_gate.EXIT_BUSY)
        self.assertIn("agent a", text)
        self.assertTrue((self.state / "claims" / "pr-7").exists())
        self.assertEqual(self.run_cli("release-pr", "7", "--agent-id", "a", "--token", token_a)[0], tick_gate.EXIT_RUN)
        self.assertFalse((self.state / "claims" / "pr-7").exists())

    def test_token_from_the_environment_is_used(self):
        token = self.tick_token("a")
        with patch.dict(os.environ, {tick_gate.TOKEN_ENV: token}):
            self.assertEqual(self.run_cli("pr", "7", "--agent-id", "a")[0], tick_gate.EXIT_RUN)

    def test_tick_that_lost_its_lock_cannot_claim(self):
        token = self.tick_token("a")
        code, text = self.run_cli("pr", "7", "--agent-id", "a", "--token", "f" * 32)
        self.assertEqual(code, tick_gate.EXIT_LOST)
        self.assertIn("LOST", text)
        self.assertFalse((self.state / "claims" / "pr-7").exists())
        self.assertTrue(token)

    def test_issue_exit_codes(self):
        remote, (a, b) = make_remote(self.root, "a", "b")
        token_a, token_b = self.tick_token("a"), self.tick_token("b")
        base = ("--base", "origin/main")
        code, text = self.run_cli("issue", "5", *base, "--repo", str(a), "--agent-id", "a", "--token", token_a)
        self.assertEqual(code, tick_gate.EXIT_RUN, text)
        self.assertIn("CLAIMED: issue #5", text)
        code, text = self.run_cli("issue", "5", *base, "--repo", str(b), "--agent-id", "b", "--token", token_b)
        self.assertEqual(code, tick_gate.EXIT_BUSY, text)
        # Refreshing our own claim never trips the limit.
        code, text = self.run_cli("issue", "5", *base, "--repo", str(a), "--agent-id", "a", "--token", token_a,
                                  open_work=99)
        self.assertEqual((code, text.strip()), (tick_gate.EXIT_RUN, "REFRESHED: issue #5"))
        # Over the limit, the new claim is released so it does not block anyone.
        code, text = self.run_cli("issue", "6", *base, "--repo", str(b), "--agent-id", "b", "--token", token_b,
                                  open_work=6)
        self.assertEqual(code, tick_gate.EXIT_NOOP)
        self.assertIn("OVER LIMIT", text)
        self.assertEqual(claims.read_issue_claim(a, 6).state, claims.RELEASED)
        # A tick that lost its lock pushes nothing.
        code, _ = self.run_cli("issue", "8", *base, "--repo", str(a), "--agent-id", "a", "--token", "0" * 32)
        self.assertEqual(code, tick_gate.EXIT_LOST)
        self.assertIsNone(claims.read_issue_claim(a, 8))
        code, text = self.run_cli("release-issue", "5", "--repo", str(a), "--agent-id", "a", "--token", token_a)
        self.assertEqual((code, text.strip()), (tick_gate.EXIT_RUN, "RELEASED: issue #5"))
        git("remote", "set-url", "origin", str(self.root / "missing.git"), cwd=b)
        code, _ = self.run_cli("issue", "5", *base, "--repo", str(b), "--agent-id", "b", "--token", token_b)
        self.assertEqual(code, tick_gate.EXIT_ERROR)
        self.assertTrue(remote.exists())


class CountOpenWorkTest(unittest.TestCase):
    def test_counts_unapproved_prs_and_claims_without_a_pr(self):
        prs = [
            {"headRefName": "feat/x-1"},
            {"headRefName": "feat/y", "closingIssuesReferences": [{"number": 2}]},
            {"headRefName": "feat/z-3", "reviewDecision": "APPROVED"},
            {"headRefName": "feat/w-4", "labels": [{"name": claims.GATEKEEPER_APPROVED_LABEL}]},
        ]
        # 1 and 2 are claims that have a PR, 3 and 4 have approved PRs,
        # 5 has no PR, and 9 is a leftover claim on a closed issue.
        count = claims.count_open_work(prs, {1, 2, 3, 4, 5}, {1, 2, 3, 4, 5, 9})
        self.assertEqual(count, 3)


if __name__ == "__main__":
    unittest.main()
