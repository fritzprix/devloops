#!/usr/bin/env python3
"""Per-item claims, so workers on one repo and GitHub account do not take the same work.

Three things are kept apart:
  - slot: one running worker, named by --agent-id (default `default`);
  - tick lock: one tick at a time per slot (tick_gate), proven by its token;
  - claim: one slot at a time per PR or issue (this module).

A claim command first proves that this tick still owns its lock, so a tick that
lost its lock can neither take nor push a claim.

A PR claim is a lock file, <state_dir>/claims/pr-N, shared by the slots on one
machine. It is linked to the tick lock that took it: it lives exactly as long as
that tick holds its lock, so a live tick never loses it (however long the tick
runs), and the claim frees as soon as the tick ends, crashes, or loses its lock.

An issue claim is a remote branch, wip/issue-N, so it also works across machines.
Each change is a fast-forward push of a small commit whose trailers record the
claim: agent, host, state (active or released), time, and the work branch. A
fast-forward push succeeds only if the branch did not move, which makes every
change a compare-and-swap; no force push or branch deletion is ever needed.
  - The owner refreshes an active claim while it works on the issue.
  - An active claim not refreshed within the TTL is stale; another slot may take
    it over and is told the previous work branch, so the work is not lost.
  - Release pushes a `released` commit; the next claim continues from it.

Exit codes:
  0 - claimed, refreshed, taken over, or released
  1 - over the open-work limit: the new claim was released, do not start it
  2 - a live tick of another slot owns the item
  3 - error (git, GitHub, or a missing token); nothing is known about the owner
  4 - this tick no longer owns its tick lock: stop without pushing
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from devloops_lib.config import (
    GATEKEEPER_APPROVED_LABEL,
    add_agent_id_argument,
    get_agent_id,
    get_claims_dir,
    get_lock_path,
    issue_claim_ttl_seconds,
    load_config,
    open_pr_limit,
)
from devloops_lib.gh import GhError, gh
from devloops_lib.tick_gate import (
    DEFAULT_AGENT,
    EXIT_BUSY,
    EXIT_ERROR,
    EXIT_LOST,
    EXIT_NOOP,
    EXIT_RUN,
    TOKEN_ENV,
    acquire_lock_token,
    describe_lock,
    release_lock,
    renew_lock,
)

OWNER_TRAILER = "Devloops-Claim-Agent"
HOST_TRAILER = "Devloops-Claim-Host"
STATE_TRAILER = "Devloops-Claim-State"
TIME_TRAILER = "Devloops-Claim-Time"
BRANCH_TRAILER = "Devloops-Claim-Branch"
ACTIVE = "active"
RELEASED = "released"
WIP_REF_PREFIX = "refs/heads/wip/issue-"
HEAD_ISSUE_PATTERN = re.compile(r"-(\d+)$")
PR_CLAIM_NAME = re.compile(r"pr-(\d+)$")
TRAILER_PATTERN = re.compile(r"^(Devloops-Claim-[A-Za-z]+): (.+)$", re.M)
# Fast-forward rejections leave the branch moved; anything else is an error.
CLAIM_ATTEMPTS = 3


class ClaimError(RuntimeError):
    """Raised when git fails in a way that says nothing about who owns an item."""


def pr_is_approved(pr: dict) -> bool:
    """Return True if the gatekeeper approved the PR (review decision or label)."""
    labels = {label["name"] if isinstance(label, dict) else label for label in pr.get("labels") or []}
    return pr.get("reviewDecision") == "APPROVED" or GATEKEEPER_APPROVED_LABEL in labels


def _read_payload(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def claim_pr(claims_dir: Path, number: int, agent: str, tick_lock: Path, tick_token: str) -> bool:
    """Return True if this tick now owns the PR.

    The claim is linked to the tick lock, so it is reclaimable as soon as the tick
    that owns it no longer holds its lock, and never while it does.
    """
    path = claims_dir / f"pr-{number}"
    extra = {"agent": agent, "ownerLock": str(tick_lock), "ownerToken": tick_token}
    if acquire_lock_token(path, extra=extra) is not None:
        return True
    return _read_payload(path).get("ownerToken") == tick_token


def pr_claim_holder(claims_dir: Path, number: int) -> str:
    payload = _read_payload(claims_dir / f"pr-{number}")
    holder = f"agent {payload.get('agent', '?')}"
    owner_lock = payload.get("ownerLock")
    if owner_lock:
        holder += f" (tick lock: {describe_lock(Path(owner_lock))})"
    return holder


def owns_pr(claims_dir: Path, number: int, tick_token: str) -> bool:
    return bool(tick_token) and _read_payload(claims_dir / f"pr-{number}").get("ownerToken") == tick_token


def release_pr(claims_dir: Path, number: int, tick_token: str) -> bool:
    """Release the PR claim if this tick owns it; return True if it was released."""
    path = claims_dir / f"pr-{number}"
    payload = _read_payload(path)
    if not tick_token or payload.get("ownerToken") != tick_token or not payload.get("token"):
        return False
    release_lock(path, token=payload["token"])
    return True


def release_tick_claims(claims_dir: Path, tick_token: str) -> list[str]:
    """Release every PR claim owned by this tick; return labels such as 'PR #7'."""
    if not tick_token or not claims_dir.is_dir():
        return []
    released = []
    for path in sorted(claims_dir.iterdir()):
        match = PR_CLAIM_NAME.fullmatch(path.name)
        if match and release_pr(claims_dir, int(match.group(1)), tick_token):
            released.append(f"PR #{match.group(1)}")
    return released


@dataclass(frozen=True)
class IssueClaim:
    sha: str
    agent: str | None
    host: str | None
    state: str
    time: int
    branch: str | None

    def owned_by(self, agent: str, host: str) -> bool:
        # Claims written before the host trailer existed match on the agent only.
        return self.state == ACTIVE and self.agent == agent and self.host in (None, host)

    def age(self) -> int:
        return int(time.time()) - self.time


def parse_claim(sha: str, message: str, commit_time: int) -> IssueClaim:
    """Parse a claim commit. Claims written before the state trailers count as active."""
    trailers = dict(TRAILER_PATTERN.findall(message))
    try:
        claimed_at = int(trailers.get(TIME_TRAILER, commit_time))
    except ValueError:
        claimed_at = commit_time
    return IssueClaim(
        sha=sha,
        agent=trailers.get(OWNER_TRAILER),
        host=trailers.get(HOST_TRAILER),
        state=RELEASED if trailers.get(STATE_TRAILER) == RELEASED else ACTIVE,
        time=claimed_at,
        branch=trailers.get(BRANCH_TRAILER),
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8"
    )


def _git_out(repo: Path, *args: str) -> str:
    result = _git(repo, *args)
    if result.returncode != 0:
        raise ClaimError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _remote_tips(repo: Path, pattern: str) -> dict[str, str]:
    out = _git_out(repo, "ls-remote", "origin", pattern)
    tips = {}
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith(WIP_REF_PREFIX):
            tips[ref] = sha
    return tips


def _remote_tip(repo: Path, number: int) -> str | None:
    return _remote_tips(repo, f"{WIP_REF_PREFIX}{number}").get(f"{WIP_REF_PREFIX}{number}")


def _read_claims(repo: Path, tips: dict[str, str]) -> dict[str, IssueClaim]:
    """Fetch the claim commits (into FETCH_HEAD only, no local refs) and parse them."""
    if not tips:
        return {}
    _git_out(repo, "fetch", "--no-tags", "--no-write-fetch-head", "origin", *sorted(tips))
    claims = {}
    for ref, sha in tips.items():
        out = _git_out(repo, "log", "-1", "--format=%ct%n%B", sha)
        commit_time, _, message = out.partition("\n")
        claims[ref] = parse_claim(sha, message, int(commit_time))
    return claims


def _push_claim(repo: Path, number: int, parent: str | None, base: str, agent: str,
                state: str, branch: str | None) -> bool:
    """Push one claim commit as a fast-forward; return False if the branch moved first."""
    ref = f"{WIP_REF_PREFIX}{number}"
    lines = [
        f"devloops claim: issue #{number} {state}",
        "",
        f"{OWNER_TRAILER}: {agent}",
        f"{HOST_TRAILER}: {socket.gethostname()}",
        f"{STATE_TRAILER}: {state}",
        f"{TIME_TRAILER}: {int(time.time())}",
    ]
    if branch:
        lines.append(f"{BRANCH_TRAILER}: {branch}")
    lines.append(f"Devloops-Claim-Id: {uuid.uuid4().hex}")
    start = parent or base
    commit = _git_out(
        repo, "-c", "user.name=devloops", "-c", "user.email=devloops@users.noreply.github.com",
        "commit-tree", f"{start}^{{tree}}", "-p", start, "-m", "\n".join(lines),
    )
    push = _git(repo, "push", "--no-verify", "origin", f"{commit}:{ref}")
    if push.returncode == 0:
        return True
    if _remote_tip(repo, number) != parent:
        return False
    raise ClaimError(f"claim push failed: {push.stderr.strip()}")


def read_issue_claim(repo: Path, number: int) -> IssueClaim | None:
    tip = _remote_tip(repo, number)
    if tip is None:
        return None
    return _read_claims(repo, {f"{WIP_REF_PREFIX}{number}": tip})[f"{WIP_REF_PREFIX}{number}"]


def claim_issue(repo: Path, number: int, base: str, agent: str, ttl_seconds: int,
                branch: str | None = None) -> dict:
    """Claim, refresh, or take over wip/issue-N. Returns a dict with `status`:

    claimed (free or released), refreshed (already ours), took-over (stale claim of
    another slot), or busy (live claim of another slot, with `owner` and `age`).
    `previousAgent` and `previousBranch` describe the claim this one replaced.
    """
    host = socket.gethostname()
    for _ in range(CLAIM_ATTEMPTS):
        claim = read_issue_claim(repo, number)
        if claim is None:
            if _push_claim(repo, number, None, base, agent, ACTIVE, branch):
                return {"status": "claimed"}
            continue
        if claim.owned_by(agent, host):
            result = {"status": "refreshed"}
            branch_to_record = branch or claim.branch
        elif claim.state == RELEASED:
            result = {"status": "claimed", "previousAgent": claim.agent, "previousBranch": claim.branch}
            branch_to_record = branch
        elif claim.age() > ttl_seconds:
            result = {"status": "took-over", "previousAgent": claim.agent, "previousBranch": claim.branch,
                      "idleSeconds": claim.age()}
            branch_to_record = branch
        else:
            return {"status": "busy", "owner": claim.agent, "host": claim.host, "age": claim.age()}
        if _push_claim(repo, number, claim.sha, base, agent, ACTIVE, branch_to_record):
            return result
    raise ClaimError(f"wip/issue-{number} kept moving during {CLAIM_ATTEMPTS} claim attempts")


def refresh_issue_claim(repo: Path, number: int, agent: str, branch: str | None = None) -> bool:
    """Refresh an active claim this slot owns; return False if it is not ours (any more)."""
    host = socket.gethostname()
    for _ in range(CLAIM_ATTEMPTS):
        claim = read_issue_claim(repo, number)
        if claim is None or not claim.owned_by(agent, host):
            return False
        if _push_claim(repo, number, claim.sha, claim.sha, agent, ACTIVE, branch or claim.branch):
            return True
    raise ClaimError(f"wip/issue-{number} kept moving during {CLAIM_ATTEMPTS} refresh attempts")


def release_issue(repo: Path, number: int, agent: str) -> bool:
    """Push a `released` claim commit if this slot owns the claim; return True if released."""
    host = socket.gethostname()
    for _ in range(CLAIM_ATTEMPTS):
        claim = read_issue_claim(repo, number)
        if claim is None or not claim.owned_by(agent, host):
            return False
        if _push_claim(repo, number, claim.sha, claim.sha, agent, RELEASED, claim.branch):
            return True
    raise ClaimError(f"wip/issue-{number} kept moving during {CLAIM_ATTEMPTS} release attempts")


def live_issue_claims(repo: Path, ttl_seconds: int) -> set[int]:
    """Return issue numbers with an active claim refreshed within the TTL."""
    claims = _read_claims(repo, _remote_tips(repo, f"{WIP_REF_PREFIX}*"))
    return {
        int(ref[len(WIP_REF_PREFIX):])
        for ref, claim in claims.items()
        if ref[len(WIP_REF_PREFIX):].isdigit() and claim.state == ACTIVE and claim.age() <= ttl_seconds
    }


def count_open_work(prs: list[dict], open_issues: set[int], claimed_issues: set[int]) -> int:
    """Count unapproved PRs plus live claims on open issues that have no open PR yet.

    Approved PRs do not count, and neither do claims on closed issues. A claim that
    already has a PR counts once, as that PR.
    """
    covered = set()
    unapproved = 0
    for pr in prs:
        for ref in pr.get("closingIssuesReferences") or []:
            covered.add(ref["number"])
        match = HEAD_ISSUE_PATTERN.search(pr.get("headRefName") or "")
        if match:
            covered.add(int(match.group(1)))
        if not pr_is_approved(pr):
            unapproved += 1
    return unapproved + len((claimed_issues & open_issues) - covered)


def _fetch_open_work(repo: Path, ttl_seconds: int) -> int:
    prs = json.loads(gh(
        "pr", "list", "--author", "@me", "--state", "open", "--limit", "100",
        "--json", "headRefName,reviewDecision,labels,closingIssuesReferences",
    ))
    issues = json.loads(gh("issue", "list", "--state", "open", "--limit", "100", "--json", "number"))
    return count_open_work(prs, {issue["number"] for issue in issues}, live_issue_claims(repo, ttl_seconds))


def _describe_takeover(number: int, result: dict) -> str:
    if result["status"] == "refreshed":
        return f"REFRESHED: issue #{number}"
    text = f"CLAIMED: issue #{number}"
    if result["status"] == "took-over":
        hours = result["idleSeconds"] / 3600
        text = f"TOOK OVER: issue #{number} from agent {result['previousAgent']} (claim idle {hours:.1f}h)"
    if result.get("previousBranch"):
        text += f"; previous work branch: {result['previousBranch']}"
    return text


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    add_agent_id_argument(common)
    # Subcommand options only: a subparser default would overwrite a value given
    # before the subcommand.
    common.add_argument("--token", default=None, help=f"Tick lock token from `tick_gate.py --lock` (default: ${TOKEN_ENV})")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_agent_id_argument(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("pr", "Claim a PR for this tick"), ("release-pr", "Release this tick's PR claim")):
        sub = commands.add_parser(name, help=help_text, parents=[common])
        sub.add_argument("number", type=int)
    issue = commands.add_parser("issue", help="Claim, refresh, or take over wip/issue-N", parents=[common])
    issue.add_argument("number", type=int)
    issue.add_argument("--base", required=True, help="Commit to base a new claim on, for example origin/main")
    issue.add_argument("--branch", default=None, help="Work branch to record on the claim")
    issue.add_argument("--limit", type=int, default=None, help="Maximum open work, including this claim (default: worker.open_pr_limit)")
    issue.add_argument("--ttl-minutes", type=float, default=None, help="Stale after this long without refresh (default: worker.issue_claim_ttl_minutes)")
    release_issue_cmd = commands.add_parser("release-issue", help="Release this slot's issue claim", parents=[common])
    release_issue_cmd.add_argument("number", type=int)
    for sub in (issue, release_issue_cmd):
        sub.add_argument("--repo", type=Path, default=Path("."), help="Git worktree to run in")
    args = parser.parse_args(argv)

    agent = get_agent_id() or DEFAULT_AGENT
    token = args.token or os.environ.get(TOKEN_ENV)
    if not token:
        sys.stderr.write(f"claim error: pass the tick lock token (--token or ${TOKEN_ENV})\n")
        return EXIT_ERROR
    tick_lock = get_lock_path("worker-loop")
    if not renew_lock(tick_lock, token):
        print(f"LOST: this tick no longer owns its tick lock ({describe_lock(tick_lock)})")
        return EXIT_LOST

    cfg = load_config()
    try:
        if args.command == "pr":
            if not claim_pr(get_claims_dir(), args.number, agent, tick_lock, token):
                print(f"BUSY: PR #{args.number} is claimed by {pr_claim_holder(get_claims_dir(), args.number)}")
                return EXIT_BUSY
            print(f"CLAIMED: PR #{args.number}")
            return EXIT_RUN
        if args.command == "release-pr":
            released = release_pr(get_claims_dir(), args.number, token)
            print(f"RELEASED: PR #{args.number}" if released else f"NOT OWNED: PR #{args.number}")
            return EXIT_RUN
        if args.command == "release-issue":
            released = release_issue(args.repo, args.number, agent)
            print(f"RELEASED: issue #{args.number}" if released else f"NOT OWNED: issue #{args.number}")
            return EXIT_RUN

        ttl = int(args.ttl_minutes * 60) if args.ttl_minutes is not None else issue_claim_ttl_seconds(cfg)
        result = claim_issue(args.repo, args.number, args.base, agent, ttl, args.branch)
        if result["status"] == "busy":
            print(f"BUSY: issue #{args.number} is claimed by agent {result['owner']} "
                  f"on host {result['host'] or '?'} (refreshed {result['age']}s ago)")
            return EXIT_BUSY
        if result["status"] == "refreshed":
            print(_describe_takeover(args.number, result))
            return EXIT_RUN
        limit = args.limit if args.limit is not None else open_pr_limit(cfg)
        open_work = _fetch_open_work(args.repo, ttl)
        if open_work > limit:
            release_issue(args.repo, args.number, agent)
            print(f"OVER LIMIT: {open_work} open items, limit {limit}; claim on issue #{args.number} released")
            return EXIT_NOOP
        print(f"{_describe_takeover(args.number, result)} ({open_work} open items)")
        return EXIT_RUN
    except (ClaimError, GhError) as e:
        sys.stderr.write(f"claim error: {e}\n")
        return EXIT_ERROR


if __name__ == "__main__":
    from devloops_lib.config import run_main

    run_main(main)
