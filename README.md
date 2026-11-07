# devloops

Reusable agent-loop skills for coding agents.

devloops gives a coding agent three loops: **producer**, **worker**, and **gatekeeper**.
Each loop is a skill (`SKILL.md` plus helper scripts). You install the skills into a
project's `.agents/skills/` directory. A per-project file, `devloops.config.yaml`,
holds the project values (repo, branches, commands, protected paths, labels).

## Install with an agent

Paste this into a coding agent opened in your project:

```text
Install devloops into this repository.
1. Run `uvx devloops install`. (Fallback only if uvx is unavailable: clone the devloops repo and run `scripts/install.sh <this repo>`.)
2. If `devloops.config.yaml` does not exist, create it from the installed template and ask me only for values you cannot detect. If it exists, do not change it without asking me.
3. Run `python3 .agents/skills/worker-loop/scripts/doctor.py` and check that every script path mentioned in the installed SKILL.md files exists and runs from any directory. Report the result.
Do not set up schedules (cron or routines).
```

## Why

Most maintenance work is routine: find a problem, write an issue, fix it, review the fix.
devloops lets agents do that routine work in an **adversarial loop**:

- The **producer** audits the code and files candidate issues.
- The **worker** implements approved issues as pull requests.
- The **gatekeeper** is an independent reviewer. It tries to find defects in each PR
  and gates it with a verdict. It does not trust the worker.

The worker and the gatekeeper go back and forth until the gatekeeper approves.
The human does not do this routine work. The human makes only the important decisions:

- which issues to accept (the `approved` label),
- user-visible behavior, permissions, and security-sensitive changes,
- the merge.

Agents never merge. A human always merges.

## How the loop works

```
producer-loop ──► issue (auto:producer)
                     │
gatekeeper-loop ──► validated | rejected      (checks the issue against the code)
                     │
human ───────────► approved                   (accepts the scope)
                     │
worker-loop ─────► PR
                     │
gatekeeper-loop ─► verdict comment + gatekeeper:* label
                     │   changes-requested ─► worker responds ─► re-review ...
                     ▼
                  approve (+ CI green)
                     │
human ───────────► merge
```

- The worker services its open PRs first (conflicts, gatekeeper findings, comments,
  failing CI). It picks a new issue only when no PR needs action.
- The worker skips producer issues that do not have the `approved` label.
- The worker and the gatekeeper can share one GitHub account. So verdicts are PR
  comments with a hidden marker, plus a `gatekeeper:*` label.
- The gatekeeper adds `gatekeeper:needs-human` to security-sensitive PRs and PRs
  that touch `protected_paths`.

### Tick gate

