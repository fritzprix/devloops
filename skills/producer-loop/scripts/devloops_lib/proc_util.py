"""Cross-platform helpers to terminate process trees (wrapper + shell + agent)."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time


def _windows_wait_pid(pid: int, timeout: float) -> None:
    import ctypes
    from ctypes import wintypes

    SYNCHRONIZE = 0x00100000
    kernel32 = ctypes.windll.kernel32
    h = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not h:
        return
    try:
        ms = int(timeout * 1000)
        kernel32.WaitForSingleObject(h, ms if ms > 0 else 0xFFFFFFFF)
    finally:
        kernel32.CloseHandle(h)


def terminate_process_tree(pid: int, *, wait_seconds: float = 30.0) -> None:
    """Terminate pid and its descendants; block until gone or timeout."""
    if pid <= 0:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            text=True,
            timeout=max(wait_seconds, 1.0),
            check=False,
        )
        _windows_wait_pid(pid, wait_seconds)
        return
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = pid
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except OSError:
            try:
                os.kill(pid, sig)
            except OSError:
                return
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return
            time.sleep(0.05)


def popen_agent(cmd: str, env: dict[str, str] | None) -> subprocess.Popen[bytes]:
    """Start the agent shell command; on Windows, in a group so taskkill /T can reap it."""
    kwargs: dict = {
        "shell": True,
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": None,
        "stderr": None,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(cmd, **kwargs)  # type: ignore[arg-type]
