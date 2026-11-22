#!/usr/bin/env python3
"""Detached tick-lock renewer for slash-command ticks.

Started by ``tick_gate.py --lock``; renews every TTL/3 until the lock is lost,
SIGTERM, or the sidecar pid file is removed.
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

from devloops_lib.proc_util import terminate_process_tree

SIDECAR_IO_WAIT_SECONDS = 0.5
SIDECAR_RETRY_INTERVAL = 0.01


def _tick_gate():
    from devloops_lib import tick_gate as tg

    return tg


def _sidecar(lock_file: Path) -> Path:
    return lock_file.with_name(f"{lock_file.name}.heartbeat")


def write_sidecar(lock_file: Path, pid: int, token: str) -> Path:
    path = _sidecar(lock_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    tmp.write_text(f"{pid}\n{token}\n", encoding="utf-8")
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


def start_detached(lock_file: Path, token: str) -> None:
    lock_file = lock_file.resolve()
    stop_heartbeat(lock_file)
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
    proc = subprocess.Popen(
        [sys.executable, str(script), "--lock-file", str(lock_file), "--token", token],
        **popen_kw,
    )
    write_sidecar(lock_file, proc.pid, token)


def _run(lock_file: Path, token: str) -> int:
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
            time.sleep(min(0.5, interval / 4))
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock-file", type=Path, required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args(argv)
    if not _tick_gate().token_holds_lock(args.lock_file, args.token):
        return
    write_sidecar(args.lock_file, os.getpid(), args.token)
    try:
        raise SystemExit(_run(args.lock_file, args.token))
    finally:
        clear_sidecar(args.lock_file)


if __name__ == "__main__":
    main()
