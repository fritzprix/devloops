#!/usr/bin/env python3
"""Multi-process tests: tick locks, claims, and the guard across real processes.

Each test starts separate Python processes through the installed entry points
(skills/worker-loop/scripts/*.py and `python -m devloops_lib.cli`), released
together by a start file, so the races are between processes, not threads.
"""
from __future__ import annotations

import json
import os
import socket
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

from devloops_lib import claims, tick_gate
from devloops_lib.lock_heartbeat import stop_heartbeat
from devloops_lib.proc_util import terminate_process_tree
from devloops_lib.config import AGENT_ID_ENV, get_claims_dir, get_lock_path, get_state_path
from devloops_lib.tick_gate import EXIT_BUSY, EXIT_LOST, EXIT_RUN, LOCK_TTL_ENV, TOKEN_ENV, WRAPPER_PID_ENV

from tests.test_claims import make_remote, push_old_claim

SCRIPTS = REPO_ROOT / "skills" / "worker-loop" / "scripts"
PROCESSES = 6
ROUNDS = 3
WAIT_SECONDS = 60

# Waits for the start file, then runs a script as __main__ with the given argv.
START_TOGETHER = """\
import os, runpy, sys, time
start = sys.argv[1]
while not os.path.exists(start):
    time.sleep(0.002)
sys.argv = sys.argv[2:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""

# Claims an issue through the library (claim.py would also query GitHub for the limit).
CLAIM_ISSUE = """\
import json, sys
from pathlib import Path
from devloops_lib.claims import claim_issue, refresh_issue_claim
repo, number, agent, ttl, action = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4]), sys.argv[5]
if action == "refresh":
    print(json.dumps({"status": "refreshed" if refresh_issue_claim(Path(repo), number, agent) else "lost"}))
else:
    print(json.dumps(claim_issue(Path(repo), number, "origin/main", agent, ttl)))
"""

# Agent run by the wrapper: records its token, waits for `go`, then tries a guarded command.
ORPHAN_AGENT = """\
import os, subprocess, sys, time
from pathlib import Path
work = Path(sys.argv[1])
(work / "agent.pid").write_text(str(os.getpid()), encoding="utf-8")
(work / "token").write_text(os.environ["DEVLOOPS_TICK_TOKEN"], encoding="utf-8")
deadline = time.time() + 60
while not (work / "go").exists() and time.time() < deadline:
    time.sleep(0.05)
marker = work / "pushed"
rc = subprocess.call([sys.executable, sys.argv[2], "run", "--", sys.executable, "-c",
                      f"open({str(marker)!r}, 'w').close()"])
(work / "rc").write_text(str(rc), encoding="utf-8")
"""


class _WindowsSafeTempDir:
    """TemporaryDirectory that reaps stray child handles before cleanup on Windows."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.name = self._tmp.name

    def cleanup(self) -> None:
        for child in tuple(getattr(self, "_children", ())):
            if child.poll() is None:
                terminate_process_tree(child.pid)
                try:
                    child.wait(timeout=WAIT_SECONDS)
                except subprocess.TimeoutExpired:
                    terminate_process_tree(child.pid, wait_seconds=5)
        for hb in Path(self.name).rglob("tick.lock.heartbeat"):
            stop_heartbeat(hb.parent / "tick.lock")
        deadline = time.time() + 10
        last_err: OSError | None = None
        while time.time() < deadline:
            try:
                self._tmp.cleanup()
                return
            except OSError as exc:
                last_err = exc
                time.sleep(0.2)
        if last_err is not None:
            raise last_err


