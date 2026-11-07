#!/usr/bin/env python3
"""Tests for producer gate (task selection, lock semantics, done marking)."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devloops_lib import producer
from devloops_lib.tick_gate import TOKEN_ENV, acquire_lock, acquire_lock_token, check_lock, read_lock_payload, release_lock

# Import CLI main
import importlib.util
_gate_path = REPO_ROOT / "skills" / "producer-loop" / "scripts" / "producer_gate.py"
_spec = importlib.util.spec_from_file_location("producer_gate_script", _gate_path)
_gate_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gate_mod)
producer_gate_main = _gate_mod.main


class ProducerGateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.state_file = self.tmp_dir / "state.json"
        self.lock_file = self.tmp_dir / "tick.lock"

    def test_due_task_selection_most_overdue(self):
        """Selects the task that is most overdue relative to its period."""
        now = 1000000.0
        cfg = {
            "producer": {
                "tasks": ["security", "cleanup", "doc-drift"],
                "default_period": "24h",  # 86400s
            }
        }
        # security ran 100,000s ago -> overdue by (100000 - 86400) = 13,600s
        # cleanup ran 200,000s ago -> overdue by (200000 - 86400) = 113,600s
        # doc-drift ran 10,000s ago -> not due yet
        state = {
            "lastRun": {
                "security": now - 100000,
                "cleanup": now - 200000,
                "doc-drift": now - 10000,
            }
        }
        selected = producer.select_next_task(cfg, state, now=now)
        self.assertEqual(selected, "cleanup")

    def test_noop_when_no_task_is_due(self):
        """Prints NOOP and exits with EXIT_NOOP (1) when no tasks are due."""
        current_time = time.time()
        # All tasks ran 10 seconds ago with 24h period
        state = {
            "lastRun": {
                t: current_time - 10 for t in producer.DEFAULT_TASKS
            }
        }
        self.state_file.write_text(json.dumps(state), encoding="utf-8")

        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            with patch.object(_gate_mod, "load_config") as mock_cfg:
                mock_cfg.return_value = {"producer": {"tasks": producer.DEFAULT_TASKS, "default_period": "24h"}}
                exit_code = producer_gate_main(["--next", "--state-file", str(self.state_file)])
                self.assertEqual(exit_code, producer.EXIT_NOOP)
                self.assertIn("NOOP", mock_out.getvalue().strip())

    def test_period_override_from_config_and_state(self):
        """Honors period overrides from config (task_periods) and state.json (taskPeriods)."""
        now = 1000000.0
        cfg = {
            "producer": {
                "tasks": ["security", "cleanup"],
                "default_period": "24h",
                "task_periods": {
                    "cleanup": "7d",  # 604800s
                },
            }
        }
        # cleanup ran 2 days ago (172800s) -> not due with 7d period
        # security ran 25 hours ago (90000s) -> due with 24h period
        state = {
            "lastRun": {
                "security": now - 90000,
                "cleanup": now - 172800,
            }
        }
        selected = producer.select_next_task(cfg, state, now=now)
        self.assertEqual(selected, "security")

        # Now override security in state.json to 48h
        state["taskPeriods"] = {"security": "48h"}  # 172800s
        # Now neither is due!
        selected_after_state_override = producer.select_next_task(cfg, state, now=now)
        self.assertIsNone(selected_after_state_override)

    def test_lock_acquire_busy_and_stale_release(self):
        """Verifies lock acquisition, BUSY response, stale lock release, and unlock."""
        # 1. Acquire lock
        acquire_lock(self.lock_file, pid=os.getpid())
        self.assertTrue(check_lock(self.lock_file))

        # 2. Trying to run with active lock returns EXIT_BUSY (2)
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            exit_code = producer_gate_main(["--lock", "--state-file", str(self.state_file)])
            self.assertEqual(exit_code, producer.EXIT_BUSY)
            self.assertIn("BUSY", mock_out.getvalue())

        # 3. Stale lock by dead PID
        release_lock(self.lock_file)
        dead_pid = 9999999
        acquire_lock(self.lock_file, pid=dead_pid)
        self.assertFalse(check_lock(self.lock_file))

        # Can now acquire because old lock was stale
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            exit_code = producer_gate_main(["--lock", "--state-file", str(self.state_file)])
            self.assertEqual(exit_code, producer.EXIT_RUN)
            self.assertTrue(check_lock(self.lock_file))

        # 4. Release lock with --unlock and the token printed by --lock
        self.assertIn(f"TOKEN {read_lock_payload(self.lock_file)['token']}", mock_out.getvalue())
        token = read_lock_payload(self.lock_file)["token"]
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            self.assertEqual(
                producer_gate_main(["--unlock", "--token", "not-mine", "--state-file", str(self.state_file)]),
                producer.EXIT_LOST,
            )
            self.assertTrue(check_lock(self.lock_file))
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            exit_code = producer_gate_main(["--unlock", "--token", token, "--state-file", str(self.state_file)])
            self.assertEqual(exit_code, producer.EXIT_RUN)
            self.assertFalse(check_lock(self.lock_file))
            self.assertIn("UNLOCKED", mock_out.getvalue())

    def test_task_without_lock_prints_task(self):
        """--task without --lock exits EXIT_RUN and prints the task name."""
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            exit_code = producer_gate_main(["--task", "security", "--state-file", str(self.state_file)])
            self.assertEqual(exit_code, producer.EXIT_RUN)
            self.assertEqual(mock_out.getvalue().strip(), "security")

    def test_task_with_lock_prints_task_and_locks(self):
        """--task with --lock exits EXIT_RUN, prints the task name, and acquires the lock."""
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            exit_code = producer_gate_main(["--task", "security", "--lock", "--state-file", str(self.state_file)])
            self.assertEqual(exit_code, producer.EXIT_RUN)
            token = read_lock_payload(self.lock_file)["token"]
            self.assertEqual(mock_out.getvalue().splitlines(), ["security", f"TOKEN {token}"])
            self.assertTrue(check_lock(self.lock_file))

    def test_agent_id_flag_scopes_the_lock(self):
        with patch.dict(os.environ, {"LOOP_STATE_DIR": str(self.tmp_dir)}), \
                patch("sys.stdout", new_callable=io.StringIO):
            exit_code = producer_gate_main(["--task", "security", "--lock", "--agent-id", "a"])
        self.assertEqual(exit_code, producer.EXIT_RUN)
        self.assertTrue(check_lock(self.tmp_dir / "producer-loop" / "agents" / "a" / "tick.lock"))

    def test_gate_under_wrapper_keeps_the_wrapper_lock(self):
        token = acquire_lock_token(self.lock_file, pid=os.getpid())
        before = self.lock_file.read_text(encoding="utf-8")
        with patch.dict(os.environ, {TOKEN_ENV: token}), \
                patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            self.assertEqual(
                producer_gate_main(["--task", "security", "--lock", "--state-file", str(self.state_file)]),
                producer.EXIT_RUN,
            )
            self.assertEqual(
                producer_gate_main(["--done", "security", "--unlock", "--state-file", str(self.state_file)]),
                producer.EXIT_RUN,
            )
            self.assertIn("LOCK KEPT", mock_out.getvalue())
        self.assertEqual(self.lock_file.read_text(encoding="utf-8"), before)

        with patch.dict(os.environ, {TOKEN_ENV: "earlier-wrapper"}), \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(
                producer_gate_main(["--task", "security", "--lock", "--state-file", str(self.state_file)]),
                producer.EXIT_BUSY,
            )

    def test_mark_task_done(self):
        """--done updates the task's lastRun timestamp in state.json."""
        state = {"lastRun": {"security": 100}}
        self.state_file.write_text(json.dumps(state), encoding="utf-8")

        before = int(time.time())
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            exit_code = producer_gate_main(["--done", "security", "--state-file", str(self.state_file)])
            self.assertEqual(exit_code, producer.EXIT_RUN)
            self.assertIn("DONE: security", mock_out.getvalue())

        loaded = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertGreaterEqual(loaded["lastRun"]["security"], before)


if __name__ == "__main__":
    unittest.main()
