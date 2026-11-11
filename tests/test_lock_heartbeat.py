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
from devloops_lib.lock_heartbeat import start_detached, stop_heartbeat

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

    def test_heartbeat_stops_after_unlock_and_lock_expires(self):
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
