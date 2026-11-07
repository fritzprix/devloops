# Dedicated Worktree Setup

All edits, builds, commits, and pushes happen in a dedicated, persistent worktree — **never in the user's checkout**, which keeps its branch and uncommitted work untouched. Skill files, scripts, the state file, and ignored rule files are read from the main checkout (`$MAIN`).

## Setup

- **Bash (Linux/macOS)**:
  ```bash
  MAIN=$(git rev-parse --show-toplevel)       # the user's checkout
  WT="$MAIN/../$(basename "$MAIN").worktrees/worker${DEVLOOPS_AGENT_ID:+-$DEVLOOPS_AGENT_ID}"
  BASE=$(python3 -c "import sys; sys.path.insert(0, '.agents'); from devloops_lib.config import load_config; print(load_config()['project']['base_branch'])") || { echo "cannot resolve project.base_branch (is devloops_lib importable from .agents/ or the source root?)" >&2; exit 1; }
  python3 -c "import subprocess, sys; from pathlib import Path; out = subprocess.run(['git', '-C', sys.argv[1], 'worktree', 'list', '--porcelain'], capture_output=True, text=True).stdout; target = Path(sys.argv[2]).resolve(); sys.exit(0 if any(line.startswith('worktree ') and Path(line[9:].strip()).resolve() == target for line in out.splitlines()) else 1)" "$MAIN" "$WT" \
    || git -C "$MAIN" worktree add --detach "$WT" "origin/$BASE"
  ```
- **PowerShell (Windows)**:
  ```powershell
  $MAIN = git rev-parse --show-toplevel
  $WT = "$MAIN/../$((Split-Path $MAIN -Leaf)).worktrees/worker$(if ($env:DEVLOOPS_AGENT_ID) { "-$env:DEVLOOPS_AGENT_ID" })"
  $BASE = (python3 -c "import sys; sys.path.insert(0, '.agents'); from devloops_lib.config import load_config; print(load_config()['project']['base_branch'])")
  if (-not $BASE) { throw "cannot resolve project.base_branch (is devloops_lib importable from .agents/ or the source root?)" }
  python3 -c "import subprocess, sys; from pathlib import Path; out = subprocess.run(['git', '-C', sys.argv[1], 'worktree', 'list', '--porcelain'], capture_output=True, text=True).stdout; target = Path(sys.argv[2]).resolve(); sys.exit(0 if any(line.startswith('worktree ') and Path(line[9:].strip()).resolve() == target for line in out.splitlines()) else 1)" "$MAIN" "$WT"
  if ($LASTEXITCODE -ne 0) {
      git -C "$MAIN" worktree add --detach "$WT" "origin/$BASE"
  }
  ```

## Worktree Lifecycle

- Keep the worktree persistent between ticks so build caches and dependency directories stay warm; the first tick pays a full build. Do not share build artifacts with `$MAIN`.
- Reinstall dependencies in `$WT` when lockfiles differ from the hash stored in state (`lockHash`).
- Ignored files are not carried over.
- A branch checked out in `$MAIN` cannot be checked out in `$WT`. In that case, work detached on `origin/<head>` and push with `git push origin HEAD:refs/heads/<head>`.
- `$WT` belongs to the worker slot. At the start of a tick with no `current`, reset it only with `python3 "$SKILL_DIR/scripts/guard.py" reset-worktree --token "$TOKEN" --repo "$WT"`:
  - it refuses if this tick no longer owns its lock, and it refuses the main checkout;
  - an unfinished rebase, merge, cherry-pick, or revert is saved as a patch under `<state_dir>/worker-loop/preserved/` and aborted;
  - remaining uncommitted changes, untracked files included, are stashed (`git -C "$WT" stash list`) before `reset --hard` and `clean -fd`.
- Never reset `$MAIN` or another slot's worktree (`worker-<id>`).
