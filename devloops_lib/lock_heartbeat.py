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


HEARTBEAT_LOG_NAME = "devloops-lock-heartbeat.log"


def heartbeat_log_path(lock_file: Path) -> Path:
    """Fixed diagnostic log file for heartbeat children.

    Lives in the system temp dir, NOT next to the lock: the child keeps this
    handle open through inherited stdout/stderr redirection, so a log inside
    the lock directory would block temp-dir cleanup on Windows
    (WinError 32) while any detached child is still alive. One shared file
    per machine is fine — every line is timestamped and the startup banner
    carries the lock path.
    """
    return Path(tempfile.gettempdir()) / HEARTBEAT_LOG_NAME


def _heartbeat_log_path(lock_file: Path) -> Path:
    return heartbeat_log_path(lock_file)


def _log_to_heartbeat_log(lock_file: Path, message: str) -> None:
    """Best-effort append to the heartbeat child's log file. Never raises."""
    try:
        path = _heartbeat_log_path(lock_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {message}\n")
    except OSError:
        pass


# Set by the heartbeat child in main() so _log() can find the lock-dir log
# file without threading the path through every call site.
_CHILD_LOCK_FILE: "Path | None" = None


def _log(message: str) -> None:
    """Log from the heartbeat child: stderr plus the lock-dir log file.

    ``start_detached`` redirects the child's stdout/stderr into the log file,
    so stderr writes alone are invisible on Windows otherwise; the direct file
    write guarantees the evidence survives even if redirection failed.
    """
    try:
        sys.stderr.write(message + "\n")
    except Exception:
        pass
    if _CHILD_LOCK_FILE is not None:
        _log_to_heartbeat_log(_CHILD_LOCK_FILE, message)


# Child startup can briefly race the parent's lock write; retry a few times
# before deciding the lock is not ours.
STARTUP_LOCK_RETRY_COUNT = 3
STARTUP_LOCK_RETRY_INTERVAL = 0.2


# Ancestor names skipped on Windows: the shells/harnesses that host a tick and
# exit right after spawning it. Their parent (the agent or terminal host) is the
# long-lived process worth watching.
_WINDOWS_TRANSIENT_EXES = frozenset({"cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe"})


def _windows_process_map() -> "dict[int, tuple[int, str]] | None":
    """Map pid -> (parent_pid, exe basename lower) via a Toolhelp32 snapshot.

    Returns None when the snapshot cannot be taken or read, or when its data
    looks unreliable (the calling process is missing from it). Never raises:
    a snapshot problem must degrade to "no parent watch", never crash the
    caller (tick_gate --lock) or hand out garbage pids.
    """
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        TH32CS_SNAPPROCESS = 0x00000002
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        invalid = ctypes.cast(ctypes.c_void_p(-1), ctypes.c_void_p).value
        if not snapshot or ctypes.cast(snapshot, ctypes.c_void_p).value == invalid:
            return None
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        procs: dict = {}
        try:
            if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                return None
            while True:
                procs[int(entry.th32ProcessID)] = (
                    int(entry.th32ParentProcessID),
                    entry.szExeFile.rsplit("\\", 1)[-1].lower(),
                )
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
        finally:
            kernel32.CloseHandle(snapshot)
        # Sanity: a snapshot that does not even see this process has layout or
        # permission problems; its pids are not trustworthy.
        if os.getpid() not in procs:
            return None
        return procs
    except Exception:
        return None


def _resolve_parent_pid_windows() -> int | None:
    """Walk up from os.getppid() with a Toolhelp32 snapshot.

    Skips transient shells (cmd/powershell/pwsh/conhost) and returns the first
    other ancestor that is still alive. Returns None (no parent watch) when the
    snapshot is unavailable, an ancestor is not visible in it, or the candidate
    is not alive — a wrong pid would make the heartbeat exit early and stop
    renewing a live tick's lock, which is worse than no watch.
    """
    try:
        pid = os.getppid()
        if pid <= 0:
            return None
        procs = _windows_process_map()
        if procs is None:
            return None
        for _ in range(40):
            # 0 = Idle, 4 = System: never watch them.
            if pid <= 0 or pid in (0, 4):
                return None
            info = procs.get(pid)
            if info is None:
                return None
            ppid, exe = info
            if exe in _WINDOWS_TRANSIENT_EXES:
                pid = ppid
                continue
            if not _tick_gate()._pid_alive(pid):
                return None
            return pid
        return None
    except Exception:
        return None


def resolve_parent_pid(explicit: int | None = None) -> int | None:
    """Resolve the long-lived parent PID for the heartbeat to watch.

    - Explicit ``explicit`` (e.g. ``--pid``) wins when positive.
    - ``DEVLOOPS_SESSION_HOLDER_PID`` wins next when set and positive.
    - On Linux (``/proc``), walk up from ``os.getppid()``, skipping transient
      shells (``-c``, ``-lc``, ``/c``, etc.) and return the first non-transient
      ancestor with ``pid > 1``.
    - On Windows, walk up the Toolhelp32 snapshot, skipping
      ``cmd/powershell/pwsh/conhost`` by exe name, and return the first other
      ancestor.
    - Returns ``None`` (no parent watch) when ``/proc`` is unavailable on
      Linux, when the snapshot or an ancestor is not visible on Windows, or
      when no watchable ancestor can be determined.
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
        return _resolve_parent_pid_windows()
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
    # A child that was still booting can write its own sidecar between the
    # clear above and the kill; clear again after the process is gone (a
    # force-killed child never runs its finally-clear) so stopping a
    # heartbeat never leaves a stale sidecar behind.
    clear_sidecar(lock_file)


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
    # Capture the child's output into a log file next to the sidecar so
    # Windows startup failures (previously DEVNULL: zero evidence) are
    # diagnosable from CI logs.
    log_file = None
    try:
        log_path = _heartbeat_log_path(lock_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "ab")
        popen_kw["stdout"] = log_file
        popen_kw["stderr"] = log_file
    except OSError:
        log_file = None
    if sys.platform == "win32":
        popen_kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kw["start_new_session"] = True
    cmd = [sys.executable, str(script), "--lock-file", str(lock_file), "--token", token]
    if watch_pid is not None:
        cmd.extend(["--parent-pid", str(watch_pid)])
    try:
        proc = subprocess.Popen(cmd, **popen_kw)
    finally:
        if log_file is not None:
            try:
                log_file.close()
            except OSError:
                pass
    _log_to_heartbeat_log(
        lock_file,
        f"heartbeat: spawned child pid={proc.pid} parent_pid={watch_pid} lock={lock_file}",
    )
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
            _log(f"heartbeat: parent {parent_pid} exited; stopping")
            return 0
        if not tg.token_holds_lock(lock_file, token):
            _log(f"heartbeat: lost lock ({tg.describe_lock(lock_file)})")
            return 1
        if not tg.renew_lock(lock_file, token):
            _log(f"heartbeat: renew failed ({tg.describe_lock(lock_file)})")
            return 1
        if not _sidecar(lock_file).is_file():
            _log("heartbeat: sidecar removed; stopping")
            return 0
        deadline = time.monotonic() + interval
        while time.monotonic() < deadline and not stopping:
            if parent_pid is not None and not tg._pid_alive(parent_pid):
                _log(f"heartbeat: parent {parent_pid} exited; stopping")
                return 0
            time.sleep(min(0.5, interval / 4))
    return 0


def main(argv: list[str] | None = None) -> None:
    global _CHILD_LOCK_FILE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock-file", type=Path, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--parent-pid", type=int, default=None)
    args = parser.parse_args(argv)
    _CHILD_LOCK_FILE = args.lock_file
    _log(
        f"heartbeat: start pid={os.getpid()} ppid={os.getppid()} "
        f"parent_pid={args.parent_pid} lock={args.lock_file} "
        f"platform={sys.platform} py={sys.version.split()[0]} exe={sys.executable}"
    )
    tg = _tick_gate()
    if args.parent_pid is not None and not tg._pid_alive(args.parent_pid):
        # A resolved parent that is already gone when the child boots is a
        # launcher artifact, not a dead session: on Windows the venv
        # python.exe redirector spawns tick_gate and exits right after it,
        # and tick_gate resolves that redirector as the watch parent.
        # Exiting here would kill every heartbeat on Windows; drop the
        # watch instead — sidecar removal, the TTL and --unlock still stop
        # the child.
        _log(
            f"heartbeat: parent {args.parent_pid} not alive at startup; "
            "continuing without parent watch"
        )
        args.parent_pid = None
    holds = False
    for attempt in range(STARTUP_LOCK_RETRY_COUNT):
        if tg.token_holds_lock(args.lock_file, args.token):
            holds = True
            break
        if attempt + 1 < STARTUP_LOCK_RETRY_COUNT:
            time.sleep(STARTUP_LOCK_RETRY_INTERVAL)
    if not holds:
        _log(
            f"heartbeat: token does not hold lock after "
            f"{STARTUP_LOCK_RETRY_COUNT} attempts ({tg.describe_lock(args.lock_file)}); exiting"
        )
        return
    write_sidecar(args.lock_file, os.getpid(), args.token, parent_pid=args.parent_pid)
    _log("heartbeat: startup checks passed; entering renew loop")
    try:
        raise SystemExit(_run(args.lock_file, args.token, parent_pid=args.parent_pid))
    finally:
        clear_sidecar(args.lock_file)


if __name__ == "__main__":
    main()
