#!/usr/bin/env python3
"""Tests for tick_gate logic."""
import errno
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloops_lib import tick_gate


class TickGateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.state_file = self.tmp_dir / "state.json"
        self.lock_file = self.tmp_dir / "tick.lock"

    def test_load_and_save_state(self):
        state = {"current": None, "lastFingerprint": "abc"}
        tick_gate.save_state(self.state_file, state)
        loaded = tick_gate.load_state(self.state_file)
        self.assertEqual(loaded, state)

    def test_lock_acquire_and_release(self):
        self.assertFalse(tick_gate.check_lock(self.lock_file))
        tick_gate.acquire_lock(self.lock_file)
        self.assertTrue(tick_gate.check_lock(self.lock_file))
        tick_gate.release_lock(self.lock_file)
        self.assertFalse(tick_gate.check_lock(self.lock_file))

    def test_lock_with_pid(self):
        # Current process PID is alive
        tick_gate.acquire_lock(self.lock_file, pid=os.getpid())
        self.assertTrue(tick_gate.check_lock(self.lock_file))
        tick_gate.release_lock(self.lock_file)

        # Dead PID is recognized as stale
        dead_pid = 9999999
        tick_gate.acquire_lock(self.lock_file, pid=dead_pid)
        self.assertFalse(tick_gate.check_lock(self.lock_file))

    def test_stale_lock_by_ttl(self):
        self.assertEqual(tick_gate.LOCK_TTL_SECONDS, 1800)
        # Create a lock file with timestamp older than TTL
        old_time = time.time() - (tick_gate.LOCK_TTL_SECONDS + 10)
        self.lock_file.write_text(json.dumps({"time": old_time, "pid": os.getpid()}))
        self.assertFalse(tick_gate.check_lock(self.lock_file))

    def test_gh_failure_raises_gh_error(self):
        with patch("devloops_lib.tick_gate.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="Bad credentials", stdout="")
            with self.assertRaises(tick_gate.GhError):
                tick_gate.gh("api", "user")

    def test_compute_fingerprint_graphql(self):
        fake_graphql = {
            "data": {
                "search": {
                    "nodes": [
                        {
                            "number": 1,
                            "headRefOid": "sha1",
                            "mergeable": "MERGEABLE",
                            "updatedAt": "2026-01-01T00:00:00Z",
                            "isDraft": False,
                            "author": {"login": "me"},
                            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "PENDING"}}}]},
                            "comments": {"nodes": [{"createdAt": "2026-01-01T01:00:00Z"}]},
                        }
                    ]
                },
                "repository": {
                    "issues": {
                        "nodes": [
                            {"number": 10, "updatedAt": "2026-01-01T00:00:00Z"}
                        ]
                    }
                }
            }
        }
        with patch.object(tick_gate, "gh") as mock_gh:
            mock_gh.side_effect = [
                json.dumps({"owner": {"login": "owner"}, "name": "repo"}),  # repo view
                json.dumps(fake_graphql),  # graphql
                "me\n",  # api user
            ]
            fp1 = tick_gate.compute_fingerprint()
            self.assertTrue(isinstance(fp1, str) and len(fp1) == 64)

            # Change in CI state produces a different fingerprint
            fake_graphql_ci_failed = json.loads(json.dumps(fake_graphql))
            fake_graphql_ci_failed["data"]["search"]["nodes"][0]["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["state"] = "FAILURE"

            mock_gh.side_effect = [
                json.dumps({"owner": {"login": "owner"}, "name": "repo"}),
                json.dumps(fake_graphql_ci_failed),
                "me\n",
            ]
            fp2 = tick_gate.compute_fingerprint()
            self.assertNotEqual(fp1, fp2)

    def test_backlog_approved_unclaimed_stable_fingerprint_runs(self):
        # AC1: approved, unclaimed issue + stable fingerprint -> RUN
        state = {"lastFingerprint": "fp_stable"}
        tick_gate.save_state(self.state_file, state)

        with patch.object(tick_gate, "preflight_decision", return_value=("fp_stable", [42])), \
             patch("sys.argv", ["tick_gate.py", "--state-file", str(self.state_file), "--lock"]), \
             self.assertRaises(SystemExit) as cm:
            tick_gate.main()
        self.assertEqual(cm.exception.code, tick_gate.EXIT_RUN)
        self.assertTrue(tick_gate.check_lock(self.lock_file))

    def test_backlog_empty_stable_fingerprint_noop(self):
        # AC2: same fingerprint + no actionable backlog -> NOOP
        state = {"lastFingerprint": "fp_stable"}
        tick_gate.save_state(self.state_file, state)

        with patch.object(tick_gate, "preflight_decision", return_value=("fp_stable", [])), \
             patch("sys.argv", ["tick_gate.py", "--state-file", str(self.state_file), "--lock"]), \
             self.assertRaises(SystemExit) as cm:
            tick_gate.main()
        self.assertEqual(cm.exception.code, tick_gate.EXIT_NOOP)
        self.assertFalse(tick_gate.check_lock(self.lock_file))

    def test_find_actionable_backlog_rules(self):
        def issue(number, labels, assignees=()):
            return {
                "number": number,
                "updatedAt": "2026-01-01T00:00:00Z",
                "labels": {"nodes": [{"name": l} for l in labels]},
                "assignees": {"nodes": [{"login": a} for a in assignees]},
            }

        pr = {
            "number": 100,
            "title": "fix: close #101",
            "bodyText": "Fixes #101 and mentions #103",
            "headRefName": "feat/some-thing-102",
            "author": {"login": "someone"},
        }
        gate_data = {
            "prs": [pr],
            "issues": [
                issue(100, ["approved"]),                      # actionable
                issue(101, ["approved"]),                      # closed by PR title/body -> skip
                issue(102, ["approved"]),                      # closed by branch suffix -> skip
                issue(103, ["approved"], ["someone"]),         # assigned to other -> skip
                issue(104, ["enhancement"]),                   # not approved -> skip
                issue(105, ["approved"], ["me"]),              # assigned to @me only -> actionable
            ],
        }
        self.assertEqual(tick_gate.find_actionable_backlog(gate_data, me="me"), [100, 105])

    def test_update_takes_precedence_over_current(self):
        state = {"current": "some-feature-branch", "lastFingerprint": "old", "tickStartFingerprint": "new_fp_1234567890"}
        tick_gate.save_state(self.state_file, state)
        with patch("sys.argv", ["tick_gate.py", "--state-file", str(self.state_file), "--update"]), \
             self.assertRaises(SystemExit) as cm:
            tick_gate.main()
        self.assertEqual(cm.exception.code, tick_gate.EXIT_RUN)
        saved = tick_gate.load_state(self.state_file)
        self.assertEqual(saved.get("lastFingerprint"), "new_fp_1234567890")
        self.assertNotIn("tickStartFingerprint", saved)

    def test_update_and_unlock_combined(self):
        tick_gate.acquire_lock(self.lock_file)
        self.assertTrue(tick_gate.check_lock(self.lock_file))
        state = {"current": None, "lastFingerprint": "old_fp", "tickStartFingerprint": "new_fp_combined"}
        tick_gate.save_state(self.state_file, state)

        with patch("sys.argv", ["tick_gate.py", "--state-file", str(self.state_file), "--update", "--unlock"]), \
             self.assertRaises(SystemExit) as cm:
            tick_gate.main()

        self.assertEqual(cm.exception.code, tick_gate.EXIT_RUN)
        saved = tick_gate.load_state(self.state_file)
        self.assertEqual(saved.get("lastFingerprint"), "new_fp_combined")
        self.assertNotIn("tickStartFingerprint", saved)
        self.assertFalse(tick_gate.check_lock(self.lock_file))

    def test_update_missing_tick_start_fingerprint(self):
        state = {"current": None, "lastFingerprint": "old_fp"}
        tick_gate.save_state(self.state_file, state)

        with patch("sys.argv", ["tick_gate.py", "--state-file", str(self.state_file), "--update"]), \
             self.assertRaises(SystemExit) as cm:
            tick_gate.main()

        self.assertEqual(cm.exception.code, tick_gate.EXIT_RUN)
        saved = tick_gate.load_state(self.state_file)
        self.assertEqual(saved.get("lastFingerprint"), "old_fp")
        self.assertNotIn("tickStartFingerprint", saved)

    def test_unlock_alone_clears_tick_start_fingerprint_without_writing_last_fingerprint(self):
        tick_gate.acquire_lock(self.lock_file)
        self.assertTrue(tick_gate.check_lock(self.lock_file))
        state = {"current": None, "lastFingerprint": "old_fp", "tickStartFingerprint": "started_fp"}
        tick_gate.save_state(self.state_file, state)

        with patch("sys.argv", ["tick_gate.py", "--state-file", str(self.state_file), "--unlock"]), \
             self.assertRaises(SystemExit) as cm:
            tick_gate.main()

        self.assertEqual(cm.exception.code, tick_gate.EXIT_RUN)
        saved = tick_gate.load_state(self.state_file)
        self.assertEqual(saved.get("lastFingerprint"), "old_fp")
        self.assertNotIn("tickStartFingerprint", saved)
        self.assertFalse(tick_gate.check_lock(self.lock_file))

    def test_update_and_unlock_gh_error_releases_lock(self):
        tick_gate.acquire_lock(self.lock_file)
        self.assertTrue(tick_gate.check_lock(self.lock_file))

        with patch.object(tick_gate, "load_state", side_effect=tick_gate.GhError("simulated gh failure")), \
             patch("sys.argv", ["tick_gate.py", "--state-file", str(self.state_file), "--update", "--unlock"]), \
             self.assertRaises(SystemExit) as cm:
            tick_gate.main()

        self.assertEqual(cm.exception.code, tick_gate.EXIT_ERROR)
        self.assertEqual(cm.exception.code, 3)
        self.assertFalse(tick_gate.check_lock(self.lock_file))

    def test_changes_during_tick_detected_next_tick(self):
        # (a) changes made during a tick (fingerprint differs from tick start) are detected next tick (gate RUNs)
        state = {"lastFingerprint": "fp_old"}
        tick_gate.save_state(self.state_file, state)

        with patch.object(tick_gate, "preflight_decision", return_value=("fp_start", [])), \
             patch("sys.argv", ["tick_gate.py", "--state-file", str(self.state_file), "--lock"]), \
             self.assertRaises(SystemExit) as cm:
            tick_gate.main()
        self.assertEqual(cm.exception.code, tick_gate.EXIT_RUN)
        self.assertTrue(tick_gate.check_lock(self.lock_file))
        saved = tick_gate.load_state(self.state_file)
        self.assertEqual(saved.get("tickStartFingerprint"), "fp_start")

        # Tick 1 ends idle -> --update --unlock (persists tickStartFingerprint as lastFingerprint)
        with patch("sys.argv", ["tick_gate.py", "--state-file", str(self.state_file), "--update", "--unlock"]), \
             self.assertRaises(SystemExit) as cm:
            tick_gate.main()
        self.assertEqual(cm.exception.code, tick_gate.EXIT_RUN)
        self.assertFalse(tick_gate.check_lock(self.lock_file))
        saved = tick_gate.load_state(self.state_file)
        self.assertEqual(saved.get("lastFingerprint"), "fp_start")
        self.assertNotIn("tickStartFingerprint", saved)

        # Tick 2 starts: changes were made during tick 1, so GitHub returns fp_during_tick
        with patch.object(tick_gate, "preflight_decision", return_value=("fp_during_tick", [])), \
             patch("sys.argv", ["tick_gate.py", "--state-file", str(self.state_file), "--lock"]), \
             self.assertRaises(SystemExit) as cm:
            tick_gate.main()
        self.assertEqual(cm.exception.code, tick_gate.EXIT_RUN)

    def test_idle_tick_unchanged_snapshot_gives_noop(self):
        # (b) after an idle tick with --update --unlock, an unchanged snapshot gives NOOP
        state = {"lastFingerprint": "fp_old"}
        tick_gate.save_state(self.state_file, state)

        with patch.object(tick_gate, "preflight_decision", return_value=("fp_idle", [])), \
             patch("sys.argv", ["tick_gate.py", "--state-file", str(self.state_file), "--lock"]), \
             self.assertRaises(SystemExit) as cm:
            tick_gate.main()
        self.assertEqual(cm.exception.code, tick_gate.EXIT_RUN)

        # Tick 1 ends idle: --update --unlock
        with patch("sys.argv", ["tick_gate.py", "--state-file", str(self.state_file), "--update", "--unlock"]), \
             self.assertRaises(SystemExit) as cm:
            tick_gate.main()
        self.assertEqual(cm.exception.code, tick_gate.EXIT_RUN)
        saved = tick_gate.load_state(self.state_file)
        self.assertEqual(saved.get("lastFingerprint"), "fp_idle")

        # Next tick: snapshot unchanged (still fp_idle) => NOOP
        with patch.object(tick_gate, "preflight_decision", return_value=("fp_idle", [])), \
             patch("sys.argv", ["tick_gate.py", "--state-file", str(self.state_file), "--lock"]), \
             self.assertRaises(SystemExit) as cm:
            tick_gate.main()
        self.assertEqual(cm.exception.code, tick_gate.EXIT_NOOP)

    def test_work_tick_unlock_only_next_gate_runs_even_if_unchanged(self):
        # (c) after a work tick with --unlock only, the next gate RUNs even if snapshot unchanged
        state = {"lastFingerprint": "fp_old"}
        tick_gate.save_state(self.state_file, state)

        with patch.object(tick_gate, "preflight_decision", return_value=("fp_work", [])), \
             patch("sys.argv", ["tick_gate.py", "--state-file", str(self.state_file), "--lock"]), \
             self.assertRaises(SystemExit) as cm:
            tick_gate.main()
        self.assertEqual(cm.exception.code, tick_gate.EXIT_RUN)
        self.assertTrue(tick_gate.check_lock(self.lock_file))

        # Tick 1 completes work: --unlock only (no --update)
        with patch("sys.argv", ["tick_gate.py", "--state-file", str(self.state_file), "--unlock"]), \
             self.assertRaises(SystemExit) as cm:
            tick_gate.main()
        self.assertEqual(cm.exception.code, tick_gate.EXIT_RUN)
        self.assertFalse(tick_gate.check_lock(self.lock_file))
        saved = tick_gate.load_state(self.state_file)
        self.assertEqual(saved.get("lastFingerprint"), "fp_old")
        self.assertNotIn("tickStartFingerprint", saved)

        # Next tick: snapshot unchanged (still fp_work) => still RUNs because lastFingerprint is still fp_old
        with patch.object(tick_gate, "preflight_decision", return_value=("fp_work", [])), \
             patch("sys.argv", ["tick_gate.py", "--state-file", str(self.state_file), "--lock"]), \
             self.assertRaises(SystemExit) as cm:
            tick_gate.main()
        self.assertEqual(cm.exception.code, tick_gate.EXIT_RUN)



    def test_concurrent_lock_attempts(self):
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(tick_gate.acquire_lock, self.lock_file)
            f2 = executor.submit(tick_gate.acquire_lock, self.lock_file)
            results = [f1.result(), f2.result()]

        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 1)
        self.assertTrue(tick_gate.check_lock(self.lock_file))

    def test_atomic_state_write_failure_preserves_old_state(self):
        old_state = {"current": None, "lastFingerprint": "initial"}
        tick_gate.save_state(self.state_file, old_state)

        new_state = {"current": 101, "lastFingerprint": "corrupted"}
        with patch.object(Path, "replace", side_effect=OSError("Simulated replace error")):
            with self.assertRaises(OSError):
                tick_gate.save_state(self.state_file, new_state)

        # Old state.json is intact
        loaded = tick_gate.load_state(self.state_file)
        self.assertEqual(loaded, old_state)

