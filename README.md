# devloops

Reusable agent-loop skills for coding agents.

devloops gives a coding agent four loops: **producer**, **worker**, **gatekeeper**, and an optional **PM orchestrator** (`pm-loop`).
Each loop is a self-contained skill (`SKILL.md` plus helper scripts). You install the
skills into your project with `npx skills add fritzprix/devloops`. A per-project file,
`devloops.config.yaml`, holds the project values (repo, branches, commands, protected
paths, labels).

## Install with an agent

Paste this into a coding agent opened in your project:

```text
Install devloops into this repository.
1. Run `npx skills add fritzprix/devloops --skill '*' -y` (add `-a claude-code` if you are Claude Code). Fallback only if Node.js is unavailable: `uvx --from git+https://github.com/fritzprix/devloops devloops install`.
2. Find where the worker-loop skill was installed (for example `.agents/skills/worker-loop` or `.claude/skills/worker-loop`); call it WORKER below.
3. If `devloops.config.yaml` does not exist, run `python3 WORKER/scripts/doctor.py --init`, then fill it in and ask me only for values you cannot detect. If it exists, do not change it without asking me.
4. Run `python3 WORKER/scripts/doctor.py` and check that every script path mentioned in the installed SKILL.md files exists and runs from any directory. Report the result.
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
- The gatekeeper **expands its own prerequisites** (CI workflows, review rubrics, fixtures)
  when missing, instead of blocking on `needs-human`. Humans collaborate only when judgment
  criteria are ambiguous (draft rubric for confirm/edit). `gatekeeper:needs-human` remains for
  loop-skill edits and ambiguous criteria. Sensitive-looking paths are read and get stricter
  review from the gatekeeper, not a human gate.

### Tick gate

