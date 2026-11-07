#!/usr/bin/env python3
"""Tests for guard.py: ownership checks and reset of a worker worktree without losing work."""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloops_lib import claims, guard, tick_gate
from devloops_lib.config import AGENT_ID_ENV, get_claims_dir, get_lock_path, get_worktree_path
from devloops_lib.tick_gate import EXIT_ERROR, EXIT_LOST, EXIT_RUN, TOKEN_ENV

from tests.test_claims import git, make_remote


class GuardTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        env = patch.dict(os.environ, {"LOOP_STATE_DIR": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(TOKEN_ENV, None)
        os.environ.pop(AGENT_ID_ENV, None)

    def token(self, agent=None):
        with patch.dict(os.environ, {AGENT_ID_ENV: agent} if agent else {}):
            return tick_gate.acquire_lock_token(get_lock_path("worker-loop"))

    def guard(self, *argv):
        out = io.StringIO()
        with patch.dict(os.environ, {}), contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = guard.main(list(argv))
        return code, out.getvalue()


class OwnershipTest(GuardTestCase):
    def test_check_needs_the_live_tick_token(self):
        token = self.token()
        self.assertEqual(self.guard("check")[0], EXIT_ERROR)
        self.assertEqual(self.guard("check", "--token", token), (EXIT_RUN, "OWNED\n"))
        code, text = self.guard("check", "--token", "0" * 32)
        self.assertEqual(code, EXIT_LOST)
        self.assertIn("LOST: tick lock", text)

    def test_run_executes_only_while_owned_and_returns_the_command_exit_code(self):
        token = self.token()
        marker = self.root / "ran"
        touch = [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]
        self.assertEqual(self.guard("run", "--token", "0" * 32, "--", *touch)[0], EXIT_LOST)
        self.assertFalse(marker.exists())
        self.assertEqual(self.guard("run", "--token", token, "--", *touch)[0], 0)
        self.assertTrue(marker.exists())
        self.assertEqual(self.guard("run", "--token", token, "--", sys.executable, "-c", "raise SystemExit(5)")[0], 5)

    def test_run_needs_the_pr_claim(self):
        token = self.token()
        other = self.token("b")
        lock_b = get_lock_path("worker-loop").parent / "agents" / "b" / "tick.lock"
        self.assertTrue(claims.claim_pr(get_claims_dir(), 7, "b", lock_b, other))
        code, text = self.guard("run", "--token", token, "--pr", "7", "--", sys.executable, "-c", "")
        self.assertEqual(code, EXIT_LOST)
        self.assertIn("PR #7 claim", text)
        claims.release_pr(get_claims_dir(), 7, other)
        self.assertTrue(claims.claim_pr(get_claims_dir(), 7, "default", get_lock_path("worker-loop"), token))
        self.assertEqual(self.guard("check", "--token", token, "--pr", "7")[0], EXIT_RUN)

    def test_check_refreshes_the_issue_claim_and_detects_its_loss(self):
        _, (repo,) = make_remote(self.root, "a")
        token = self.token()
        claims.claim_issue(repo, 5, "origin/main", "default", 3600)
        before = claims.read_issue_claim(repo, 5).sha
        self.assertEqual(self.guard("check", "--token", token, "--issue", "5", "--repo", str(repo))[0], EXIT_RUN)
        self.assertNotEqual(claims.read_issue_claim(repo, 5).sha, before)
        claims.release_issue(repo, 5, "default")
        code, text = self.guard("check", "--token", token, "--issue", "5", "--repo", str(repo))
        self.assertEqual(code, EXIT_LOST)
        self.assertIn("issue #5 claim (agent default, released)", text)


class ResetWorktreeTest(GuardTestCase):
    def setUp(self):
        super().setUp()
        _, (self.main,) = make_remote(self.root, "main")
        self.worktrees = {}
        for agent in ("a", "b"):
            with patch.dict(os.environ, {AGENT_ID_ENV: agent}):
                path = get_worktree_path(self.main)
            git("worktree", "add", "-b", f"work-{agent}", str(path), "origin/main", cwd=self.main)
            self.worktrees[agent] = path

    def reset(self, agent, token):
        return self.guard("reset-worktree", "--agent-id", agent, "--token", token, "--repo", str(self.worktrees[agent]))

    def dirty(self, path: Path):
        (path / "file.txt").write_text("edited\n", encoding="utf-8")
        (path / "new.txt").write_text("untracked\n", encoding="utf-8")

    def test_dirty_changes_are_stashed_before_the_reset(self):
        token = self.token("a")
        work = self.worktrees["a"]
        self.dirty(work)
        code, text = self.reset("a", token)
        self.assertEqual(code, EXIT_RUN, text)
        self.assertIn("PRESERVED: stash", text)
        self.assertEqual(git("status", "--porcelain", cwd=work), "")
        self.assertIn("devloops preserved: agent a", git("stash", "list", cwd=work))
        git("stash", "pop", cwd=work)
        self.assertEqual((work / "file.txt").read_text(encoding="utf-8"), "edited\n")
        self.assertEqual((work / "new.txt").read_text(encoding="utf-8"), "untracked\n")

    def test_unfinished_rebase_is_saved_as_a_patch_and_aborted(self):
        token = self.token("a")
        work = self.worktrees["a"]
        (work / "file.txt").write_text("ours\n", encoding="utf-8")
        git("commit", "-am", "ours", cwd=work)
        git("checkout", "-b", "theirs", "origin/main", cwd=work)
        (work / "file.txt").write_text("theirs\n", encoding="utf-8")
        git("commit", "-am", "theirs", cwd=work)
        git("checkout", "work-a", cwd=work)
        with self.assertRaises(subprocess.CalledProcessError):
            git("rebase", "theirs", cwd=work)
        code, text = self.reset("a", token)
        self.assertEqual(code, EXIT_RUN, text)
        patches = list((get_lock_path("worker-loop").parent / "agents" / "a" / "preserved").glob("*-rebase.patch"))
        self.assertEqual(len(patches), 1, text)
        self.assertIn("ours", patches[0].read_text(encoding="utf-8"))
        self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=work), "work-a")
        self.assertEqual((work / "file.txt").read_text(encoding="utf-8"), "ours\n")

    def test_one_slot_reset_does_not_touch_another_slot_worktree(self):
        token = self.token("a")
        self.dirty(self.worktrees["b"])
        self.assertEqual(self.reset("a", token)[0], EXIT_RUN)
        self.assertEqual((self.worktrees["b"] / "file.txt").read_text(encoding="utf-8"), "edited\n")
        self.assertTrue((self.worktrees["b"] / "new.txt").exists())

    def test_main_checkout_is_refused(self):
        token = self.token()
        self.dirty(self.main)
        code, text = self.guard("reset-worktree", "--token", token, "--repo", str(self.main))
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("main checkout", text)
        self.assertEqual((self.main / "file.txt").read_text(encoding="utf-8"), "edited\n")
        self.assertEqual(git("stash", "list", cwd=self.main), "")

    def test_tick_that_lost_its_lock_changes_nothing(self):
        self.token("a")
        self.dirty(self.worktrees["a"])
        code, _ = self.reset("a", "0" * 32)
        self.assertEqual(code, EXIT_LOST)
        self.assertEqual((self.worktrees["a"] / "file.txt").read_text(encoding="utf-8"), "edited\n")
        self.assertEqual(git("stash", "list", cwd=self.worktrees["a"]), "")


if __name__ == "__main__":
    unittest.main()
