# Dedicated Worktree Setup

All edits, builds, commits, and pushes happen in a dedicated, persistent worktree — **never in the user's checkout**, which keeps its branch and uncommitted work untouched. Skill files, scripts, the state file, and ignored rule files are read from the main checkout (`$MAIN`).

## Setup

- **Bash (Linux/macOS)**:
  ```bash
  MAIN=$(git rev-parse --show-toplevel)       # the user's checkout
  WT="$MAIN/../$(basename "$MAIN").worktrees/worker"
  BASE=$(python3 -c "import sys; sys.path.insert(0, '.agents'); from devloops_lib.config import load_config; print(load_config()['project']['base_branch'])") || { echo "cannot resolve project.base_branch (is devloops_lib importable from .agents/ or the source root?)" >&2; exit 1; }
  python3 -c "import subprocess, sys; from pathlib import Path; out = subprocess.run(['git', '-C', sys.argv[1], 'worktree', 'list', '--porcelain'], capture_output=True, text=True).stdout; target = Path(sys.argv[2]).resolve(); sys.exit(0 if any(line.startswith('worktree ') and Path(line[9:].strip()).resolve() == target for line in out.splitlines()) else 1)" "$MAIN" "$WT" \
    || git -C "$MAIN" worktree add --detach "$WT" "origin/$BASE"
  ```
- **PowerShell (Windows)**:
  ```powershell
  $MAIN = git rev-parse --show-toplevel
  $WT = "$MAIN/../$((Split-Path $MAIN -Leaf)).worktrees/worker"
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
- `$WT` belongs to the worker: at the start of a tick with no `current`, `git -C "$WT" reset --hard && git -C "$WT" clean -fd` is allowed. Never run these against `$MAIN`.
