#!/usr/bin/env python3
"""Tests for automatic agent slot check-ins."""
from __future__ import annotations

import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devloops_lib.config import AGENT_ID_ENV, get_state_path
from devloops_lib.sessions import (
    CheckinError,
    acquire_checkin,
    is_checkin_stale,
    load_checkins,
    release_checkin,
)


def _acquire_in_child(loop_dir: str, session: str, out: multiprocessing.Queue) -> None:
    try:
        env = {
            "LOOP_STATE_DIR": loop_dir,
            "DEVLOOPS_SESSION_ID": session,
            "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        }
        with patch.dict(os.environ, env, clear=False):
            out.put(acquire_checkin(None, Path(loop_dir) / "checkins.json", session=session))
    except Exception as exc:
        out.put(exc)


class SessionsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "worker-loop"
        self.root.mkdir(parents=True)
        self.checkins = self.root / "checkins.json"
        self.env = {"LOOP_STATE_DIR": str(self.root.parent / "worker-loop")}

    def test_two_sessions_get_distinct_slots(self):
        with patch.dict(os.environ, {"LOOP_STATE_DIR": str(self.root)}, clear=True):
            a = acquire_checkin(None, self.checkins, session="session-a", holder=1001)
            b = acquire_checkin(None, self.checkins, session="session-b", holder=1002)
        self.assertEqual(a, "default")
        self.assertEqual(b, "default-2")

    def test_stale_slot_reclaimed(self):
        stale = {"host": "x", "sessionKey": "gone", "startedAt": 1, "pid": 999999}
        self.checkins.write_text('{"default": ' + __import__("json").dumps(stale) + "}", encoding="utf-8")
        self.assertTrue(is_checkin_stale(stale))
        with patch.dict(os.environ, {"LOOP_STATE_DIR": str(self.root)}, clear=True):
            slot = acquire_checkin(None, self.checkins, session="new", holder=os.getpid())
        self.assertEqual(slot, "default")

    def test_explicit_agent_id_respected(self):
        with patch.dict(os.environ, {"LOOP_STATE_DIR": str(self.root)}, clear=True):
            slot = acquire_checkin("worker-a", self.checkins, session="s1", holder=1)
            self.assertEqual(slot, "worker-a")
            self.assertEqual(os.environ[AGENT_ID_ENV], "worker-a")

    def test_release_frees_slot(self):
        with patch.dict(os.environ, {"LOOP_STATE_DIR": str(self.root)}, clear=True):
            acquire_checkin(None, self.checkins, session="s1", holder=1)
            self.assertTrue(release_checkin("default", self.checkins, session="s1", holder=1))
            self.assertEqual(load_checkins(self.checkins), {})

    def test_concurrent_acquire_distinct_slots(self):
        ctx = multiprocessing.get_context("spawn")
        q: multiprocessing.Queue = ctx.Queue()
        root = str(self.root)
        procs = [
            ctx.Process(target=_acquire_in_child, args=(root, f"s{i}", q))
            for i in range(2)
        ]
        env_patch = patch.dict(os.environ, {"LOOP_STATE_DIR": root}, clear=False)
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)
            self.assertEqual(p.exitcode, 0)
        results = [q.get(timeout=5) for _ in procs]
        for item in results:
            self.assertIsInstance(item, str)
        self.assertNotEqual(results[0], results[1])

    def test_checkin_scopes_state_path(self):
        with patch.dict(os.environ, {"LOOP_STATE_DIR": str(self.root)}, clear=True):
            acquire_checkin(None, self.checkins, session="scoped", holder=42)
            path = get_state_path("worker-loop")
        self.assertIn("/agents/default/", str(path))


if __name__ == "__main__":
    unittest.main()