class LockPublicationTest(unittest.TestCase):
    """Issue #22: mutual exclusion across publish/reclaim/release."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.lock_file = Path(self._tmp.name) / "tick.lock"

    def _payload(self):
        return json.loads(self.lock_file.read_text(encoding="utf-8"))

    def _write_stale_lock(self, token="old-owner"):
        self.lock_file.write_text(
            json.dumps({"time": time.time() - (tick_gate.LOCK_TTL_SECONDS + 10), "token": token}),
            encoding="utf-8",
        )

    def _pause_thread_before_unlink(self, thread_name):
        """Patch _unlink_lock so thread_name pauses after validating, before removal."""
        validated = threading.Event()
        resume = threading.Event()
        self.addCleanup(resume.set)
        original = tick_gate._unlink_lock

        def paused_unlink(lock_file):
            if threading.current_thread().name == thread_name:
                validated.set()
                resume.wait(10)
            return original(lock_file)

        patcher = patch.object(tick_gate, "_unlink_lock", side_effect=paused_unlink)
        patcher.start()
        self.addCleanup(patcher.stop)
        return validated, resume

    def test_contender_cannot_steal_lock_during_create_before_payload(self):
        # Issue #22's reported window, which exists only on the no-hard-link
        # fallback: A is paused between the exclusive create and the payload
        # write while B runs the full contested path. Events only; no sleeps.
        created = threading.Event()
        resume = threading.Event()
        self.addCleanup(resume.set)
        original_fdopen = os.fdopen

        def paused_fdopen(fd, *args, **kwargs):
            if threading.current_thread().name == "A":
                created.set()
                resume.wait(10)
            return original_fdopen(fd, *args, **kwargs)

        outcomes = {}
        owner = threading.Thread(
            name="A",
            target=lambda: outcomes.update(a=tick_gate.acquire_lock_token(self.lock_file)),
        )
        unsupported = OSError(errno.ENOTSUP, "hard links unsupported")
        with patch.object(tick_gate.os, "link", side_effect=unsupported), \
                patch.object(tick_gate.os, "fdopen", side_effect=paused_fdopen):
            owner.start()
            self.assertTrue(created.wait(10))
            self.assertEqual(self.lock_file.read_text(encoding="utf-8"), "")
            inode = self.lock_file.stat().st_ino
            outcomes["b"] = tick_gate.acquire_lock(self.lock_file)
            resume.set()
            owner.join(10)

        self.assertIs(outcomes["b"], False)
        self.assertIsNotNone(outcomes.get("a"))
        self.assertEqual(self._payload()["token"], outcomes["a"])
        self.assertEqual(self.lock_file.stat().st_ino, inode)

    def test_contender_cannot_steal_lock_published_during_acquire(self):
        # Hard-link path: owner A has published and is paused before acquire
        # returns; contender B must not steal or unlink A's live lock.
        # Synchronization is event-based; no sleeps.
        published = threading.Event()
        resume = threading.Event()
        original = tick_gate._publish_lock

        def slow_publish(lock_file, data):
            ok = original(lock_file, data)
            if ok:
                published.set()
                resume.wait(10)
            return ok

        outcomes = {}

        def owner():
            outcomes["a"] = tick_gate.acquire_lock_token(self.lock_file)

        def contender():
            self.assertTrue(published.wait(10))
            outcomes["b"] = tick_gate.acquire_lock(self.lock_file)

        with patch.object(tick_gate, "_publish_lock", side_effect=slow_publish):
            t_owner = threading.Thread(target=owner)
            t_contender = threading.Thread(target=contender)
            t_owner.start()
            t_contender.start()
            self.assertTrue(published.wait(10))
            resume.set()
            t_owner.join(10)
            t_contender.join(10)

        self.assertIsNotNone(outcomes.get("a"))
        self.assertIs(outcomes.get("b"), False)
        self.assertEqual(self._payload()["token"], outcomes["a"])
        self.assertTrue(tick_gate.check_lock(self.lock_file))

    def test_fresh_unparseable_lock_is_treated_as_in_progress(self):
        # Empty/partial/corrupt payloads younger than TTL are an in-progress
        # acquisition, never a stale lock to steal.
        for bad in ("", "not json", "null"):
            with self.subTest(payload=bad):
                if self.lock_file.exists():
                    self.lock_file.unlink()
                self.lock_file.write_text(bad, encoding="utf-8")
                self.assertTrue(tick_gate.check_lock(self.lock_file))
                self.assertFalse(tick_gate.acquire_lock(self.lock_file))
                # The contender must not have deleted or rewritten the lock.
                self.assertEqual(self.lock_file.read_text(encoding="utf-8"), bad)

    def test_corrupt_lock_older_than_ttl_is_reclaimable(self):
        self.lock_file.write_text("{corrupt", encoding="utf-8")
        old = time.time() - (tick_gate.LOCK_TTL_SECONDS + 60)
        os.utime(self.lock_file, (old, old))
        self.assertFalse(tick_gate.check_lock(self.lock_file))
        self.assertTrue(tick_gate.acquire_lock(self.lock_file))
        self.assertIn("token", self._payload())

    def test_reclaimer_paused_before_removal_cannot_delete_newer_owner(self):
        # C validates the stale lock and pauses before removing it. B then runs
        # the full contested path. If B could reclaim and publish here, C would
        # delete B's live lock on resume and both would own the lock.
        self._write_stale_lock()
        validated, resume = self._pause_thread_before_unlink("C")
        tokens = {}
        reclaimer = threading.Thread(
            name="C",
            target=lambda: tokens.update(c=tick_gate.acquire_lock_token(self.lock_file)),
        )
        reclaimer.start()
        self.assertTrue(validated.wait(10))
        tokens["b"] = tick_gate.acquire_lock_token(self.lock_file)
        resume.set()
        reclaimer.join(10)

        winners = [token for token in tokens.values() if token is not None]
        self.assertEqual(len(winners), 1, msg=f"tokens={tokens}")
        self.assertEqual(self._payload()["token"], winners[0])
        self.assertTrue(tick_gate.check_lock(self.lock_file))

    def test_late_release_cannot_open_a_gap_during_reclaim(self):
        # The old owner's release must not remove the validated lock under a
        # paused reclaimer, or a third caller could publish into the gap and
        # then be deleted by the reclaimer.
        self._write_stale_lock(token="old-owner")
        validated, resume = self._pause_thread_before_unlink("C")
        tokens = {}
        reclaimer = threading.Thread(
            name="C",
            target=lambda: tokens.update(c=tick_gate.acquire_lock_token(self.lock_file)),
        )
        reclaimer.start()
        self.assertTrue(validated.wait(10))
        with patch.object(tick_gate, "GUARD_RELEASE_WAIT_SECONDS", 0):
            tick_gate.release_lock(self.lock_file, token="old-owner")
        self.assertEqual(self._payload()["token"], "old-owner")
        resume.set()
        reclaimer.join(10)

        self.assertIsNotNone(tokens.get("c"))
        self.assertEqual(self._payload()["token"], tokens["c"])

    def test_guard_left_by_a_dead_holder_does_not_block_reclaim(self):
        code = (
            "import os, sys;"
            "from pathlib import Path;"
            "sys.path.insert(0, sys.argv[1]);"
            "from devloops_lib import tick_gate;"
            "guard = tick_gate._removal_guard(Path(sys.argv[2]));"
            "held = guard.__enter__();"
            "os._exit(0 if held else 1)"
        )
        proc = subprocess.run([sys.executable, "-c", code, str(REPO_ROOT), str(self.lock_file)], timeout=60)
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(self.lock_file.with_name("tick.lock.guard").exists())
        self._write_stale_lock()
        self.assertIsNotNone(tick_gate.acquire_lock_token(self.lock_file))

    def test_unavailable_guard_fails_closed(self):
        self._write_stale_lock(token="old-owner")
        before = self.lock_file.read_text(encoding="utf-8")
        with patch.object(tick_gate, "_try_os_lock", side_effect=OSError(errno.ENOLCK, "no locks")), \
                patch.object(tick_gate, "GUARD_RELEASE_WAIT_SECONDS", 0):
            self.assertFalse(tick_gate.acquire_lock(self.lock_file))
            tick_gate.release_lock(self.lock_file, token="old-owner")
            self.assertEqual(self.lock_file.read_text(encoding="utf-8"), before)
            # The operator's unconditional --unlock remains the escape hatch.
            tick_gate.release_lock(self.lock_file)
        self.assertFalse(self.lock_file.exists())

    def test_publish_falls_back_only_when_hard_links_are_unsupported(self):
        cases = [
            (errno.EACCES, "busy"),
            (errno.EIO, "raises"),
            (errno.EPERM, "fallback"),
            (errno.ENOTSUP, "fallback"),
        ]
        for code, expected in cases:
            with self.subTest(errno=errno.errorcode[code]), \
                    patch.object(tick_gate.os, "link", side_effect=OSError(code, "link failed")):
                if self.lock_file.exists():
                    self.lock_file.unlink()
                if expected == "raises":
                    with self.assertRaises(OSError):
                        tick_gate._publish_lock(self.lock_file, b"{}")
                else:
                    self.assertEqual(tick_gate._publish_lock(self.lock_file, b"{}"), expected == "fallback")
                self.assertEqual(self.lock_file.exists(), expected == "fallback")

    def test_stale_lock_with_concurrent_contenders_has_one_winner(self):
        # Scheduling-dependent stress check; the deterministic interleavings
        # are covered by the paused-reclaimer tests above.
        self._write_stale_lock()
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: tick_gate.acquire_lock(self.lock_file), range(4)))
        self.assertEqual(results.count(True), 1)
        self.assertTrue(tick_gate.check_lock(self.lock_file))

    def test_non_owner_release_cannot_delete_replacement_lock(self):
        token_a = tick_gate.acquire_lock_token(self.lock_file)
        self.assertIsNotNone(token_a)
        # A's lock goes stale and B reclaims it.
        stale = self._payload()
        stale["time"] = time.time() - (tick_gate.LOCK_TTL_SECONDS + 10)
        self.lock_file.write_text(json.dumps(stale), encoding="utf-8")
        token_b = tick_gate.acquire_lock_token(self.lock_file)
        self.assertIsNotNone(token_b)
        self.assertNotEqual(token_a, token_b)
        # A's late, token-checked release must not remove B's replacement lock.
        tick_gate.release_lock(self.lock_file, token=token_a)
        self.assertEqual(self._payload()["token"], token_b)
        # B releases its own lock normally.
        tick_gate.release_lock(self.lock_file, token=token_b)
        self.assertFalse(self.lock_file.exists())

    def test_subprocess_contenders_exactly_one_winner(self):
        code = (
            "import sys;"
            "from pathlib import Path;"
            "sys.path.insert(0, sys.argv[1]);"
            "from devloops_lib import tick_gate;"
            "sys.stdout.write('1' if tick_gate.acquire_lock(Path(sys.argv[2])) else '0')"
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(REPO_ROOT), str(self.lock_file)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for _ in range(4)
        ]
        outputs = [p.communicate(timeout=60) for p in procs]
        winners = sum(1 for out, _err in outputs if out.strip() == "1")
        self.assertEqual(winners, 1, msg=f"outputs={outputs}")
        self.assertTrue(tick_gate.check_lock(self.lock_file))


if __name__ == "__main__":
    unittest.main()
