#!/usr/bin/env python3
"""Preflight gate for agent loops.

Computes a fingerprint from a single GraphQL request covering:
  - Open PRs authored by @me: headRefOid, mergeable, CI statusCheckRollup state,
    updatedAt, and last comment createdAt.
  - Candidate issues: updatedAt timestamps.

It also detects an actionable approved backlog: open issues carrying the approved
label, not assigned to another user, and with no open PR closing them.

If state.json has no 'current', the fingerprint matches 'lastFingerprint', and there
is no actionable approved backlog, outputs 'NOOP' and exits with code 1 so external
schedulers (e.g., `python3 tick_gate.py && ...`) do not invoke the LLM.

The tick lock belongs to one loop slot (loop + agent id). `--lock` prints the
ownership token as `TOKEN <token>`; `--renew`, `--unlock`, the claims, and the
guard prove ownership with that token. Under `devloops tick`, the wrapper owns
the lock and exports its token, so the same commands work without taking it twice.

Exit codes:
  0 - RUN (actionable work or state changed)
  1 - NOOP (state unchanged and idle)
  2 - BUSY (a tick of this slot holds a live lock; not proof of another worker)
  3 - ERROR (authentication, network, or GraphQL failure; not NOOP)
  4 - LOST (this tick no longer owns the lock; stop without pushing)
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

# Support running directly or as module
try:
    from devloops_lib.config import add_agent_id_argument, get_agent_id, get_state_path, load_config
    from devloops_lib.gh import GhError, gh
except ImportError:
    # Walk up to find repo root
    _root = Path(__file__).resolve().parent.parent
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
    from devloops_lib.config import add_agent_id_argument, get_agent_id, get_state_path, load_config
    from devloops_lib.gh import GhError, gh

EXIT_RUN = 0
EXIT_NOOP = 1
EXIT_BUSY = 2
EXIT_ERROR = 3
EXIT_LOST = 4

# A lock is live for this long after its last renewal. The wrapper renews it every
# TTL/3 while the agent runs; a direct run renews it through --renew and the guard.
LOCK_TTL_SECONDS = 1800
LOCK_TTL_ENV = "DEVLOOPS_LOCK_TTL_SECONDS"
DEFAULT_AGENT = "default"

# The removal guard is held only for a validate-and-unlink, so a release can
# wait briefly for it; reclaim never waits (a guard holder means "busy").
GUARD_RELEASE_WAIT_SECONDS = 1.0
# Windows refuses to delete a file another process has open for reading.
UNLINK_WAIT_SECONDS = 0.2
RETRY_INTERVAL_SECONDS = 0.01
# os.link errors meaning "this filesystem has no hard links".
LINK_UNSUPPORTED_ERRNOS = frozenset(
    {errno.EPERM, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL}
)

# Set by `devloops tick` for the agent it starts; see held_by_wrapper.
WRAPPER_PID_ENV = "DEVLOOPS_TICK_WRAPPER_PID"
TOKEN_ENV = "DEVLOOPS_TICK_TOKEN"

# Matches issue_candidates.py default (--limit 100): a starved approved issue is by
# definition not recently updated, so a small window would drop it from backlog detection.
ISSUE_LIMIT = 100
# PR search covers all open PRs (closing-PR detection), so @me PRs must not be pushed
# out of the window by other authors' PRs.
PR_LIMIT = 100

GRAPHQL_GATE_QUERY = """
query($prQuery: String!, $prLimit: Int!, $owner: String!, $name: String!, $issueLimit: Int!) {
  search(query: $prQuery, type: ISSUE, first: $prLimit) {
    nodes {
      ... on PullRequest {
        number
        headRefOid
        mergeable
        updatedAt
        isDraft
        author { login }
        title
        bodyText
        headRefName
        commits(last: 1) {
          nodes {
            commit {
              statusCheckRollup {
                state
              }
            }
          }
        }
        comments(last: 1) {
          nodes {
            createdAt
          }
        }
      }
    }
  }
  repository(owner: $owner, name: $name) {
    issues(first: $issueLimit, states: [OPEN], orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        number
        updatedAt
        labels(first: 20) { nodes { name } }
        assignees(first: 10) { nodes { login } }
      }
    }
  }
}
"""


# GhError is imported from devloops_lib.gh


def default_state_path(loop_name: str = "worker-loop") -> Path:
    return get_state_path(loop_name)


def load_state(path: Path) -> dict:
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f".tmp.{os.getpid()}.{time.time_ns()}")
    try:
        temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(path)
    except BaseException:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise


# gh helper is imported from devloops_lib.gh


def _pid_alive(pid: int) -> bool:
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        # A killed process that its parent has not reaped yet is a zombie (Linux).
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except OSError:
            return True
        return stat.rpartition(")")[2].split()[:1] != ["Z"]
    import ctypes
    from ctypes import wintypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h_proc:
        return False
    try:
        # A process that exited stays openable while any handle to it is open.
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(h_proc, ctypes.byref(exit_code)):
            return True
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(h_proc)


def lock_ttl() -> int:
    """Return the lock TTL in seconds: $DEVLOOPS_LOCK_TTL_SECONDS, or LOCK_TTL_SECONDS."""
    raw = os.environ.get(LOCK_TTL_ENV)
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return LOCK_TTL_SECONDS


def _is_stale_payload(data: dict, ttl_seconds: int) -> bool:
    """Return True when a parsed payload describes an abandoned lock.

    A payload linked to an owner lock (`ownerLock` + `ownerToken`, used by claims)
    lives exactly as long as that tick lock is held with that token, so a live tick
    never loses its claims to the TTL, and a dead tick's claims free at once.
    """
    owner_lock = data.get("ownerLock")
    if owner_lock:
        return not lock_is_owned(Path(owner_lock), data.get("ownerToken") or "")
    # If lock expired by TTL (time is the last renewal), it is stale regardless of PID
    if time.time() - data.get("time", 0) > ttl_seconds:
        return True
    # A recorded PID is checked only on its own host; elsewhere the TTL decides.
    pid = data.get("pid")
    if pid and data.get("host") in (None, socket.gethostname()):
        return not _pid_alive(pid)
    return False


def _read_lock_raw(lock_file: Path) -> str | None:
    """Read lock payload text verbatim. Returns None when the file is missing.

    PermissionError counts as missing: on Windows a file being deleted cannot
    be opened. Publication stays exclusive, so a wrong guess only means busy.
    """
    try:
        return lock_file.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError):
        return None


def check_lock(lock_file: Path, ttl_seconds: int | None = None) -> bool:
    """Return True if an active, non-stale lock exists."""
    raw = _read_lock_raw(lock_file)
    return raw is not None and _raw_is_live(lock_file, raw, ttl_seconds)


def read_lock_payload(lock_file: Path) -> dict | None:
    """Return the parsed lock payload, or None when it is missing or not a JSON object."""
    raw = _read_lock_raw(lock_file)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def lock_is_owned(lock_file: Path, token: str, ttl_seconds: int | None = None) -> bool:
    """Return True if lock_file is live and carries token."""
    if not token:
        return False
    data = read_lock_payload(lock_file)
    return bool(data) and data.get("token") == token and not _is_stale_payload(data, ttl_seconds or lock_ttl())


def describe_lock(lock_file: Path) -> str:
    """Describe the lock holder for BUSY and LOST messages.

    A holder is a tick of this loop slot. It can be a duplicate start, the slot's
    own earlier tick, or a crashed tick whose lock has not expired yet: the text
    reports what the lock records, never that another worker exists.
    """
    raw = _read_lock_raw(lock_file)
    if raw is None:
        return "no lock file"
    data = read_lock_payload(lock_file)
    if data is None:
        return "lock payload is being written or is corrupt; it expires with the TTL"
    now = time.time()
    parts = [f"loop {data.get('loop', '?')}", f"agent {data.get('agent', DEFAULT_AGENT)}"]
    pid = data.get("pid")
    if pid:
        host = data.get("host")
        same_host = host in (None, socket.gethostname())
        state = ("alive" if _pid_alive(pid) else "dead") if same_host else f"on host {host}"
        parts.append(f"pid {pid} {state}")
    if "time" in data:
        parts.append(f"renewed {int(now - data['time'])}s ago")
    if "started" in data:
        parts.append(f"started {int(now - data['started'])}s ago")
    parts.append("stale" if _is_stale_payload(data, lock_ttl()) else "live")
    return ", ".join(parts)


def owner_fields(loop: str) -> dict:
    """Return the payload fields that identify a tick lock's owner."""
    return {
        "loop": loop,
        "agent": get_agent_id() or DEFAULT_AGENT,
        "host": socket.gethostname(),
        "started": int(time.time()),
    }