class ProcessTestCase(unittest.TestCase):
    ttl = None
    _children: list[subprocess.Popen]
    SLOT_AGENT: str | None = None

    def setUp(self):
        self._children = []
        if sys.platform == "win32":
            self._tmpdir = _WindowsSafeTempDir()
            self.addCleanup(self._tmpdir.cleanup)
            tmp_name = self._tmpdir.name
        else:
            self._tmpdir = tempfile.TemporaryDirectory()
            self.addCleanup(self._tmpdir.cleanup)
            tmp_name = self._tmpdir.name
        self.root = Path(tmp_name)
        self.state = self.root / "state"
        self.env = {k: v for k, v in os.environ.items() if k not in (TOKEN_ENV, WRAPPER_PID_ENV, AGENT_ID_ENV)}
        self.env.update({"LOOP_STATE_DIR": str(self.state), "PYTHONPATH": str(REPO_ROOT)})
        if self.ttl is not None:
            self.env[LOCK_TTL_ENV] = str(self.ttl)
        if self.SLOT_AGENT:
            self.env[AGENT_ID_ENV] = self.SLOT_AGENT
        patcher = patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._reap_children_and_heartbeats)
        self.start_runner = self.root / "start_together.py"
        self.start_runner.write_text(START_TOGETHER, encoding="utf-8")

    def _reap_children_and_heartbeats(self) -> None:
        for proc in self._children:
            if proc.poll() is None:
                terminate_process_tree(proc.pid)
                try:
                    proc.wait(timeout=WAIT_SECONDS)
                except subprocess.TimeoutExpired:
                    terminate_process_tree(proc.pid, wait_seconds=5)
        for hb in self.root.rglob("tick.lock.heartbeat"):
            stop_heartbeat(hb.parent / "tick.lock")

    def write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def slot_path(self, agent, getter, *args):
        with patch.dict(os.environ, {AGENT_ID_ENV: agent} if agent else {}):
            return getter(*args)

    def mark_in_progress(self, agent=None):
        """Give the slot a current item, so its worker gate runs without asking GitHub."""
        state = self.slot_path(agent, get_state_path, "worker-loop")
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"current": {"kind": "pr", "number": 1}}), encoding="utf-8")

    def py(self, *argv):
        return subprocess.run([sys.executable, *map(str, argv)], cwd=str(self.root), env=self.env,
                              capture_output=True, text=True, timeout=WAIT_SECONDS)

    def script(self, name, *args):
        return self.py(SCRIPTS / name, *args)

    def together(self, commands):
        """Run [argv, ...] as separate processes released at the same moment."""
        start = self.root / f"start-{time.time_ns()}"
        procs = [
            subprocess.Popen([sys.executable, str(self.start_runner), str(start), *map(str, argv)],
                             cwd=str(self.root), env=self.env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
            for argv in commands
        ]
        time.sleep(0.3)
        start.touch()
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=WAIT_SECONDS)
            results.append((proc.returncode, out, err))
        return results

    def wait_for(self, path: Path, proc=None):
        deadline = time.time() + WAIT_SECONDS
        while not path.exists():
            if proc is not None and proc.poll() is not None:
                self.fail(f"process exited with {proc.returncode} before {path.name} appeared: {proc.communicate()}")
            if time.time() > deadline:
                self.fail(f"timed out waiting for {path}")
            time.sleep(0.05)

    def wrapper(self, agent_cmd, agent=None, output=subprocess.PIPE):
        argv = [sys.executable, "-m", "devloops_lib.cli", "tick", "worker", "--agent-cmd", agent_cmd]
        if agent:
            argv += ["--agent-id", agent]
        popen_kw: dict = {
            "cwd": str(self.root),
            "env": self.env,
            "stdout": output,
            "stderr": output,
            "text": True,
        }
        if sys.platform == "win32":
            popen_kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kw["start_new_session"] = True
        proc = subprocess.Popen(argv, **popen_kw)
        self._children.append(proc)
        if isinstance(self._tmpdir, _WindowsSafeTempDir):
            self._tmpdir._children = self._children  # type: ignore[attr-defined]
        return proc


def token_of(stdout: str) -> str:
    lines = [line for line in stdout.splitlines() if line.startswith("TOKEN ")]
    return lines[0].split()[1] if lines else ""


class SingleSlotProcessTest(ProcessTestCase):
    """Lock/claim races use one explicit slot so contenders share one tick.lock."""

    SLOT_AGENT = "race"


