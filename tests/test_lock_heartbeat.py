#!/usr/bin/env python3
"""Tests for detached tick lock heartbeat."""
from __future__ import annotations

import os
import signal
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
    read_sidecar_parent_pid,
    resolve_parent_pid,
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

    def _hb_log_tail(self, lines: int = 60) -> str:
        """Tail of the shared heartbeat diagnostic log, for failure messages.

        The log is global per machine, so it may contain other tests' lines;
        startup/spawn banners carry the lock path for filtering.
        """
        from devloops_lib.lock_heartbeat import heartbeat_log_path

        try:
            content = heartbeat_log_path(self.lock).read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError as exc:
            return f"<no heartbeat log: {exc}>"
        return "\n".join(content.splitlines()[-lines:]) or "<empty>"

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
        age = time.time() - payload["time"]
        self.assertLessEqual(age, self.ttl, f"lock not renewed (age {age:.2f}s > ttl {self.ttl}); heartbeat log tail:\n{self._hb_log_tail()}")
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

    def test_start_detached_uses_neutral_cwd_and_resolved_lock_path(self):
        captured: dict = {}

        class FakeProc:
            pid = 424242

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return FakeProc()

        with patch("devloops_lib.lock_heartbeat.stop_heartbeat"), \
                patch("devloops_lib.lock_heartbeat.subprocess.Popen", side_effect=fake_popen):
            start_detached(self.lock, "tok-123")

        self.assertEqual(captured["kwargs"]["cwd"], tempfile.gettempdir())
        self.assertNotEqual(Path(captured["kwargs"]["cwd"]).resolve(), self.loop_dir.resolve())
        lock_arg = captured["cmd"][captured["cmd"].index("--lock-file") + 1]
        self.assertEqual(Path(lock_arg), self.lock.resolve())
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

    def test_heartbeat_exits_and_lock_expires_when_parent_killed(self):
        # Spawn a parent process that runs `tick_gate.py --lock` and stays alive until killed.
        # The process announces its real pid and pins itself as the session holder:
        # on Windows the Popen'd process is the venv python.exe launcher that forks
        # the actual interpreter, so parent.pid alone is not the watch target.
        parent_code = (
            "import os, subprocess, sys, time; "
            "print(f'HOLDER {os.getpid()}', flush=True); "
            "os.environ['DEVLOOPS_SESSION_HOLDER_PID'] = str(os.getpid()); "
            f"r = subprocess.run([sys.executable, {str(SCRIPTS / 'tick_gate.py')!r}, '--lock'], "
            "capture_output=True, text=True); "
            "print(r.stdout, flush=True); "
            "time.sleep(60)"
        )
        parent = subprocess.Popen(
            [sys.executable, "-c", parent_code],
            cwd=str(self.loop_dir),
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: (parent.kill(), parent.wait()) if parent.poll() is None else None)
        self.addCleanup(lambda: stop_heartbeat(self.lock))

        token = None
        holder = None
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and (token is None or holder is None):
            line = parent.stdout.readline()
            if line.startswith("TOKEN "):
                token = line.split()[1].strip()
            elif line.startswith("HOLDER "):
                holder = int(line.split()[1].strip())
        self.assertIsNotNone(token, "parent did not emit TOKEN")
        self.assertIsNotNone(holder, "parent did not emit HOLDER")

        def _kill_holder() -> None:
            try:
                os.kill(holder, signal.SIGTERM)
            except OSError:
                pass

        self.addCleanup(_kill_holder)

        # Wait briefly for heartbeat sidecar to appear with parent_pid recorded.
        hb_info = None
        hb_parent = None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            hb_info = read_sidecar(self.lock)
            hb_parent = read_sidecar_parent_pid(self.lock)
            if hb_info and hb_parent == holder:
                break
            time.sleep(0.1)
        self.assertIsNotNone(
            hb_info,
            f"heartbeat sidecar never appeared within 5s; heartbeat log tail:\n{self._hb_log_tail()}",
        )
        self.assertEqual(hb_parent, holder)
        hb_pid = hb_info[0]
        self.assertTrue(tick_gate._pid_alive(hb_pid))

        # The sidecar is written by the parent at spawn time, before the
        # detached child finishes booting; wait until the child has
        # actually renewed the lock once so killing the holder races
        # neither its startup check nor a not-yet-started renew loop.
        initial_time = tick_gate.read_lock_payload(self.lock)["time"]
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            payload = tick_gate.read_lock_payload(self.lock)
            if payload and payload["time"] > initial_time:
                break
            time.sleep(0.1)
        payload = tick_gate.read_lock_payload(self.lock)
        self.assertGreater(
            payload["time"],
            initial_time,
            f"heartbeat never renewed the lock; heartbeat log tail:\n{self._hb_log_tail()}",
        )

        # Kill the session holder without calling --unlock; the heartbeat
        # must detect it and exit (the launcher parent dies too).
        _kill_holder()
        parent.kill()
        parent.wait(timeout=5)

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and tick_gate._pid_alive(hb_pid):
            time.sleep(0.1)
        self.assertFalse(tick_gate._pid_alive(hb_pid), "heartbeat did not exit after parent died")
        self.assertIsNone(read_sidecar(self.lock))

        # Once the heartbeat has stopped, the lock expires within the TTL.
        time.sleep(self.ttl + 0.5)
        self.assertFalse(tick_gate.check_lock(self.lock, ttl_seconds=self.ttl))

    def test_unlock_stops_heartbeat_and_leaves_no_process(self):
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
        hb_info = read_sidecar(self.lock)
        self.assertIsNotNone(hb_info)
        hb_pid = hb_info[0]
        self.assertTrue(tick_gate._pid_alive(hb_pid))

        unlock = subprocess.run(
            [sys.executable, str(SCRIPTS / "tick_gate.py"), "--unlock", "--token", token],
            cwd=str(self.loop_dir),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(unlock.returncode, tick_gate.EXIT_RUN, unlock.stdout + unlock.stderr)
        self.assertFalse(tick_gate._pid_alive(hb_pid))
        self.assertIsNone(read_sidecar(self.lock))

    def test_resolve_parent_pid_skips_transient_shells(self):
        from devloops_lib import sessions

        procs = {
            300: sessions.ProcessInfo(comm="bash", cmdline=["bash", "-c", "python3 tick_gate.py --lock"], ppid=200),
            301: sessions.ProcessInfo(comm="bash", cmdline=["bash", "-lc", "python3 tick_gate.py --lock"], ppid=200),
            200: sessions.ProcessInfo(comm="claude", cmdline=["claude"], ppid=1),
        }
        with patch("devloops_lib.sessions._process_info", side_effect=lambda p: procs.get(p)), \
                patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEVLOOPS_SESSION_HOLDER_PID", None)
            if sys.platform != "win32":
                with patch("devloops_lib.lock_heartbeat.os.getppid", return_value=300):
                    self.assertEqual(resolve_parent_pid(), 200)
                with patch("devloops_lib.lock_heartbeat.os.getppid", return_value=301):
                    self.assertEqual(resolve_parent_pid(), 200)

    def test_resolve_parent_pid_returns_none_without_proc_or_on_windows(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEVLOOPS_SESSION_HOLDER_PID", None)
            with patch("devloops_lib.sessions._process_info", return_value=None), \
                    patch("devloops_lib.lock_heartbeat.os.getppid", return_value=300):
                self.assertIsNone(resolve_parent_pid())
            with patch("devloops_lib.lock_heartbeat.sys.platform", "win32"), \
                    patch("devloops_lib.lock_heartbeat._windows_process_map", return_value=None), \
                    patch("devloops_lib.lock_heartbeat.os.getppid", return_value=300):
                self.assertIsNone(resolve_parent_pid())
            with patch("devloops_lib.lock_heartbeat.os.getppid", return_value=1):
                self.assertIsNone(resolve_parent_pid())

    def test_resolve_parent_pid_windows_skips_transient_shells(self):
        procs = {
            300: (200, "cmd.exe"),
            201: (200, "conhost.exe"),
            302: (200, "bash.exe"),
            303: (200, "sh.exe"),
            200: (4, "agent.exe"),
        }
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEVLOOPS_SESSION_HOLDER_PID", None)
            with patch("devloops_lib.lock_heartbeat.sys.platform", "win32"), \
                    patch("devloops_lib.lock_heartbeat._windows_process_map", return_value=procs), \
                    patch("devloops_lib.tick_gate._pid_alive", return_value=True):
                with patch("devloops_lib.lock_heartbeat.os.getppid", return_value=300):
                    self.assertEqual(resolve_parent_pid(), 200)
                with patch("devloops_lib.lock_heartbeat.os.getppid", return_value=201):
                    self.assertEqual(resolve_parent_pid(), 200)
                # G4.1: per-call POSIX shells on Windows (bash.exe / sh.exe)
                # are transient too — never watch them.
                with patch("devloops_lib.lock_heartbeat.os.getppid", return_value=302):
                    self.assertEqual(resolve_parent_pid(), 200)
                with patch("devloops_lib.lock_heartbeat.os.getppid", return_value=303):
                    self.assertEqual(resolve_parent_pid(), 200)
                # An ancestor that is not visible in the snapshot: no watch.
                with patch("devloops_lib.lock_heartbeat.os.getppid", return_value=999):
                    self.assertIsNone(resolve_parent_pid())
                # System pid is never watched.
                with patch("devloops_lib.lock_heartbeat.os.getppid", return_value=4):
                    self.assertIsNone(resolve_parent_pid())
            # A candidate pid that is not alive is never watched (garbage
            # snapshot data must degrade to no watch, not early heartbeat exit).
            with patch("devloops_lib.lock_heartbeat.sys.platform", "win32"), \
                    patch("devloops_lib.lock_heartbeat._windows_process_map", return_value=procs), \
                    patch("devloops_lib.tick_gate._pid_alive", return_value=False), \
                    patch("devloops_lib.lock_heartbeat.os.getppid", return_value=300):
                self.assertIsNone(resolve_parent_pid())

    def test_resolve_parent_pid_windows_skips_venv_python_launcher(self):
        # G2.1/G3.1: inside a venv, skip the Scripts\python.exe redirector and
        # watch the session host.
        procs = {
            400: (300, "python.exe"),  # venv launcher (immediate parent)
            300: (4, "agent.exe"),     # long-lived session host
        }
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEVLOOPS_SESSION_HOLDER_PID", None)
            with patch("devloops_lib.lock_heartbeat.sys.platform", "win32"), \
                    patch("devloops_lib.lock_heartbeat._windows_process_map", return_value=procs), \
                    patch("devloops_lib.tick_gate._pid_alive", return_value=True), \
                    patch("devloops_lib.lock_heartbeat.os.getppid", return_value=400), \
                    patch("devloops_lib.lock_heartbeat._running_in_venv", return_value=True), \
                    patch("devloops_lib.lock_heartbeat.sys.executable",
                          r"C:\venv\Scripts\python.exe"):
                self.assertEqual(resolve_parent_pid(), 300)

    def test_resolve_parent_pid_windows_keeps_python_session_host(self):
        # G3.1: outside a venv, a python.exe ancestor is the session host and
        # must stay watched (explorer → cmd → python → bash → tick_gate).
        procs = {
            500: (400, "bash.exe"),
            400: (300, "python.exe"),  # Python-based agent host
            300: (200, "cmd.exe"),
            200: (4, "explorer.exe"),
        }
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEVLOOPS_SESSION_HOLDER_PID", None)
            with patch("devloops_lib.lock_heartbeat.sys.platform", "win32"), \
                    patch("devloops_lib.lock_heartbeat._windows_process_map", return_value=procs), \
                    patch("devloops_lib.tick_gate._pid_alive", return_value=True), \
                    patch("devloops_lib.lock_heartbeat.os.getppid", return_value=500), \
                    patch("devloops_lib.lock_heartbeat._running_in_venv", return_value=False), \
                    patch("devloops_lib.lock_heartbeat.sys.executable",
                          r"C:\Python313\python.exe"):
                self.assertEqual(resolve_parent_pid(), 400)

    def test_start_detached_none_parent_pid_disables_parent_watch(self):
        captured: dict = {}

        class FakeProc:
            pid = 424243

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            return FakeProc()

        with patch("devloops_lib.lock_heartbeat.stop_heartbeat"), \
                patch("devloops_lib.lock_heartbeat.subprocess.Popen", side_effect=fake_popen):
            start_detached(self.lock, "tok-none", parent_pid=None)

        self.assertNotIn("--parent-pid", captured["cmd"])
        self.assertIsNone(read_sidecar_parent_pid(self.lock))
        clear_sidecar(self.lock)

    def test_heartbeat_starts_without_pythonpath_in_installed_layout(self):
        env_no_pypath = {k: v for k, v in self.env.items() if k != "PYTHONPATH"}
        first = subprocess.run(
            [sys.executable, str(SCRIPTS / "tick_gate.py"), "--lock"],
            cwd=str(self.loop_dir),
            env=env_no_pypath,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(first.returncode, tick_gate.EXIT_RUN, first.stdout + first.stderr)
        token = [ln.split()[1] for ln in first.stdout.splitlines() if ln.startswith("TOKEN ")][0]
        self.addCleanup(lambda: stop_heartbeat(self.lock))
        time.sleep(self.ttl + 1)
        payload = tick_gate.read_lock_payload(self.lock)
        self.assertIsNotNone(payload)
        age = time.time() - payload["time"]
        self.assertLessEqual(age, self.ttl, f"lock not renewed without PYTHONPATH (age {age:.2f}s > ttl {self.ttl}); heartbeat log tail:\n{self._hb_log_tail()}")
        self.assertTrue(tick_gate.lock_is_owned(self.lock, token))
        unlock = subprocess.run(
            [sys.executable, str(SCRIPTS / "tick_gate.py"), "--unlock", "--token", token],
            cwd=str(self.loop_dir),
            env=env_no_pypath,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(unlock.returncode, tick_gate.EXIT_RUN, unlock.stdout + unlock.stderr)


    def test_watch_parent_dead_at_startup_exits_without_sidecar(self):
        # Owner decision (2026-11-26, #78): a watched parent that is already
        # gone when the child boots must exit, never continue without a
        # parent watch (no sidecar, no renewing).
        from devloops_lib import lock_heartbeat

        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait(timeout=10)
        token = tick_gate.acquire_lock_token(self.lock)
        try:
            lock_heartbeat.main([
                "--lock-file", str(self.lock), "--token", token,
                "--parent-pid", str(dead.pid),
            ])
            self.assertIsNone(read_sidecar(self.lock),
                              "heartbeat wrote a sidecar despite a dead watched parent")
            payload = tick_gate.read_lock_payload(self.lock)
            self.assertEqual(payload.get("token"), token)
        finally:
            tick_gate.release_lock(self.lock, token=token)

    def test_start_detached_dead_parent_clears_spawn_sidecar(self):
        # G1.5: start_detached writes a sidecar before the child runs; when the
        # child exits immediately (dead watched parent), that sidecar must not
        # remain for stop_heartbeat to target a reused PID.
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait(timeout=10)
        token = tick_gate.acquire_lock_token(self.lock)
        try:
            start_detached(self.lock, token, parent_pid=dead.pid)
            # Poll briefly for the child exit + parent-side clear.
            deadline = time.time() + 5
            while time.time() < deadline and read_sidecar(self.lock) is not None:
                time.sleep(0.05)
            self.assertIsNone(read_sidecar(self.lock),
                              f"spawn sidecar remained after dead-parent exit; log:\n{self._hb_log_tail()}")
        finally:
            stop_heartbeat(self.lock)
            tick_gate.release_lock(self.lock, token=token)

    @unittest.skipUnless(sys.platform.startswith("linux"), "automatic parent resolution needs /proc")
    def test_heartbeat_exits_when_auto_resolved_parent_killed(self):
        # Owner decision (2026-11-26, #78): exercise automatic parent
        # resolution (Linux /proc walk from os.getppid()) without pinning
        # DEVLOOPS_SESSION_HOLDER_PID.
        env = {k: v for k, v in self.env.items() if k != "DEVLOOPS_SESSION_HOLDER_PID"}
        parent_code = (
            "import os, subprocess, sys, time; "
            "print(f'HOLDER {os.getpid()}', flush=True); "
            f"r = subprocess.run([sys.executable, {str(SCRIPTS / 'tick_gate.py')!r}, '--lock'], "
            "capture_output=True, text=True); "
            "print(r.stdout, flush=True); "
            "time.sleep(60)"
        )
        parent = subprocess.Popen(
            [sys.executable, "-c", parent_code],
            cwd=str(self.loop_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: (parent.kill(), parent.wait()) if parent.poll() is None else None)
        self.addCleanup(lambda: stop_heartbeat(self.lock))

        token = None
        holder = None
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and (token is None or holder is None):
            line = parent.stdout.readline()
            if not line:
                break
            if line.startswith("TOKEN "):
                token = line.split()[1].strip()
            elif line.startswith("HOLDER "):
                holder = int(line.split()[1].strip())
        self.assertIsNotNone(token, "parent did not emit TOKEN")
        self.assertIsNotNone(holder, "parent did not emit HOLDER")

        # The parent Popen'd tick_gate directly, so the auto resolver must
        # watch exactly the holder pid — no env pinning involved.
        hb_info = None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            hb_info = read_sidecar(self.lock)
            if hb_info and read_sidecar_parent_pid(self.lock) == holder:
                break
            time.sleep(0.1)
        self.assertIsNotNone(hb_info, "heartbeat sidecar never appeared")
        self.assertEqual(read_sidecar_parent_pid(self.lock), holder,
                         "auto resolver did not watch the direct parent")
        hb_pid = hb_info[0]

        initial_time = tick_gate.read_lock_payload(self.lock)["time"]
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            payload = tick_gate.read_lock_payload(self.lock)
            if payload and payload["time"] > initial_time:
                break
            time.sleep(0.1)
        self.assertGreater(tick_gate.read_lock_payload(self.lock)["time"], initial_time,
                           "heartbeat never renewed the lock")

        try:
            os.kill(holder, signal.SIGTERM)
        except OSError:
            pass
        parent.kill()
        parent.wait(timeout=5)

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and tick_gate._pid_alive(hb_pid):
            time.sleep(0.1)
        self.assertFalse(tick_gate._pid_alive(hb_pid), "heartbeat did not exit after the auto-watched parent died")
        self.assertIsNone(read_sidecar(self.lock))
        time.sleep(self.ttl + 0.5)
        self.assertFalse(tick_gate.check_lock(self.lock, ttl_seconds=self.ttl))


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
