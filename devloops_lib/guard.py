#!/usr/bin/env python3
"""Ownership guard for pushes and destructive git commands of a worker tick.

  check           Prove that this tick still owns its tick lock (and renew it), and,
                  with --pr or --issue, that it still owns that item's claim (the
                  issue claim is refreshed).
  run -- CMD      Run `check`, then CMD. If ownership is lost, CMD never runs.
  reset-worktree  Run `check`, then reset the worker worktree without losing work:
                  an unfinished rebase, merge, cherry-pick, or revert is saved as a
                  patch under <state_dir>/<loop>/preserved/ and aborted; remaining
                  changes, untracked files included, are stashed (`git stash list`)
                  before `reset --hard` and `clean -fd`. The main checkout is refused.

Exit codes:
  0 - owned (`run`: the exit code of CMD)
  3 - error
  4 - LOST: this tick no longer owns the lock or the claim; nothing was changed
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from devloops_lib.claims import ClaimError, owns_pr, pr_claim_holder, read_issue_claim, refresh_issue_claim
from devloops_lib.config import add_agent_id_argument, get_agent_id, get_claims_dir, get_lock_path
from devloops_lib.gh import GhError
from devloops_lib.tick_gate import DEFAULT_AGENT, EXIT_ERROR, EXIT_LOST, EXIT_RUN, TOKEN_ENV, describe_lock, renew_lock

# Marker file or directory in the git dir -> the command that aborts the operation.
UNFINISHED_OPERATIONS = (
    ("rebase-merge", ["rebase", "--abort"]),
    ("rebase-apply", ["rebase", "--abort"]),
    ("MERGE_HEAD", ["merge", "--abort"]),
    ("CHERRY_PICK_HEAD", ["cherry-pick", "--abort"]),
    ("REVERT_HEAD", ["revert", "--abort"]),
)


class GuardError(RuntimeError):
    """Raised when a git command the guard needs fails."""


def ownership_problem(token: str, loop: str, pr: int | None = None, issue: int | None = None,
                      repo: Path | None = None) -> str | None:
    """Return None if this tick owns the lock and the item, else what was lost."""
    lock = get_lock_path(loop)
    if not renew_lock(lock, token):
        return f"tick lock ({describe_lock(lock)})"
    if pr is not None and not owns_pr(get_claims_dir(), pr, token):
        return f"PR #{pr} claim (held by {pr_claim_holder(get_claims_dir(), pr)})"
    if issue is not None:
        agent = get_agent_id() or DEFAULT_AGENT
        if not refresh_issue_claim(repo or Path("."), issue, agent):
            claim = read_issue_claim(repo or Path("."), issue)
            owner = f"agent {claim.agent}, {claim.state}" if claim else "no claim"
            return f"issue #{issue} claim ({owner})"
    return None


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise GuardError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _identity_args(repo: Path) -> list[str]:
    """Return a fallback committer identity for `git stash` when none is configured."""
    probe = subprocess.run(["git", "-C", str(repo), "var", "GIT_COMMITTER_IDENT"], capture_output=True, text=True)
    if probe.returncode == 0:
        return []
    return ["-c", "user.name=devloops", "-c", "user.email=devloops@users.noreply.github.com"]


def preserve_and_reset(repo: Path, preserve_dir: Path, label: str) -> list[str]:
    """Reset a linked worktree after saving its uncommitted work; return what was saved."""
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir").strip())
    common_dir = Path(_git(repo, "rev-parse", "--git-common-dir").strip())
    if not common_dir.is_absolute():
        common_dir = (repo / common_dir).resolve()
    if git_dir.resolve() == common_dir.resolve():
        raise GuardError(f"{repo} is a main checkout, not a linked worker worktree; refusing to reset it")

    saved = []
    for marker, abort in UNFINISHED_OPERATIONS:
        if (git_dir / marker).exists():
            preserve_dir.mkdir(parents=True, exist_ok=True)
            patch = preserve_dir / f"{time.strftime('%Y%m%dT%H%M%S')}-{abort[0]}.patch"
            patch.write_text(
                f"# {label}: unfinished {abort[0]} in {repo}\n# git status:\n"
                + "".join(f"#   {line}\n" for line in _git(repo, "status", "--porcelain").splitlines())
                + _git(repo, "diff", "HEAD"),
                encoding="utf-8",
            )
            _git(repo, *abort)
            saved.append(f"patch {patch}")
            break

    if _git(repo, "status", "--porcelain", "--untracked-files=all").strip():
        _git(repo, *_identity_args(repo), "stash", "push", "--include-untracked", "-m", f"devloops preserved: {label}")
        saved.append(f"stash {_git(repo, 'rev-parse', '--short', 'stash@{0}').strip()} ({label})")

    _git(repo, "reset", "--hard")
    _git(repo, "clean", "-fd")
    return saved


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    add_agent_id_argument(common)
    # Subcommand options only: a subparser default would overwrite a value given
    # before the subcommand.
    common.add_argument("--token", default=None, help=f"Tick lock token from `tick_gate.py --lock` (default: ${TOKEN_ENV})")
    common.add_argument("--loop", default="worker-loop", help="Loop whose tick lock to check (default: worker-loop)")
    common.add_argument("--repo", type=Path, default=Path("."), help="Worker worktree")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("check", "Check ownership"), ("run", "Check ownership, then run the command after --")):
        sub = commands.add_parser(name, help=help_text, parents=[common])
        sub.add_argument("--pr", type=int, default=None, help="Also require this tick's claim on PR N")
        sub.add_argument("--issue", type=int, default=None, help="Also require (and refresh) this slot's claim on issue N")
        if name == "run":
            sub.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to run, after --")
    commands.add_parser("reset-worktree", help="Check ownership, preserve uncommitted work, reset --repo", parents=[common])
    args = parser.parse_args(argv)

    token = args.token or os.environ.get(TOKEN_ENV)
    if not token:
        sys.stderr.write(f"guard error: pass the tick lock token (--token or ${TOKEN_ENV})\n")
        return EXIT_ERROR
    cmd = list(getattr(args, "cmd", None) or [])
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if args.command == "run" and not cmd:
        parser.error("run needs a command after --")

    try:
        lost = ownership_problem(token, args.loop, getattr(args, "pr", None), getattr(args, "issue", None), args.repo)
        if lost:
            print(f"LOST: {lost}; nothing was run")
            return EXIT_LOST
        if args.command == "check":
            print("OWNED")
            return EXIT_RUN
        if args.command == "run":
            return subprocess.run(cmd).returncode
        agent = get_agent_id() or DEFAULT_AGENT
        label = f"agent {agent}, tick {token[:8]}, {time.strftime('%Y-%m-%dT%H:%M:%S%z')}"
        saved = preserve_and_reset(args.repo, get_lock_path(args.loop).parent / "preserved", label)
        for item in saved:
            print(f"PRESERVED: {item}")
        print(f"RESET: {args.repo}")
        return EXIT_RUN
    except (ClaimError, GhError, GuardError) as e:
        sys.stderr.write(f"guard error: {e}\n")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