class TickLockProcessTest(SingleSlotProcessTest):
    def test_duplicate_direct_ticks_have_exactly_one_owner(self):
        self.mark_in_progress()
        lock = get_lock_path("worker-loop")
        for _ in range(ROUNDS):
            results = self.together([[SCRIPTS / "tick_gate.py", "--lock"]] * PROCESSES)
            winners = [r for r in results if r[0] == EXIT_RUN]
            self.assertEqual(len(winners), 1, results)
            for code, out, _ in results:
                if code != EXIT_RUN:
                    self.assertEqual(code, EXIT_BUSY, out)
                    self.assertIn("BUSY", out)
            token = token_of(winners[0][1])
            self.assertTrue(tick_gate.lock_is_owned(lock, token))
            self.assertEqual(self.script("tick_gate.py", "--unlock", "--token", token).returncode, EXIT_RUN)
            self.assertFalse(lock.exists())


class LongTickProcessTest(SingleSlotProcessTest):
    ttl = 3

    def test_wrapper_keeps_its_lock_while_the_agent_runs_past_the_ttl(self):
        self.mark_in_progress()
        lock = get_lock_path("worker-loop")
        agent_seconds = 3 * self.ttl
        wrapper = self.wrapper(f'"{sys.executable}" -c "import time; time.sleep({agent_seconds})"')
        self.wait_for(lock, wrapper)
        started = tick_gate.read_lock_payload(lock)["started"]
        attempts = 0
        while wrapper.poll() is None:
            result = self.script("tick_gate.py", "--lock")
            if wrapper.poll() is not None and result.returncode == EXIT_RUN:
                break
            self.assertEqual(result.returncode, EXIT_BUSY, result.stdout + result.stderr)
            payload = tick_gate.read_lock_payload(lock)
            if payload:
                self.assertLessEqual(time.time() - payload["time"], self.ttl)
            attempts += 1
            time.sleep(0.2)
        out, err = wrapper.communicate(timeout=WAIT_SECONDS)
        self.assertEqual(wrapper.returncode, 0, out + err)
        self.assertGreater(attempts, 3)
        self.assertGreaterEqual(time.time() - started, agent_seconds)
        self.assertIn("UNLOCKED", out)