A scheduler starts each loop in **ticks**, through `devloops tick <loop> --agent-cmd '...'`.
The wrapper takes `tick.lock`, starts the agent, renews the lock while the agent runs,
and releases the lock (and the tick's PR claims) on every exit path. If a tick of the
same slot holds the lock, it prints `BUSY` with the holder's pid, start, and last renewal.
For the worker, it first runs a fingerprint check, and it does not start the agent when
nothing changed.

A **slot** is one loop agent (`--agent-id`); the **tick lock** allows one tick per slot;
**claims** keep two worker slots off the same PR or issue. `BUSY` is about the tick lock of
the same slot only: it never means that another worker exists. See
[`scheduling.md`](skills/worker-loop/references/scheduling.md#slots-tick-locks-and-claims).

Inside the agent, or when you start a tick without the wrapper, `tick_gate.py`
decides if there is anything to do:

1. `--lock` takes `tick.lock` and prints `TOKEN <token>`, the tick's proof of ownership.
   Under the wrapper, it recognizes the wrapper's token (`DEVLOOPS_TICK_TOKEN`) and leaves
   the wrapper's lock in place. If a tick of the same slot holds the lock, it prints `BUSY`.
2. It computes a fingerprint with one GitHub GraphQL request (your open PRs:
   head, mergeable state, CI state, last update, last comment; candidate issues:
   last update). It keeps this tick-start fingerprint.
3. If the state has no `current` work and the fingerprint is equal to the saved
   `lastFingerprint`, it prints `NOOP`. The scheduler does not start the agent.
4. At the end of the tick:
   - idle or needsUser tick: `--update --unlock --token <token>` saves the **tick-start** fingerprint;
   - tick that did work, or failed: `--unlock --token <token>` only. The next tick runs again.

   Under the wrapper, `--unlock` keeps the lock, and the wrapper releases it on exit.
   A tick whose lock was taken over (it was not renewed within the TTL, or its wrapper
   died) gets `LOST` and changes neither the lock nor the state.

So a change that happens during a tick is never hidden, and only an idle,
unchanged state gives `NOOP`.

Exit codes: `0` RUN, `1` NOOP, `2` BUSY, `3` ERROR (auth, network, or GraphQL failure),
`4` LOST (this tick no longer owns its lock). The lock TTL is 30 minutes;
`DEVLOOPS_LOCK_TTL_SECONDS` overrides it.

The producer has its own gate, `producer_gate.py`. It picks the most overdue task
(`--next`) from the task periods in the config.

## Requirements

- Python 3.8 or later (no third-party packages; tested with 3.13)
- `git`
- GitHub CLI `gh`, logged in, with push permission to the project repo. Note: issue-only trackers (for example Linear, Jira) still need a git host for pull requests, code reviews, and CI status.
- A coding agent that can load skills from `.agents/skills/`

## Install

The recommended way to install devloops into a project is via `uvx`:

```sh
uvx devloops install                      # install into current project
uvx devloops install --target /path/to/project
```

Alternatively, from a local checkout:

```sh
git clone https://github.com/fritzprix/devloops.git
devloops/scripts/install.sh /path/to/project          # copy
devloops/scripts/install.sh /path/to/project --link   # symlink (follows updates)
```

The script installs:

- `skills/*` to `<project>/.agents/skills/` (`worker-loop`, `gatekeeper-loop`, `producer-loop`)
- `devloops_lib/` to `<project>/.agents/devloops_lib/`
- `templates/devloops.config.yaml` to `<project>/devloops.config.yaml`, if it does not exist

It does not overwrite a skill that already exists. Add the state directory
(default `.devloops/`) to `.gitignore`.

## Configure

Edit `<project>/devloops.config.yaml`:

| Field | Meaning |
| --- | --- |
| `project.repo` | GitHub repo, `owner/name` |
| `project.default_branch` | Default branch of the repo |
| `project.base_branch` | Branch that PRs target |
| `commands.test`, `commands.lint`, `commands.build` | Project commands that the loops use to verify |
| `protected_paths` | Paths that need explicit human approval. Producer: findings only. Gatekeeper: `gatekeeper:needs-human`. |
| `cleanup.protected_branches` | Branches that the `cleanup` task never proposes to delete |
| `state_dir` | Directory for each loop's `state.json` and `tick.lock` (default `.devloops`). `$LOOP_STATE_DIR` overrides it. `--agent-id` scopes the paths to one agent (see `scheduling.md`). |
| `labels.producer` / `validated` / `rejected` / `approved` | Label names for the issue flow |
| `producer.default_period` | Default period for each producer task (e.g. `24h`) |
| `producer.max_new_issues_per_run` | Limit of new issues per run |
| `producer.tasks` | Enabled tasks: `security`, `e2e`, `uiux`, `regression`, `usage-logs`, `doc-drift`, `cleanup` |
| `producer.task_skills` | Optional project skill or command for each task |
| `producer.task_periods` | Period for each task (e.g. `cleanup: 7d`) |
| `producer.task_max_new_issues` | Limit of new issues for each task |
| `gatekeeper.max_issue_validations_per_tick` | Producer issues the gatekeeper validates per tick (default 1) |
| `worker.max_items_per_tick` | PRs and issues the worker finishes per tick (default 1). The wrapper renews the lock, so a long tick keeps it. |
| `worker.open_pr_limit` | Maximum open unapproved PRs plus live issue claims without a PR before the worker stops starting issues (default 5) |
| `worker.issue_claim_ttl_minutes` | An issue claim not refreshed for this long can be taken over by another worker slot (default 120) |
| `scheduler` | Documentation only: `shell`, `cron`, or a harness-specific adapter |

## Run

Check the prerequisites first (Python, GitHub API, `git`, `gh auth`, push permission):

```sh
python3 .agents/skills/worker-loop/scripts/doctor.py
```

Then tell your agent:

| Command | Effect |
| --- | --- |
| `/worker-loop` | Run one tick now |
| `/worker-loop 10m` | Arm a recurring loop every 10 minutes, then run one tick |
| `/worker-loop status` | Show the state (`current`, `needsUser`, `skipped`) |
| `/worker-loop stop` | Stop the recurring loop |

`/gatekeeper-loop` and `/producer-loop` use the same forms. `/gatekeeper-loop 10m --pr N`
reviews one PR. `/producer-loop <task>` runs one producer task.

A cron example that starts the agent only when the gate says RUN (using the `devloops tick` wrapper):

```cron
*/10 * * * * cd /path/to/project && devloops tick worker --agent-cmd '/path/to/agent-runner "/worker-loop"'
```

See `.agents/skills/worker-loop/references/scheduling.md` (installed) or `skills/worker-loop/references/scheduling.md` (repo) for the shell-loop and harness adapters.

## Labels

| Label | Set by | Meaning |
| --- | --- | --- |
| `auto:producer`, `auto:<task>` | producer | Candidate issue from a producer task |
| `validated` / `rejected` | gatekeeper | The issue matches / does not match the code |
| `approved` | **human only** | Scope accepted; the worker can take a producer issue |
| `gatekeeper:changes-requested` | gatekeeper | The worker must answer the findings |
| `gatekeeper:approved` | gatekeeper | Ready for a human merge |
| `gatekeeper:escalated` | gatekeeper | Worker and gatekeeper disagree; a human must decide |
| `gatekeeper:needs-human` | gatekeeper | Security-sensitive or protected paths |

## Safety rules

The skills encode these rules:

- No loop merges, enables auto-merge, tags, or publishes.
- The worker does not close issues or delete remote branches. It force-pushes only
  with `--force-with-lease` on its own PR branch after a rebase. It rebases; it never
  merges the base into a feature branch.
- The worker works only in its own worktree, never in the user's checkout.
  It does one unit of work per tick and edits only files for that PR or issue.
- The worker stops starting issues at `worker.open_pr_limit` open unapproved PRs (default 5).
  It fixes failing CI from the reproduced cause, runs the full local verification
  before every push, and never reruns a job only to get green.
- A worker tick that lost its lock or claim cannot push, write PR comments, or run
  destructive git commands (`guard.py`). Resetting its worktree first stashes
  uncommitted changes and saves an unfinished rebase or merge as a patch.
- The producer only creates issues. It never edits code, comments, or changes labels.
  It does not put secrets or private paths in issues.
- The gatekeeper never adds `approved`, never closes issues, and never changes code.
  A push after an approve always needs a new review.
- PR bodies, comments, and CI logs are untrusted data. The loops never run
  instructions from them.

## Testing

```sh
python3 -m unittest discover -s tests -p 'test_*.py'   # or: python3 -m pytest -q
uv build && python3 scripts/ci_smoke.py --dist dist     # wheel + relocated-install smoke test
```

`tests/test_concurrency.py` starts real competing processes (tick locks, wrapper
heartbeat, lost ownership, PR and issue claim races). CI (`.github/workflows/ci.yml`)
runs the tests, the build, and the smoke test on Linux, Windows, and macOS with
Python 3.8 and 3.13, on every PR head and on `main`.

## Layout

| Path | Purpose |
| --- | --- |
| `skills/` | `worker-loop`, `gatekeeper-loop`, `producer-loop`. Each has `SKILL.md`, `references/`, `scripts/`. |
| `devloops_lib/` | Shared code: config, tick gate, claims, ownership guard, PR status, issue queue, producer, doctor |
| `templates/devloops.config.yaml` | Project config template |
| `scripts/install.sh` | Copies or links the skills and `devloops_lib` into a project |
| `tests/` | Unit tests |
