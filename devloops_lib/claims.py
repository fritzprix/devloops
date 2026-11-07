#!/usr/bin/env python3
"""Per-item claims, so workers on one repo and GitHub account do not take the same work.

A PR claim is a lock file, <state_dir>/claims/pr-N, shared by agents on one machine.
An issue claim is a remote branch, wip/issue-N, so it also works across machines.

Exit codes:
  0 - claimed, or already owned by this agent
  1 - claim kept, but open work is over the limit: do not start this item
  2 - another agent owns the item
  3 - error (git or GitHub failure); nothing is known about the owner
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path

from devloops_lib.config import add_agent_id_argument, get_agent_id, get_claims_dir
from devloops_lib.gh import GhError, gh
from devloops_lib.tick_gate import (
    EXIT_BUSY,
    EXIT_ERROR,
    EXIT_NOOP,
    EXIT_RUN,
    acquire_lock_token,
    release_lock,
)

DEFAULT_AGENT = "default"
MAX_OPEN_WORK = 5
APPROVED_LABEL = "gatekeeper:approved"
OWNER_TRAILER = "Devloops-Claim-Agent"
WIP_REF_PREFIX = "refs/heads/wip/issue-"
HEAD_ISSUE_PATTERN = re.compile(r"-(\d+)$")


class ClaimError(RuntimeError):
    """Raised when git fails in a way that says nothing about who owns an item."""


def _read_payload(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def claim_pr(claims_dir: Path, number: int, agent: str) -> bool:
    """Return True if this agent now owns the PR. A claim older than the lock TTL is reclaimed."""
    path = claims_dir / f"pr-{number}"
    if acquire_lock_token(path, extra={"agent": agent}) is not None:
        return True
    return _read_payload(path).get("agent") == agent


def release_pr(claims_dir: Path, number: int, agent: str) -> bool:
    """Release the PR claim if this agent owns it; return True if it was released."""
    path = claims_dir / f"pr-{number}"
    payload = _read_payload(path)
    if payload.get("agent") != agent or not payload.get("token"):
        return False
    release_lock(path, token=payload["token"])
    return True


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8"
    )


def _git_out(repo: Path, *args: str) -> str:
    result = _git(repo, *args)
    if result.returncode != 0:
        raise ClaimError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _issue_owner(repo: Path, number: int) -> str | None:
    """Return the agent recorded on wip/issue-N; raise ClaimError if the branch cannot be read."""
    _git_out(repo, "fetch", "--no-tags", "origin", f"{WIP_REF_PREFIX}{number}")
    message = _git_out(repo, "log", "-1", "--format=%B", "FETCH_HEAD")
    match = re.search(rf"^{OWNER_TRAILER}: (.+)$", message, re.M)
    return match.group(1).strip() if match else None


def claim_issue(repo: Path, number: int, base: str, agent: str) -> bool:
    """Push wip/issue-N without force; return True if this agent owns the issue.

    The claim commit is unique to this attempt. If two agents pushed the base commit
    itself, the second push would succeed as "up to date" and both would win. With
    unique commits, the second push is a rejected non-fast-forward.
    """
    tree = f"{base}^{{tree}}"
    message = f"devloops claim: issue #{number}\n\n{OWNER_TRAILER}: {agent}\nDevloops-Claim-Id: {uuid.uuid4().hex}"
    commit = _git_out(
        repo, "-c", "user.name=devloops", "-c", "user.email=devloops@users.noreply.github.com",
        "commit-tree", tree, "-p", base, "-m", message,
    )
    push = _git(repo, "push", "origin", f"{commit}:{WIP_REF_PREFIX}{number}")
    if push.returncode == 0:
        return True
    try:
        return _issue_owner(repo, number) == agent
    except ClaimError as e:
        raise ClaimError(f"claim push failed: {push.stderr.strip()} ({e})") from e


def count_open_work(prs: list[dict], open_issues: set[int], wip_issues: set[int]) -> int:
    """Count unapproved PRs plus claimed open issues that have no open PR yet.

    Approved PRs do not count, and neither do claims on closed issues, because wip
    branches are never deleted. A claim that already has a PR counts once, as that PR.
    """
    covered = set()
    unapproved = 0
    for pr in prs:
        for ref in pr.get("closingIssuesReferences") or []:
            covered.add(ref["number"])
        match = HEAD_ISSUE_PATTERN.search(pr.get("headRefName") or "")
        if match:
            covered.add(int(match.group(1)))
        labels = {label["name"] for label in pr.get("labels") or []}
        if pr.get("reviewDecision") != "APPROVED" and APPROVED_LABEL not in labels:
            unapproved += 1
    return unapproved + len((wip_issues & open_issues) - covered)


def _wip_issues(repo: Path) -> set[int]:
    out = _git_out(repo, "ls-remote", "origin", f"{WIP_REF_PREFIX}*")
    return {int(line.rsplit("-", 1)[1]) for line in out.splitlines() if line.strip()}


def _fetch_open_work(repo: Path) -> int:
    prs = json.loads(gh(
        "pr", "list", "--author", "@me", "--state", "open", "--limit", "100",
        "--json", "headRefName,reviewDecision,labels,closingIssuesReferences",
    ))
    issues = json.loads(gh("issue", "list", "--state", "open", "--limit", "100", "--json", "number"))
    return count_open_work(prs, {issue["number"] for issue in issues}, _wip_issues(repo))


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    add_agent_id_argument(common)
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, parents=[common]
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("pr", "Claim a PR"), ("release-pr", "Release this agent's PR claim")):
        sub = commands.add_parser(name, help=help_text, parents=[common])
        sub.add_argument("number", type=int)
    issue = commands.add_parser("issue", help="Claim an issue with a wip/issue-N branch", parents=[common])
    issue.add_argument("number", type=int)
    issue.add_argument("--base", required=True, help="Commit to base the claim on, for example origin/main")
    issue.add_argument("--repo", type=Path, default=Path("."), help="Git worktree to run in")
    issue.add_argument("--limit", type=int, default=MAX_OPEN_WORK, help="Maximum open work, including this claim")
    args = parser.parse_args(argv)

    agent = get_agent_id() or DEFAULT_AGENT
    try:
        if args.command == "pr":
            if not claim_pr(get_claims_dir(), args.number, agent):
                print(f"BUSY: another agent owns PR #{args.number}")
                return EXIT_BUSY
            print(f"CLAIMED: PR #{args.number}")
            return EXIT_RUN
        if args.command == "release-pr":
            released = release_pr(get_claims_dir(), args.number, agent)
            print(f"RELEASED: PR #{args.number}" if released else f"NOT OWNED: PR #{args.number}")
            return EXIT_RUN

        if not claim_issue(args.repo, args.number, args.base, agent):
            print(f"BUSY: another agent owns issue #{args.number}")
            return EXIT_BUSY
        open_work = _fetch_open_work(args.repo)
        if open_work > args.limit:
            print(f"OVER LIMIT: {open_work} open items, limit {args.limit}; claim on issue #{args.number} kept")
            return EXIT_NOOP
        print(f"CLAIMED: issue #{args.number} ({open_work} open items)")
        return EXIT_RUN
    except (ClaimError, GhError) as e:
        sys.stderr.write(f"claim error: {e}\n")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