def _raw_is_live(lock_file: Path, raw: str, ttl_seconds: int | None = None) -> bool:
    """Return True if the payload read from lock_file describes a live owner.

    Incomplete/corrupt payload policy: a lock file whose JSON cannot be parsed
    (empty, partial, or corrupt) is treated as an in-progress acquisition while
    it is younger than the TTL, so a contender never mistakes an acquiring
    owner for a stale lock and never steals it. Only a corrupt lock older than
    the TTL is reclaimed as stale.
    """
    ttl_seconds = ttl_seconds or lock_ttl()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("lock payload must be a JSON object")
    except Exception:
        try:
            mtime = lock_file.stat().st_mtime
        except FileNotFoundError:
            return False
        return time.time() - mtime <= ttl_seconds
    return not _is_stale_payload(data, ttl_seconds)


def _publish_lock(lock_file: Path, data: bytes) -> bool:
    """Publish a complete lock payload atomically and exclusively.

    The payload is fully written to a unique temporary file, then hard-linked
    into place: link creation is atomic and exclusive (FileExistsError if the
    lock already exists), so the lock file never exists with an empty or
    partial payload. On filesystems without hard-link support, falls back to
    O_CREAT|O_EXCL create-then-write; there, the fresh-corrupt-is-live policy
    (_raw_is_live) keeps contenders from stealing an in-progress acquisition.

    A PermissionError means the lock is being deleted on Windows, so it is
    reported as busy. Other unexpected link errors propagate.
    """
    tmp = lock_file.parent / f".{lock_file.name}.tmp.{uuid.uuid4().hex}"
    tmp.write_bytes(data)
    try:
        try:
            os.link(str(tmp), str(lock_file))
            return True
        except FileExistsError:
            return False
        except OSError as exc:
            if exc.errno == errno.EACCES:
                return False
            if exc.errno not in LINK_UNSUPPORTED_ERRNOS:
                raise
        try:
            fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, PermissionError):
            return False
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return True
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _try_os_lock(fd: int) -> None:
    """Take a non-blocking exclusive OS lock on fd; raise OSError if unavailable."""
    if sys.platform == "win32":
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _os_unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    # POSIX: closing the descriptor drops the flock.