A scheduler starts each loop in **ticks**, through the skill's `scripts/tick.py --agent-cmd '...'` (or `devloops tick <loop>`).
The wrapper takes `tick.lock`, starts the agent, renews the lock while the agent runs,
and releases the lock (and the tick's PR claims) on every exit path. If a tick of the
same slot holds the lock, it prints `BUSY` with the holder's pid, start, and last renewal.
For the worker, it first runs a fingerprint check, and it does not start the agent when
nothing changed.

A **slot** is one loop agent (`--agent-id`, or auto-assigned `default`, `default-2`, … on
`tick_gate.py --lock`); the **tick lock** allows one tick per slot;
**claims** keep two worker slots off the same PR or issue. `BUSY` is about the tick lock of
the same slot only: it never means that another worker exists. See
[`scheduling.md`](skills/worker-loop/references/scheduling.md#slots-tick-locks-and-claims).

Inside the agent, or when you start a tick without the wrapper, `tick_gate.py`
decides if there is anything to do:

1. `--lock` takes `tick.lock`, prints `AGENT <id>` when the slot was auto-assigned, then
   `TOKEN <token>` (the tick's proof of ownership). A detached heartbeat keeps the lock alive
   for long ticks without manual `--renew`.
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
- A coding agent that loads skills (`SKILL.md` directories)
- To install: Node.js 22.20 or later for `npx skills` (or `uv` for the Python installer)

## Install

devloops installs with the [`skills` CLI](https://github.com/vercel-labs/skills)
(Node.js 22.20 or later). Each skill is self-contained: it ships its own copy of the
shared code (`scripts/devloops_lib/`) and the config template (`assets/`), so nothing
else needs to be installed. Run from your project root:

```sh
# 1. Install the three skills (prompts for your coding agents)
npx skills add fritzprix/devloops --skill '*'

# 2. Create devloops.config.yaml from the template, then edit it (see Configure)
python3 .agents/skills/worker-loop/scripts/doctor.py --init

# 3. Check the prerequisites
python3 .agents/skills/worker-loop/scripts/doctor.py
```

Then add the state directory (default `.devloops/`) to `.gitignore`.

Where the skills land depends on the agents you pick. Cursor, Codex, Gemini CLI,
GitHub Copilot, and most other agents read `.agents/skills/`; Claude Code reads
`.claude/skills/`. The commands in this README use `.agents/skills/`: replace it with
your agent's directory. For a non-interactive install, name the agents:

```sh
npx skills add fritzprix/devloops --skill '*' -a cursor claude-code -y
```

The CLI records the installed versions in `skills-lock.json`; commit it with the skills.

### Update

```sh
npx skills check     # list available updates
npx skills update    # update the installed skills
```

Always update the three skills together. They share state and lock files, and
`doctor.py` fails when their copies of `devloops_lib` differ.

### Without Node.js

Run the Python installer straight from GitHub (devloops is not on PyPI yet). It copies
the skills to `.agents/skills/` and creates `devloops.config.yaml` if it does not exist:

```sh
uvx --from git+https://github.com/fritzprix/devloops devloops install
uvx --from git+https://github.com/fritzprix/devloops devloops install --target /path/to/project
```

From a local checkout, `scripts/install.sh /path/to/project` does the same, and
`--link` symlinks the skills instead (they follow the checkout). These installers
never overwrite a skill that already exists: to update, delete the three skill
directories and install again.

## Configure

Edit `<project>/devloops.config.yaml`:

| Field | Meaning |
| --- | --- |
| `project.repo` | GitHub repo, `owner/name` |
| `project.default_branch` | Default branch of the repo |
| `project.base_branch` | Branch that PRs target |
| `commands.test`, `commands.lint`, `commands.build` | Project commands that the loops use to verify |
| `environment.capabilities` | Declared agent tools (browser, screen_capture, etc.); worker cites a **missing** name before `needsUser`. See escalation-policy.md. |
| `protected_paths` | High-attention paths. Producer: findings only. Worker: edits when in scope and unambiguous. Gatekeeper: `gatekeeper:needs-human` for stricter review. See `skills/worker-loop/references/escalation-policy.md`. |
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
| `worker.open_pr_limit` | Throttle: worker stops starting issues (idles, no `needsUser`) while this many unapproved PRs plus live issue claims without a PR are open (default 5) |
| `worker.issue_claim_ttl_minutes` | An issue claim not refreshed for this long can be taken over by another worker slot (default 120) |
| `scheduler` | Documentation only: `shell`, `cron`, or a harness-specific adapter |

## Run

Check the prerequisites first (Python, GitHub API, `git`, `gh auth`, push permission,
matching skill versions):

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

`/pm-loop` orchestrates the three role loops: only the PM arms a cron; it cascades ticks downward while peer schedulers stay disarmed. See `pm-loop/SKILL.md`.

A cron example that starts the agent only when the gate says RUN, through each skill's
tick wrapper `scripts/tick.py`:

```cron
*/10 * * * * cd /path/to/project && python3 .agents/skills/worker-loop/scripts/tick.py --agent-cmd '/path/to/agent-runner "/worker-loop"'
```

The gatekeeper and producer have the same wrapper in their own skill
(`gatekeeper-loop/scripts/tick.py`, `producer-loop/scripts/tick.py`). `devloops tick <loop>`
is the same wrapper, for setups that installed the `devloops` command
(`uv tool install git+https://github.com/fritzprix/devloops`).

See [`scheduling.md`](skills/worker-loop/references/scheduling.md) (installed at
`worker-loop/references/scheduling.md`) for the shell-loop and harness adapters.

## Labels

| Label | Set by | Meaning |
| --- | --- | --- |
| `auto:producer`, `auto:<task>` | producer | Candidate issue from a producer task |
| `validated` / `rejected` | gatekeeper | The issue matches / does not match the code |
| `approved` | **human only** | Scope accepted; the worker can take a producer issue |
| `gatekeeper:changes-requested` | gatekeeper | The worker must answer the findings |
| `gatekeeper:approved` | gatekeeper | Ready for a human merge |
| `gatekeeper:escalated` | gatekeeper | Review round limit or repeated dispute on the same finding |
| `gatekeeper:needs-human` | gatekeeper | Stricter review before approve (security, protected paths, loop skills, no CI on head) |

## Safety rules

The skills encode these rules:

- No loop merges, enables auto-merge, tags, or publishes.
- The worker does not close issues or delete remote branches. It force-pushes only
  with `--force-with-lease` on its own PR branch after a rebase. It rebases; it never
  merges the base into a feature branch.
- The worker works only in its own worktree, never in the user's checkout.
  It does one unit of work per tick and edits only files for that PR or issue.
- The worker throttles new issues at `worker.open_pr_limit` open unapproved PRs (default 5) without `needsUser`.
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

Edit shared code only in `devloops_lib/` and `templates/`, then run
`python3 scripts/sync_skill_lib.py` and commit the copies it writes into each skill.
`tests/test_skill_lib_sync.py` fails while a copy is out of date.

`tests/test_concurrency.py` starts real competing processes (tick locks, wrapper
heartbeat, lost ownership, PR and issue claim races). CI (`.github/workflows/ci.yml`)
runs the tests, the build, and the smoke test on Linux, Windows, and macOS with
Python 3.8 and 3.13, on every PR head and on `main`.

## Layout

| Path | Purpose |
| --- | --- |
| `skills/` | `worker-loop`, `gatekeeper-loop`, `producer-loop`, `pm-loop`. Each has `SKILL.md`, `references/`, `scripts/` (with a generated `devloops_lib/` copy), and `assets/` (generated config template). |
| `devloops_lib/` | Shared code (the source of the copies): config, tick gate, claims, ownership guard, PR status, issue queue, producer, doctor |
| `templates/devloops.config.yaml` | Project config template (the source of the copies) |
| `scripts/sync_skill_lib.py` | Copies `devloops_lib/` and the template into each skill; `--check` reports stale copies |
| `scripts/install.sh` | Copies or links the skills and the config template into a project |
| `tests/` | Unit tests |
