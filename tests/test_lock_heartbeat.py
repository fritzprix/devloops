#!/usr/bin/env python3
"""Tests for detached tick lock heartbeat."""
from __future__ import annotations

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

from devloops_lib import tick_gate
from devloops_lib.lock_heartbeat import (
    clear_sidecar,
    read_sidecar,
    start_detached,
    stop_heartbeat,
    write_sidecar,
)

SCRIPTS = REPO_ROOT / "skills" / "worker-loop" / "scripts"


class LockHeartbeatTest(unittest.TestCase):
    ttl = 3

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.loop_dir = Path(self._tmp.name) / "worker-loop"
        self.loop_dir.mkdir(parents=True)
        self.lock = self.loop_dir / "agents" / "race" / "tick.lock"
        self.env = {
            k: v
            for k, v in os.environ.items()
            if k not in (tick_gate.TOKEN_ENV, tick_gate.LOCK_TTL_ENV)
        }
        self.env.update({
            "LOOP_STATE_DIR": str(self.loop_dir),
            "PYTHONPATH": str(REPO_ROOT),
            tick_gate.LOCK_TTL_ENV: str(self.ttl),
            "DEVLOOPS_AGENT_ID": "race",
        })
        race_state = self.loop_dir / "agents" / "race" / "state.json"
        race_state.parent.mkdir(parents=True)
        race_state.write_text('{"current": {"kind": "pr", "number": 1}}', encoding="utf-8")

    def test_slash_path_lock_survives_past_ttl(self):
        self.addCleanup(stop_heartbeat, self.lock)
        first = subprocess.run(
            [sys.executable, str(SCRIPTS / "tick_gate.py"), "--lock"],
            cwd=str(self.loop_dir),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(first.returncode, tick_gate.EXIT_RUN, first.stdout + first.stderr)
        token = [ln.split()[1] for ln in first.stdout.splitlines() if ln.startswith("TOKEN ")][0]
        time.sleep(self.ttl + 1)
        payload = tick_gate.read_lock_payload(self.lock)
        self.assertIsNotNone(payload)
        self.assertLessEqual(time.time() - payload["time"], self.ttl)
        self.assertTrue(tick_gate.lock_is_owned(self.lock, token))
        stop_heartbeat(self.lock)
        unlock_env = {**self.env, "DEVLOOPS_AGENT_ID": "race"}
        subprocess.run(
            [sys.executable, str(SCRIPTS / "tick_gate.py"), "--unlock", "--token", token],
            cwd=str(self.loop_dir),
            env=unlock_env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_heartbeat_survives_past_ttl_without_pythonpath(self):
        # Installed-skill layout (issue #83): nothing but the skill's own scripts
        # directory is on sys.path, PYTHONPATH unset. The detached heartbeat runs
        # devloops_lib/lock_heartbeat.py as a file, which used to die on
        # ModuleNotFoundError, silently losing the lock after the TTL.
        self.addCleanup(stop_heartbeat, self.lock)
        env = {k: v for k, v in self.env.items() if k != "PYTHONPATH"}
        first = subprocess.run(
            [sys.executable, str(SCRIPTS / "tick_gate.py"), "--lock"],
            cwd=str(self.loop_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(first.returncode, tick_gate.EXIT_RUN, first.stdout + first.stderr)
        token = [ln.split()[1] for ln in first.stdout.splitlines() if ln.startswith("TOKEN ")][0]
        time.sleep(self.ttl + 1)
        payload = tick_gate.read_lock_payload(self.lock)
        self.assertIsNotNone(payload)
        self.assertLessEqual(time.time() - payload["time"], self.ttl)
        self.assertTrue(tick_gate.lock_is_owned(self.lock, token))
        stop_heartbeat(self.lock)
        subprocess.run(
            [sys.executable, str(SCRIPTS / "tick_gate.py"), "--unlock", "--token", token],
            cwd=str(self.loop_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_max_lifetime_lets_dead_caller_lock_expire(self):
        # G1.1: caller takes the lock and exits without --unlock; after max
        # lifetime the heartbeat stops renewing so a second --lock can RUN.
        self.addCleanup(stop_heartbeat, self.lock)
        env = {
            k: v for k, v in self.env.items() if k != "PYTHONPATH"
        }
        env["DEVLOOPS_HEARTBEAT_MAX_LIFETIME"] = str(self.ttl + 1)
        first = subprocess.run(
            [sys.executable, str(SCRIPTS / "tick_gate.py"), "--lock"],
            cwd=str(self.loop_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(first.returncode, tick_gate.EXIT_RUN, first.stdout + first.stderr)
        # Dead caller: no --unlock. Wait past max lifetime + TTL so the lock is stale.
        time.sleep(self.ttl + 1 + self.ttl + 1)
        second = subprocess.run(
            [sys.executable, str(SCRIPTS / "tick_gate.py"), "--lock"],
            cwd=str(self.loop_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(second.returncode, tick_gate.EXIT_RUN, second.stdout + second.stderr)
        token2 = [ln.split()[1] for ln in second.stdout.splitlines() if ln.startswith("TOKEN ")][0]
        stop_heartbeat(self.lock)
        subprocess.run(
            [sys.executable, str(SCRIPTS / "tick_gate.py"), "--unlock", "--token", token2],
            cwd=str(self.loop_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_heartbeat_stops_after_unlock_and_lock_expires(self):
        self.addCleanup(stop_heartbeat, self.lock)
        token = tick_gate.acquire_lock_token(self.lock)
        start_detached(self.lock, token)
        time.sleep(0.3)
        stop_heartbeat(self.lock)
        tick_gate.release_lock(self.lock, token=token)
        old = tick_gate.read_lock_payload(self.lock)
        if old:
            old["time"] = time.time() - (self.ttl + 2)
            self.lock.write_text(__import__("json").dumps(old), encoding="utf-8")
        self.assertFalse(tick_gate.check_lock(self.lock))

    def test_start_detached_uses_neutral_cwd_and_resolved_lock_path(self):
        captured: dict = {}

        class FakeProc:
            pid = 424242

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return FakeProc()

        with patch("devloops_lib.lock_heartbeat.stop_heartbeat"), \
                patch("devloops_lib.lock_heartbeat.subprocess.Popen", side_effect=fake_popen), \
                patch("devloops_lib.lock_heartbeat.resolve_max_lifetime", return_value=12.0):
            start_detached(self.lock, "tok-123")

        self.assertEqual(captured["kwargs"]["cwd"], tempfile.gettempdir())
        self.assertNotEqual(Path(captured["kwargs"]["cwd"]).resolve(), self.loop_dir.resolve())
        lock_arg = captured["cmd"][captured["cmd"].index("--lock-file") + 1]
        self.assertEqual(Path(lock_arg), self.lock.resolve())
        self.assertEqual(captured["cmd"][captured["cmd"].index("--max-lifetime") + 1], "12.0")
        clear_sidecar(self.lock)

    def test_sidecar_io_retries_on_windows_permission_error(self):
        real_replace = os.replace
        replace_calls = 0

        def flaky_replace(src, dst):
            nonlocal replace_calls
            replace_calls += 1
            if replace_calls == 1:
                raise PermissionError(13, "sharing violation")
            return real_replace(src, dst)

        with patch("devloops_lib.lock_heartbeat.os.replace", side_effect=flaky_replace):
            write_sidecar(self.lock, 12345, "tok-abc")
        self.assertEqual(replace_calls, 2)

        real_read_text = Path.read_text
        read_calls = 0

        def flaky_read_text(path_self, *args, **kwargs):
            nonlocal read_calls
            read_calls += 1
            if read_calls == 1:
                raise PermissionError(13, "sharing violation")
            return real_read_text(path_self, *args, **kwargs)

        with patch.object(Path, "read_text", flaky_read_text):
            self.assertEqual(read_sidecar(self.lock), (12345, "tok-abc"))
        self.assertEqual(read_calls, 2)

        real_unlink = Path.unlink
        unlink_calls = 0

        def flaky_unlink(path_self, *args, **kwargs):
            nonlocal unlink_calls
            unlink_calls += 1
            if unlink_calls == 1:
                raise PermissionError(13, "sharing violation")
            return real_unlink(path_self, *args, **kwargs)

        with patch.object(Path, "unlink", flaky_unlink):
            clear_sidecar(self.lock)
        self.assertEqual(unlink_calls, 2)
        self.assertIsNone(read_sidecar(self.lock))


class LockTtlConfigTest(unittest.TestCase):
    def test_lock_ttl_reads_config(self):
        from devloops_lib import config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "devloops.config.yaml").write_text("worker:\n  lock_ttl_seconds: 99\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(tick_gate.LOCK_TTL_ENV, None)
                self.assertEqual(tick_gate.lock_ttl(config.load_config(start_dir=root)), 99)


if __name__ == "__main__":
    unittest.main()
