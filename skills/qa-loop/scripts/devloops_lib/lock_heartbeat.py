#!/usr/bin/env python3
"""Detached tick-lock renewer for slash-command ticks.

Started by ``tick_gate.py --lock``; renews every TTL/3 until the lock is lost,
the watched parent process exits, SIGTERM, or the sidecar pid file is removed.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

_LIB_PARENT = str(Path(__file__).resolve().parent.parent)
if _LIB_PARENT not in sys.path:
    sys.path.insert(0, _LIB_PARENT)

from devloops_lib.proc_util import terminate_process_tree

SIDECAR_IO_WAIT_SECONDS = 0.5
SIDECAR_RETRY_INTERVAL = 0.01


def _tick_gate():
    from devloops_lib import tick_gate as tg

    return tg


def _sidecar(lock_file: Path) -> Path:
    return lock_file.with_name(f"{lock_file.name}.heartbeat")


def resolve_parent_pid(explicit: int | None = None) -> int | None:
    """Resolve the long-lived parent PID for the heartbeat to watch.

    - Explicit ``explicit`` (e.g. ``--pid``) wins when positive.
    - ``DEVLOOPS_SESSION_HOLDER_PID`` wins next when set and positive.
    - On Linux (``/proc``), walk up from ``os.getppid()``, skipping transient
      shells (``-c``, ``-lc``, ``/c``, etc.) and return the first non-transient
      ancestor with ``pid > 1``.
    - Returns ``None`` (no parent watch) on Windows, when ``/proc`` is
      unavailable, or when no ancestor with ``pid > 1`` can be determined.
    """
    if explicit is not None:
        return explicit if explicit > 0 else None
    env = os.environ.get("DEVLOOPS_SESSION_HOLDER_PID", "").strip()
    if env:
        try:
            val = int(env)
            return val if val > 0 else None
        except ValueError:
            pass
    if sys.platform == "win32":
        return None
    ppid = os.getppid()
    if ppid <= 1:
        return None
    from devloops_lib import sessions

    pid = ppid
    for _ in range(40):
        if pid <= 1:
            return None
        info = sessions._process_info(pid)
        if info is None:
            return None
        if sessions._is_transient_shell(info):
            pid = info.ppid
            continue
        return pid
    return None


def write_sidecar(lock_file: Path, pid: int, token: str, parent_pid: int | None = None) -> Path:
    path = _sidecar(lock_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    content = f"{pid}\n{token}\n" if parent_pid is None else f"{pid}\n{token}\n{parent_pid}\n"
    tmp.write_text(content, encoding="utf-8")
    deadline = time.monotonic() + SIDECAR_IO_WAIT_SECONDS
    try:
        while True:
            try:
                os.replace(str(tmp), str(path))
                return path
            except PermissionError:
                if time.monotonic() >= deadline:
                    return path
                time.sleep(SIDECAR_RETRY_INTERVAL)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def read_sidecar(lock_file: Path) -> tuple[int, str] | None:
    path = _sidecar(lock_file)
    deadline = time.monotonic() + SIDECAR_IO_WAIT_SECONDS
    while True:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) < 2:
                return None
            return int(lines[0]), lines[1].strip()
        except FileNotFoundError:
            return None
        except PermissionError:
            if time.monotonic() >= deadline:
                return None
            time.sleep(SIDECAR_RETRY_INTERVAL)
        except (OSError, ValueError):
            return None


def read_sidecar_parent_pid(lock_file: Path) -> int | None:
    path = _sidecar(lock_file)
    deadline = time.monotonic() + SIDECAR_IO_WAIT_SECONDS
    while True:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) < 3 or not lines[2].strip():
                return None
            return int(lines[2].strip())
        except FileNotFoundError:
            return None
        except PermissionError:
            if time.monotonic() >= deadline:
                return None
            time.sleep(SIDECAR_RETRY_INTERVAL)
        except (OSError, ValueError):
            return None


def clear_sidecar(lock_file: Path) -> None:
    path = _sidecar(lock_file)
    deadline = time.monotonic() + SIDECAR_IO_WAIT_SECONDS
    while True:
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                return
            time.sleep(SIDECAR_RETRY_INTERVAL)
        except OSError:
            return


def stop_heartbeat(lock_file: Path, *, wait_seconds: float = 10.0) -> None:
    info = read_sidecar(lock_file)
    clear_sidecar(lock_file)
    if not info:
        return
    pid, _token = info
    if pid == os.getpid():
        return
    terminate_process_tree(pid, wait_seconds=wait_seconds)


def start_detached(lock_file: Path, token: str, *, parent_pid: int | None = None) -> None:
    lock_file = lock_file.resolve()
    stop_heartbeat(lock_file)
    watch_pid = parent_pid if (parent_pid is not None and parent_pid > 0) else None
    script = Path(__file__).resolve()
    popen_kw: dict = {
        "cwd": tempfile.gettempdir(),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        popen_kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kw["start_new_session"] = True
    cmd = [sys.executable, str(script), "--lock-file", str(lock_file), "--token", token]
    if watch_pid is not None:
        cmd.extend(["--parent-pid", str(watch_pid)])
    proc = subprocess.Popen(cmd, **popen_kw)
    write_sidecar(lock_file, proc.pid, token, parent_pid=watch_pid)


def _run(lock_file: Path, token: str, *, parent_pid: int | None = None) -> int:
    tg = _tick_gate()
    interval = max(tg.lock_ttl() / 3, 1.0)
    stopping = False

    def _stop(*_args: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, _stop)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _stop)

    while not stopping:
        if parent_pid is not None and not tg._pid_alive(parent_pid):
            return 0
        if not tg.token_holds_lock(lock_file, token):
            sys.stderr.write(f"lock heartbeat: lost lock ({tg.describe_lock(lock_file)})\n")
            return 1
        if not tg.renew_lock(lock_file, token):
            sys.stderr.write(f"lock heartbeat: renew failed ({tg.describe_lock(lock_file)})\n")
            return 1
        if not _sidecar(lock_file).is_file():
            return 0
        deadline = time.monotonic() + interval
        while time.monotonic() < deadline and not stopping:
            if parent_pid is not None and not tg._pid_alive(parent_pid):
                return 0
            time.sleep(min(0.5, interval / 4))
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock-file", type=Path, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--parent-pid", type=int, default=None)
    args = parser.parse_args(argv)
    tg = _tick_gate()
    if args.parent_pid is not None and not tg._pid_alive(args.parent_pid):
        clear_sidecar(args.lock_file)
        return
    if not tg.token_holds_lock(args.lock_file, args.token):
        return
    write_sidecar(args.lock_file, os.getpid(), args.token, parent_pid=args.parent_pid)
    try:
        raise SystemExit(_run(args.lock_file, args.token, parent_pid=args.parent_pid))
    finally:
        clear_sidecar(args.lock_file)


if __name__ == "__main__":
    main()