class LostOwnershipProcessTest(SingleSlotProcessTest):
    ttl = 1

    def test_tick_that_lost_its_lock_cannot_unlock_claim_or_run_guarded_commands(self):
        self.mark_in_progress()
        lock = get_lock_path("worker-loop")
        first = token_of(self.script("tick_gate.py", "--lock").stdout)
        self.assertTrue(first)
        stop_heartbeat(lock)
        time.sleep(self.ttl + 0.5)
        second = token_of(self.script("tick_gate.py", "--lock").stdout)
        self.assertTrue(second, "a direct lock that was not renewed within the TTL must be reclaimable")

        late = self.script("tick_gate.py", "--unlock", "--token", first)
        self.assertEqual(late.returncode, EXIT_LOST, late.stdout)
        self.assertTrue(tick_gate.lock_is_owned(lock, second))

        marker = self.root / "pushed"
        guarded = self.script("guard.py", "run", "--token", first, "--",
                              sys.executable, "-c", f"open({str(marker)!r}, 'w').close()")
        self.assertEqual(guarded.returncode, EXIT_LOST, guarded.stdout + guarded.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(self.script("claim.py", "pr", "7", "--token", first).returncode, EXIT_LOST)
        self.assertFalse((get_claims_dir() / "pr-7").exists())

        owner = self.script("guard.py", "run", "--token", second, "--",
                            sys.executable, "-c", f"open({str(marker)!r}, 'w').close()")
        self.assertEqual(owner.returncode, 0, owner.stdout + owner.stderr)
        self.assertTrue(marker.exists())
        self.assertEqual(self.script("tick_gate.py", "--unlock", "--token", second).returncode, EXIT_RUN)


class WrapperExitProcessTest(SingleSlotProcessTest):
    def test_agent_error_exit_releases_the_lock_and_the_claims(self):
        self.mark_in_progress()
        agent = self.write("agent.py", (
            "import subprocess, sys\n"
            f"rc = subprocess.call([sys.executable, {str(SCRIPTS / 'claim.py')!r}, 'pr', '7'])\n"
            "sys.exit(7 if rc == 0 else 50 + rc)\n"
        ))
        wrapper = self.wrapper(f'"{sys.executable}" "{agent}"')
        out, err = wrapper.communicate(timeout=WAIT_SECONDS)
        self.assertEqual(wrapper.returncode, 7, out + err)
        self.assertIn("RELEASED CLAIMS: PR #7", out)
        self.assertFalse(get_lock_path("worker-loop").exists())
        self.assertFalse((get_claims_dir() / "pr-7").exists())

    def test_agent_of_a_killed_wrapper_cannot_push_after_the_next_tick_starts(self):
        self.mark_in_progress()
        work = self.root / "work"
        work.mkdir()
        agent = self.write("agent.py", ORPHAN_AGENT)
        # No pipes: the orphaned agent would keep them open after the wrapper dies.
        wrapper = self.wrapper(f'"{sys.executable}" "{agent}" "{work}" "{SCRIPTS / "guard.py"}"',
                               output=subprocess.DEVNULL)
        try:
            self.wait_for(work / "token", wrapper)
            wrapper.kill()
            wrapper.wait(timeout=WAIT_SECONDS)
            # The wrapper's pid is dead, so the next tick reclaims the lock at once.
            next_tick = self.script("tick_gate.py", "--lock")
            self.assertEqual(next_tick.returncode, EXIT_RUN, next_tick.stdout + next_tick.stderr)
        finally:
            (work / "go").touch()
        self.wait_for(work / "rc")
        stop_heartbeat(get_lock_path("worker-loop"))
        pid_file = work / "agent.pid"
        if pid_file.exists():
            try:
                orphan = int(pid_file.read_text(encoding="utf-8").strip())
                terminate_process_tree(orphan, wait_seconds=5)
            except (OSError, ValueError):
                pass
        self.assertEqual((work / "rc").read_text(encoding="utf-8"), str(EXIT_LOST))
        self.assertFalse((work / "pushed").exists())
        self.assertTrue(tick_gate.lock_is_owned(get_lock_path("worker-loop"), token_of(next_tick.stdout)))


class PrClaimProcessTest(ProcessTestCase):
    def test_slots_racing_for_one_pr_have_exactly_one_winner(self):
        slots = [f"s{i}" for i in range(PROCESSES)]
        tokens = {}
        for slot in slots:
            tokens[slot] = tick_gate.acquire_lock_token(self.slot_path(slot, get_lock_path, "worker-loop"))
        for _ in range(ROUNDS):
            results = self.together([
                [SCRIPTS / "claim.py", "pr", "7", "--agent-id", slot, "--token", tokens[slot]] for slot in slots
            ])
            codes = [code for code, _, _ in results]
            self.assertEqual(codes.count(EXIT_RUN), 1, results)
            self.assertEqual(codes.count(EXIT_BUSY), PROCESSES - 1, results)
            winner = slots[codes.index(EXIT_RUN)]
            self.assertTrue(claims.owns_pr(get_claims_dir(), 7, tokens[winner]))
            self.assertEqual(self.script("claim.py", "release-pr", "7", "--agent-id", winner,
                                         "--token", tokens[winner]).returncode, EXIT_RUN)


class LiveTickClaimProcessTest(ProcessTestCase):
    ttl = 1

    def test_claim_of_a_live_long_tick_is_never_taken(self):
        self.mark_in_progress("a")
        agent = self.write("agent.py", (
            "import subprocess, sys, time\n"
            f"rc = subprocess.call([sys.executable, {str(SCRIPTS / 'claim.py')!r}, 'pr', '7'])\n"
            f"open({str(self.root / 'claimed')!r}, 'w').close()\n"
            "time.sleep(4)\n"
            "sys.exit(rc)\n"
        ))
        wrapper = self.wrapper(f'"{sys.executable}" "{agent}"', agent="a")
        self.wait_for(self.root / "claimed", wrapper)
        lock_b = self.slot_path("b", get_lock_path, "worker-loop")
        token_b = tick_gate.acquire_lock_token(lock_b)
        attempts = 0
        while wrapper.poll() is None:
            result = self.script("claim.py", "pr", "7", "--agent-id", "b", "--token", token_b)
            if result.returncode == EXIT_RUN:
                self.assertIsNotNone(wrapper.poll(), "slot b took the PR from a live tick")
                break
            self.assertEqual(result.returncode, EXIT_BUSY, result.stdout + result.stderr)
            attempts += 1
            time.sleep(0.2)
        out, err = wrapper.communicate(timeout=WAIT_SECONDS)
        self.assertEqual(wrapper.returncode, 0, out + err)
        self.assertGreater(attempts, 3)
        result = self.script("claim.py", "pr", "7", "--agent-id", "b", "--token", token_b)
        self.assertEqual(result.returncode, EXIT_RUN, result.stdout + result.stderr)


class IssueClaimProcessTest(ProcessTestCase):
    TTL = 3600

    def setUp(self):
        super().setUp()
        self.clones = [f"c{i}" for i in range(PROCESSES)]
        self.remote, paths = make_remote(self.root, *self.clones)
        self.repos = dict(zip(self.clones, paths))
        self.claim_script = self.write("claim_issue.py", CLAIM_ISSUE)

    def race(self, number, action_of=lambda slot: "claim"):
        results = self.together([
            [self.claim_script, self.repos[slot], number, slot, self.TTL, action_of(slot)] for slot in self.clones
        ])
        statuses = {}
        for slot, (code, out, err) in zip(self.clones, results):
            self.assertEqual(code, 0, err)
            statuses[slot] = json.loads(out)["status"]
        return statuses

    def owner(self, number):
        return claims.read_issue_claim(self.repos["c0"], number).agent

    def test_slots_racing_for_a_free_issue_have_exactly_one_winner(self):
        for number in range(1, ROUNDS + 1):
            statuses = self.race(number)
            winners = [slot for slot, status in statuses.items() if status == "claimed"]
            self.assertEqual(len(winners), 1, statuses)
            self.assertEqual(sorted(set(statuses.values())), ["busy", "claimed"])
            self.assertEqual(self.owner(number), winners[0])

    def test_slots_racing_to_take_over_a_stale_claim_have_exactly_one_winner(self):
        push_old_claim(self.repos["c0"], 5, "gone", age_seconds=self.TTL + 60, branch="feat/old-5")
        statuses = self.race(5)
        winners = [slot for slot, status in statuses.items() if status == "took-over"]
        self.assertEqual(len(winners), 1, statuses)
        self.assertEqual(self.owner(5), winners[0])

    def test_released_issue_is_claimed_again_by_exactly_one_slot(self):
        self.assertEqual(claims.claim_issue(self.repos["c0"], 5, "origin/main", "c0", self.TTL)["status"], "claimed")
        self.assertTrue(claims.release_issue(self.repos["c0"], 5, "c0"))
        statuses = self.race(5)
        self.assertEqual(list(statuses.values()).count("claimed"), 1, statuses)

    def test_owner_refresh_and_takeovers_never_leave_two_owners(self):
        # c0 owns a claim that just went stale. It refreshes while the others try
        # to take it over: either the refresh lands first and everyone else is
        # busy, or one takeover lands first and the owner learns it lost the claim.
        push_old_claim(self.repos["c0"], 5, "c0", age_seconds=self.TTL + 60, host=socket.gethostname())
        statuses = self.race(5, action_of=lambda slot: "refresh" if slot == "c0" else "claim")
        takeovers = [slot for slot, status in statuses.items() if status == "took-over"]
        if statuses["c0"] == "refreshed":
            self.assertEqual(takeovers, [], statuses)
            self.assertEqual(self.owner(5), "c0")
        else:
            self.assertEqual(statuses["c0"], "lost", statuses)
            self.assertEqual(len(takeovers), 1, statuses)
            self.assertEqual(self.owner(5), takeovers[0])


if __name__ == "__main__":
    unittest.main()
