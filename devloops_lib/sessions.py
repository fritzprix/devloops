"""Automatic loop slot check-in when no --agent-id is set.

Parallel harness sessions (e.g. LibrAgent and Claude Code on the same repo)
each claim a distinct agent id (`default`, `default-2`, ...) under
`<state_dir>/<loop>/checkins.json`, set ``DEVLOOPS_AGENT_ID``, and release on
``--unlock`` or idle ``NOOP``.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from devloops_lib.config import AGENT_ID_ENV, validate_agent_id

DEFAULT_SLOT = "default"
SLOT_NAME = re.compile(r"^default(?:-\d+)?$")
CHECKIN_TTL_SECONDS = 1800
MAX_AUTO_SLOTS = 32
_HOLDER_ENV = "DEVLOOPS_SESSION_HOLDER_PID"

_SESSION_ID_ENVS = (
    "DEVLOOPS_SESSION_ID",
    "WORKER_LOOP_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_TERMINAL_SESSION_ID",
    "CURSOR_AGENT_SESSION_ID",
    "CURSOR_TRACE_ID",
)

_TRANSIENT_SHELLS = frozenset({"bash", "sh", "dash", "zsh", "fish", "ksh"})


class CheckinError(RuntimeError):
    pass


class _UnsetType:
    pass


_HOLDER_UNSET = _UnsetType()


def session_key() -> str:
    """Stable id for this harness session (env vars first, then per-process fallback)."""
    for name in _SESSION_ID_ENVS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return f"proc-{os.getpid()}-{uuid.uuid4().hex[:12]}"


def _host() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "unknown"


@dataclass(frozen=True)
class ProcessInfo:
    comm: str
    cmdline: list[str]
    ppid: int


_process_info_lookup: Callable[[int], ProcessInfo | None] | None = None


def _linux_ppid(pid: int) -> int:
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().split()[3])
    except (OSError, ValueError, IndexError):
        return 0


def _read_cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        return [part.decode(errors="replace") for part in raw.split(b"\0") if part]
    except OSError:
        return []


def _process_info(pid: int) -> ProcessInfo | None:
    if pid <= 0:
        return None
    if _process_info_lookup is not None:
        return _process_info_lookup(pid)
    try:
        comm = Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return None
    return ProcessInfo(comm=comm, cmdline=_read_cmdline(pid), ppid=_linux_ppid(pid))


def _comm_base(comm: str) -> str:
    return comm.rsplit("/", 1)[-1].lower()


def _is_transient_shell(info: ProcessInfo) -> bool:
    base = _comm_base(info.comm)
    if base not in _TRANSIENT_SHELLS and not base.startswith("bash"):
        return False
    lowered = [arg.lower() for arg in info.cmdline]
    return "-c" in info.cmdline or "/c" in lowered


def _is_ephemeral_runner(info: ProcessInfo) -> bool:
    base = _comm_base(info.comm)
    joined = " ".join(info.cmdline).lower()
    if base.startswith("python"):
        return "tick_gate" in joined or "lock_heartbeat" in joined
    if base.startswith("node"):
        return True
    return base.startswith("ruby")


def _is_trusted_session_holder(info: ProcessInfo) -> bool:
    joined = " ".join(info.cmdline).lower()
    base = _comm_base(info.comm)
    return "libragent" in joined or base == "libragent"


def resolve_holder_pid(explicit: int | None = None) -> int | None:
    """Long-lived session PID, or None → check-in uses session TTL only (no pid field)."""
    if explicit is not None:
        return explicit
    env = os.environ.get(_HOLDER_ENV)
    if env:
        return int(env)
    if sys.platform == "win32":
        return None

    pid = os.getppid()
    for _ in range(40):
        info = _process_info(pid)
        if info is None:
            break
        if _is_transient_shell(info):
            pid = info.ppid
            continue
        if _is_ephemeral_runner(info):
            pid = info.ppid
            continue
        if _is_trusted_session_holder(info):
            return pid
        pid = info.ppid
    return None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def checkin_record(
    session: str,
    holder_pid: int | None,
    *,
    started_at: int | None = None,
) -> dict:
    now = int(time.time())
    record: dict = {
        "host": _host(),
        "sessionKey": session,
        "startedAt": started_at if started_at is not None else now,
    }
    if holder_pid is not None:
        record["pid"] = holder_pid
    return record


def is_checkin_stale(record: dict | None) -> bool:
    if not record or not isinstance(record, dict):
        return True
    started = int(record.get("startedAt") or 0)
    pid = record.get("pid")
    if pid is None:
        return time.time() - started > CHECKIN_TTL_SECONDS
    if sys.platform == "win32":
        return time.time() - started > CHECKIN_TTL_SECONDS
    if not _pid_alive(int(pid)):
        return True
    return False


def _same_checkin_holder(record: dict, session: str, holder: int | None) -> bool:
    if session and record.get("sessionKey") == session:
        return True
    rec_pid = record.get("pid")
    if holder is None:
        return rec_pid is None and record.get("sessionKey") == session
    return rec_pid == holder


def iter_slot_names(existing: dict | None = None) -> list[str]:
    names = [DEFAULT_SLOT] + [f"default-{n}" for n in range(2, MAX_AUTO_SLOTS + 1)]
    if existing:
        extra = sorted(k for k in existing if SLOT_NAME.match(k) and k not in names)
        names.extend(extra)
    return names


def checkins_path(loop_name: str, loop_dir: Path) -> Path:
    return loop_dir / "checkins.json"


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_checkins(path: Path) -> dict:
    return _read_json(path)


def find_slot_by_session(session: str, path: Path) -> str | None:
    for name, record in load_checkins(path).items():
        if isinstance(record, dict) and record.get("sessionKey") == session:
            return name
    return None


def find_slot_by_holder_pid(holder_pid: int | None, path: Path) -> str | None:
    if holder_pid is None:
        return None
    for name, record in load_checkins(path).items():
        if isinstance(record, dict) and record.get("pid") == holder_pid:
            return name
    return None


@contextlib.contextmanager
def _exclusive(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 30.0
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise CheckinError(f"check-in lock busy: {lock_path}")
            time.sleep(0.01)
    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp.{os.getpid()}.{time.time_ns()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def acquire_checkin(
    explicit: str | None,
    path: Path,
    *,
    session: str | None = None,
    holder: int | None | object = _HOLDER_UNSET,
) -> str:
    """Claim a slot; set ``DEVLOOPS_AGENT_ID``. Explicit id wins over auto."""
    if holder is _HOLDER_UNSET:
        resolved: int | None = resolve_holder_pid(None)
    else:
        resolved = holder  # type: ignore[assignment]
    sess = session or session_key()
    if "DEVLOOPS_SESSION_ID" not in os.environ:
        os.environ["DEVLOOPS_SESSION_ID"] = sess

    with _exclusive(path.with_name(path.name + ".lock")):
        checkins = _read_json(path)
        if explicit is None:
            existing = find_slot_by_session(sess, path)
            if existing and not is_checkin_stale(checkins.get(existing)):
                os.environ[AGENT_ID_ENV] = existing
                return existing
        requested = explicit
        if requested:
            slot = validate_agent_id(requested)
            current = checkins.get(slot)
            if current and not is_checkin_stale(current) and not _same_checkin_holder(current, sess, resolved):
                other = current.get("pid") or current.get("sessionKey")
                raise CheckinError(f"slot {slot!r} busy ({other} on {current.get('host')})")
            started = (
                (current or {}).get("startedAt")
                if current and _same_checkin_holder(current, sess, resolved)
                else None
            )
            checkins[slot] = checkin_record(sess, resolved, started_at=started)
            _atomic_write_json(path, checkins)
            os.environ[AGENT_ID_ENV] = slot
            return slot

        for slot in iter_slot_names(checkins):
            current = checkins.get(slot)
            if current and not is_checkin_stale(current):
                if _same_checkin_holder(current, sess, resolved):
                    os.environ[AGENT_ID_ENV] = slot
                    return slot
                continue
            checkins[slot] = checkin_record(sess, resolved)
            _atomic_write_json(path, checkins)
            os.environ[AGENT_ID_ENV] = slot
            return slot

    raise CheckinError("no free agent slot (all check-ins active)")


def release_checkin(
    slot: str | None,
    path: Path,
    *,
    session: str | None = None,
    holder: int | None | object = _HOLDER_UNSET,
) -> bool:
    if holder is _HOLDER_UNSET:
        resolved: int | None = resolve_holder_pid(None)
    else:
        resolved = holder  # type: ignore[assignment]
    sess = session or session_key()

    if slot is None:
        slot = find_slot_by_session(sess, path) or find_slot_by_holder_pid(resolved, path)
        if slot is None:
            return False
    else:
        slot = validate_agent_id(slot)

    with _exclusive(path.with_name(path.name + ".lock")):
        checkins = _read_json(path)
        record = checkins.get(slot)
        if not record:
            return False
        if not _same_checkin_holder(record, sess, resolved):
            rec_sess = record.get("sessionKey")
            rec_pid = record.get("pid")
            if rec_pid is not None and resolved is not None and rec_pid != resolved:
                return False
            if rec_sess and rec_sess != sess:
                return False
        del checkins[slot]
        _atomic_write_json(path, checkins)
        return True