@contextlib.contextmanager
def _removal_guard(lock_file: Path, wait_seconds: float = 0.0):
    """Serialize every removal of lock_file; yield whether the guard is held.

    Publishing needs the lock file to be absent, so while this guard is held
    the payload can only change if the holder removes it. Validate-then-unlink
    inside the guard is therefore atomic with respect to other reclaimers and
    releasers, and cannot delete a newer owner's lock.

    The guard is an OS lock on a sibling file that is never deleted. The kernel
    drops it when the holder dies, so it has no TTL or stale state of its own.
    Where OS locks are unavailable the guard is never held, so callers fail
    closed: a stale lock then needs an operator `--unlock`.
    """
    guard_path = lock_file.parent / f"{lock_file.name}.guard"
    try:
        fd = os.open(str(guard_path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        yield False
        return
    held = False
    deadline = time.monotonic() + wait_seconds
    try:
        while True:
            try:
                _try_os_lock(fd)
                held = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(RETRY_INTERVAL_SECONDS)
        yield held
    finally:
        if held:
            try:
                _os_unlock(fd)
            except OSError:
                pass
        os.close(fd)


def _unlink_lock(lock_file: Path) -> bool:
    """Delete the lock file; return True once it is gone."""
    deadline = time.monotonic() + UNLINK_WAIT_SECONDS
    while True:
        try:
            lock_file.unlink()
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(RETRY_INTERVAL_SECONDS)
        except OSError:
            return False


def _reclaim_stale(lock_file: Path) -> bool:
    """Remove a lock only after proving it is stale; return True when it is
    safe to attempt publishing.

    Validation and removal both happen inside the removal guard. A contender
    that finds the guard taken backs off as busy; one that gets it after a
    reclaim finds the file absent or a fresh lock, which is never removed.
    """
    with _removal_guard(lock_file) as held:
        if not held:
            return False
        raw = _read_lock_raw(lock_file)
        if raw is None:
            return True
        if _raw_is_live(lock_file, raw):
            return False
        return _unlink_lock(lock_file)


def acquire_lock_token(lock_file: Path, pid: int | None = None, extra: dict | None = None) -> str | None:
    """Try to acquire the lock. Return the ownership token on success, None if busy.

    Publication is atomic and exclusive (_publish_lock), so a freshly created
    live lock can never be reclaimed as partially published. Stale locks are
    removed only through the ownership-safe _reclaim_stale path. Fields in extra
    are stored in the payload.
    """
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    payload = {"started": int(time.time()), **(extra or {}), "time": time.time(), "token": token}
    if pid is not None:
        payload["pid"] = pid
    data = json.dumps(payload).encode("utf-8")
    for _ in range(2):
        if _publish_lock(lock_file, data):
            return token
        if check_lock(lock_file):
            return None
        if not _reclaim_stale(lock_file):
            return None
    return None


def acquire_lock(lock_file: Path, pid: int | None = None) -> bool:
    return acquire_lock_token(lock_file, pid=pid) is not None


def release_lock(lock_file: Path, token: str | None = None) -> None:
    """Remove the lock file.

    With a token: ownership-safe release. The lock is unlinked only when its
    current payload carries the same token, so a late release by an old owner
    never deletes a replacement lock. If the removal guard stays busy the lock
    is left in place and recovers through the TTL. Without a token:
    unconditional operator release (CLI --unlock and legacy callers), which
    still removes the lock when the guard cannot be taken.
    """
    with _removal_guard(lock_file, wait_seconds=GUARD_RELEASE_WAIT_SECONDS) as held:
        if token is not None:
            if not held:
                return
            raw = _read_lock_raw(lock_file)
            if raw is None:
                return
            try:
                data = json.loads(raw)
            except Exception:
                return  # cannot prove ownership of a corrupt payload: leave it
            if not isinstance(data, dict) or data.get("token") != token:
                return
        _unlink_lock(lock_file)


def _replace_payload(lock_file: Path, data: bytes) -> bool:
    """Atomically replace the payload of an existing lock; return True on success.

    Only called inside the removal guard, so nobody can remove or reclaim the lock
    meanwhile. Windows refuses to replace a file that a reader has open, so retry briefly.
    """
    tmp = lock_file.parent / f".{lock_file.name}.renew.{uuid.uuid4().hex}"
    tmp.write_bytes(data)
    deadline = time.monotonic() + UNLINK_WAIT_SECONDS * 5
    try:
        while True:
            try:
                os.replace(str(tmp), str(lock_file))
                return True
            except PermissionError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(RETRY_INTERVAL_SECONDS)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def renew_lock(lock_file: Path, token: str) -> bool:
    """Renew a lock this token owns; return False if ownership is lost.

    Renewal happens inside the removal guard: a contender that saw the lock as
    stale must take the guard to remove it, and then re-reads the renewed, live
    payload. A lock that expired but was not yet reclaimed is still this owner's.
    """
    if not token:
        return False
    with _removal_guard(lock_file, wait_seconds=GUARD_RELEASE_WAIT_SECONDS) as held:
        if not held:
            # Without the guard the payload cannot be replaced safely; report the
            # current ownership so a busy guard is never mistaken for a loss.
            return lock_is_owned(lock_file, token)
        data = read_lock_payload(lock_file)
        if not data or data.get("token") != token:
            return False
        data["time"] = time.time()
        return _replace_payload(lock_file, json.dumps(data).encode("utf-8"))


def held_by_wrapper(lock_file: Path) -> bool:
    """Return True if this process runs under the `devloops tick` holding lock_file.

    The wrapper exports its token to the agent. It must match the token in the
    lock, so a duplicate tick, a gate for another slot, or a variable left over
    from an earlier wrapper still sees a normal lock.
    """
    return token_holds_lock(lock_file, os.environ.get(TOKEN_ENV) or "")


def token_holds_lock(lock_file: Path, token: str) -> bool:
    """Return True if the lock file still carries token.

    An expired lock that nobody reclaimed still carries its token: no other tick
    ran in between, because taking the lock replaces the token.
    """
    if not token:
        return False
    data = read_lock_payload(lock_file)
    return bool(data) and data.get("token") == token


def take_tick_lock(lock_file: Path, loop: str, pid: int | None = None) -> str | None:
    """Return this tick's token, or None if a live lock of this slot is held.

    Under the wrapper that holds lock_file, the token is the wrapper's, so a call
    inside the wrapper never counts as a duplicate tick.
    """
    if held_by_wrapper(lock_file):
        return os.environ[TOKEN_ENV]
    return acquire_lock_token(lock_file, pid=pid, extra=owner_fields(loop))


CLOSING_PATTERN = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b", re.I)
# Same heuristic as issue_candidates.py: only 3+ digit branch suffixes count as closing refs.
BRANCH_ISSUE_PATTERN = re.compile(r"-(\d{3,})\b")


def fetch_gate_data() -> dict:
    """Fetch open PRs (all authors) and open issues via a single GraphQL request."""
    repo_raw = gh("repo", "view", "--json", "owner,name")
    repo = json.loads(repo_raw)
    owner, name = repo["owner"]["login"], repo["name"]
    pr_query = f"repo:{owner}/{name} is:pr is:open"

    query_raw = gh(
        "api", "graphql",
        "-f", f"query={GRAPHQL_GATE_QUERY}",
        "-F", f"prQuery={pr_query}",
        "-F", f"owner={owner}",
        "-F", f"name={name}",
        "-F", f"prLimit={PR_LIMIT}",
        "-F", f"issueLimit={ISSUE_LIMIT}",
    )
    data = json.loads(query_raw).get("data", {})
    return {
        "prs": data.get("search", {}).get("nodes") or [],
        "issues": (data.get("repository") or {}).get("issues", {}).get("nodes") or [],
    }


def compute_fingerprint(gate_data: dict | None = None, me: str | None = None) -> str:
    """Compute sha256 hash from @me open PRs and candidate issue updatedAt timestamps.

    Only PR state fields that require worker action are hashed; labels, assignees,
    and PR body text are intentionally excluded so they do not mask a stable fingerprint.
    """
    if gate_data is None:
        gate_data = fetch_gate_data()
    if me is None:
        me = gh("api", "user", "--jq", ".login").strip()

    prs_fingerprint = []
    for pr in gate_data["prs"]:
        if not pr:
            continue
        if (pr.get("author") or {}).get("login") != me:
            continue
        if pr.get("isDraft"):
            continue

        commit_nodes = (pr.get("commits") or {}).get("nodes") or []
        last_commit = commit_nodes[0].get("commit") if commit_nodes else {}
        ci_state = ((last_commit.get("statusCheckRollup") or {}).get("state")) or ""

        comment_nodes = (pr.get("comments") or {}).get("nodes") or []
        last_comment_time = comment_nodes[0].get("createdAt") if comment_nodes else ""

        prs_fingerprint.append((
            pr["number"],
            pr.get("headRefOid"),
            pr.get("mergeable"),
            ci_state,
            pr.get("updatedAt"),
            last_comment_time,
        ))
    prs_fingerprint.sort()

    issues_fingerprint = sorted((i["number"], i.get("updatedAt")) for i in gate_data["issues"])

    hasher = hashlib.sha256()
    hasher.update(json.dumps({"prs": prs_fingerprint, "issues": issues_fingerprint}).encode("utf-8"))
    return hasher.hexdigest()


def find_actionable_backlog(gate_data: dict, me: str, approved_label: str = "approved") -> list[int]:
    """Return open, actionable issues with the approved label and no open closing PR.

    An issue is actionable when it carries approved_label, is not assigned to another
    user (issues assigned to @me are still actionable: the worker claims by assigning
    @me, an in-progress claim is covered by state.current, and an open claim is covered
    by its closing PR), and no open PR closes it (via closing keywords in title/body or
    a `-<N>` branch suffix, same heuristic as issue_candidates.py).
    """
    closing_prs: set[int] = set()
    for pr in gate_data["prs"]:
        if not pr:
            continue
        text = f"{pr.get('title') or ''} {pr.get('bodyText') or ''}"
        closes = {int(n) for n in CLOSING_PATTERN.findall(text)}
        closes |= {int(n) for n in BRANCH_ISSUE_PATTERN.findall(pr.get("headRefName") or "")}
        closing_prs |= closes

    actionable: list[int] = []
    for issue in gate_data["issues"]:
        if not issue:
            continue
        labels = {l.get("name") for l in ((issue.get("labels") or {}).get("nodes") or []) if l}
        if approved_label not in labels:
            continue
        assignees = {a.get("login") for a in ((issue.get("assignees") or {}).get("nodes") or []) if a}
        if assignees - {me}:
            continue
        if issue["number"] in closing_prs:
            continue
        actionable.append(issue["number"])
    return sorted(actionable)


def preflight_decision(config: dict | None = None) -> tuple[str, list[int]]:
    """Return (fingerprint, actionable approved backlog) from one GraphQL fetch.

    The backlog makes the gate RUN even when the fingerprint is unchanged, so an
    approved issue with no closing PR is never starved by a quiet repo.
    """
    gate_data = fetch_gate_data()
    me = gh("api", "user", "--jq", ".login").strip()
    cfg = config or load_config()
    approved_label = cfg.get("labels", {}).get("approved", "approved")
    return (
        compute_fingerprint(gate_data, me),
        find_actionable_backlog(gate_data, me, approved_label),
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", default="worker-loop", help="Loop name (default: worker-loop)")
    parser.add_argument("--state-file", type=Path, default=None, help="Path to state.json")
    add_agent_id_argument(parser)
    parser.add_argument("--update", action="store_true", help="Update lastFingerprint in state.json after a successful tick")
    parser.add_argument("--lock", action="store_true", help="Acquire tick.lock before exiting 0; prints TOKEN <token>")
    parser.add_argument("--pid", type=int, default=None, help="Process ID of the tick owner to store in tick.lock")
    parser.add_argument("--unlock", action="store_true", help="Release tick.lock owned by --token")
    parser.add_argument("--renew", action="store_true", help="Renew tick.lock owned by --token; exits 4 (LOST) if not owned")
    parser.add_argument("--token", default=None, help=f"Token printed by --lock (default: ${TOKEN_ENV})")
    parser.add_argument("--force-unlock", action="store_true", help="Operator only: remove tick.lock whoever owns it")
    args = parser.parse_args(argv)

    state_path = args.state_file or default_state_path(args.loop)
    lock_path = state_path.parent / "tick.lock"
    token = args.token or os.environ.get(TOKEN_ENV)
    # Under the wrapper, --lock and --unlock leave its lock in place.
    wrapper_held = held_by_wrapper(lock_path)

    try:
        if args.force_unlock:
            release_lock(lock_path)
            print("UNLOCKED (forced by operator)")
            sys.exit(EXIT_RUN)

        if args.renew:
            if not token:
                sys.stderr.write(f"--renew needs the token printed by --lock (--token or ${TOKEN_ENV})\n")
                sys.exit(EXIT_ERROR)
            if renew_lock(lock_path, token):
                print("RENEWED")
                sys.exit(EXIT_RUN)
            print(f"LOST ({describe_lock(lock_path)})")
            sys.exit(EXIT_LOST)

        if args.unlock and not wrapper_held:
            if not token:
                sys.stderr.write(
                    f"--unlock needs the token printed by --lock (--token or ${TOKEN_ENV}); "
                    "an operator can use --force-unlock\n"
                )
                sys.exit(EXIT_ERROR)
            if not token_holds_lock(lock_path, token):
                # A tick that lost its lock must not write the slot's state either.
                print(f"LOST ({describe_lock(lock_path)})")
                sys.exit(EXIT_LOST)

        if args.update or args.unlock:
            try:
                state = load_state(state_path)
                tick_start_fp = state.pop("tickStartFingerprint", None)
                if args.update and tick_start_fp is not None:
                    state["lastFingerprint"] = tick_start_fp
                    print(f"UPDATED: {tick_start_fp[:12]}")
                if tick_start_fp is not None or (args.update and state_path.is_file()):
                    save_state(state_path, state)
            finally:
                # The lock is released even if the state update fails.
                if args.unlock:
                    if wrapper_held:
                        print("LOCK KEPT (held by devloops tick)")
                    else:
                        release_lock(lock_path, token=token)
                        print("UNLOCKED")
            sys.exit(EXIT_RUN)

        if check_lock(lock_path) and not wrapper_held:
            print(f"BUSY ({describe_lock(lock_path)})")
            sys.exit(EXIT_BUSY)

        if args.loop != "worker-loop":
            # The fingerprint gate is the worker's; other loops only take the lock here.
            tick_token = take_tick_lock(lock_path, args.loop, pid=args.pid) if args.lock else None
            if args.lock and tick_token is None:
                print(f"BUSY ({describe_lock(lock_path)})")
                sys.exit(EXIT_BUSY)
            print(f"RUN ({args.loop} has no fingerprint gate)")
            if tick_token:
                print(f"TOKEN {tick_token}")
            sys.exit(EXIT_RUN)

        state = load_state(state_path)

        # If currently implementing an issue or branch, always run
        if state.get("current"):
            tick_token = None
            if args.lock:
                tick_token = take_tick_lock(lock_path, args.loop, pid=args.pid)
                if tick_token is None:
                    print(f"BUSY ({describe_lock(lock_path)})")
                    sys.exit(EXIT_BUSY)
            print("RUN (current work in progress)")
            if tick_token:
                print(f"TOKEN {tick_token}")
            sys.exit(EXIT_RUN)

        fp, backlog = preflight_decision()
        last_fp = state.get("lastFingerprint")

        if last_fp and last_fp == fp and not backlog:
            print("NOOP (no changes)")
            sys.exit(EXIT_NOOP)

        # State changed (or approved backlog waiting): acquire lock if requested, but DO NOT save lastFingerprint yet.
        # lastFingerprint must be saved only after a successful tick via --update.
        tick_token = None
        if args.lock:
            tick_token = take_tick_lock(lock_path, args.loop, pid=args.pid)
            if tick_token is None:
                print(f"BUSY ({describe_lock(lock_path)})")
                sys.exit(EXIT_BUSY)
            state["tickStartFingerprint"] = fp
            save_state(state_path, state)
        if last_fp and last_fp == fp:
            print(f"RUN (approved backlog waiting: #{', #'.join(map(str, backlog))})")
        else:
            print(f"RUN (changes detected: {fp[:12]})")
        if tick_token:
            print(f"TOKEN {tick_token}")
        sys.exit(EXIT_RUN)

    except GhError as e:
        sys.stderr.write(f"tick_gate GitHub CLI error: {e}\n")
        sys.exit(EXIT_ERROR)
    except Exception as e:
        sys.stderr.write(f"tick_gate unexpected error: {e}\n")
        sys.exit(EXIT_ERROR)


if __name__ == "__main__":
    from devloops_lib.config import run_main

    run_main(main)
