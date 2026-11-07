---
name: producer-loop
description: 'Autonomous producer loop that periodically audits codebase areas (security, e2e, uiux, regression, usage-logs, doc-drift, cleanup) and files structured candidate issues. Invocation: /producer-loop, /producer-loop <interval>, /producer-loop <task>, /producer-loop <task> <interval>, /producer-loop status, /producer-loop stop.'
---

# Producer Loop

Autonomous loop that scans the repository across specialized audit areas and files deduplicated, high-signal candidate issues.
Issues are tagged with `labels.producer` (`auto:producer`) and task labels (`auto:<task>`), ready for the gatekeeper loop to validate.

See [`$SKILL_DIR/../worker-loop/references/scheduling.md`](../worker-loop/references/scheduling.md) for background scheduling adapters (shell loop, cron, harness adapters) which are reused directly by reference.

**Path convention**: `$SKILL_DIR` denotes the directory containing this SKILL.md (skill loaders report it on load). All script commands below use `"$SKILL_DIR/scripts/..."`, so they resolve from any current directory — in a repo checkout or in the installed `.agents/skills/` layout.

## Commands

| Command | Behavior |
| --- | --- |
| `/producer-loop` | Run one tick now: selects the most overdue due task. If no task is due, prints `NOOP`. |
| `/producer-loop <interval>` | Arm a recurring loop (e.g. `1h`, `24h`), then run one tick now. |
| `/producer-loop <task>` | Run specified task immediately (e.g. `/producer-loop security`). |
| `/producer-loop <task> <interval>` | Arm recurring execution for that specific task, then run one tick now. |
| `/producer-loop status` | Print loop state summary (`lastRun`, periods, fingerprints count, scheduler PID); do no work. |
| `/producer-loop stop` | Disarm the recurring loop by terminating the recorded PID. |

## Tasks

Documented in detail in [references/tasks.md](references/tasks.md):
- `security`: Audits code, dependencies, and configuration for vulnerabilities and exposed secrets.
- `e2e`: Audits end-to-end integration journeys and contract drifts.
- `uiux`: Audits UI accessibility, responsive design, and error handling.
- `regression`: Analyzes recent changes on the base branch for missing regression coverage.
- `usage-logs`: Analyzes logs and telemetry for recurring errors and performance warnings.
- `doc-drift`: Audits synchronization between implementation and documentation.
- `cleanup`: Proposes removal of stale branches and dead assets.

Projects map tasks to specific project skills or commands via `producer.task_skills` in `devloops.config.yaml` (e.g. `producer.task_skills: {security: code-audit-expert}`). If unmapped, generic project-agnostic inspection applies.

## State

`<state_dir>/producer-loop/state.json` (default `.devloops/producer-loop/state.json`, git-ignored, resolved via `devloops.config.yaml` or `$LOOP_STATE_DIR`).
Lock file: `<state_dir>/producer-loop/tick.lock` (lock TTL: 30 minutes, 1800s).

```json
{
  "scheduler": { "kind": "process", "id": 1234 },
  "lastRun": {
    "security": 1730360000,
    "cleanup": 1729800000
  },
  "taskPeriods": {
    "cleanup": "7d"
  },
  "fingerprints": {
    "4a2b1c...": {
      "task": "security",
      "key": "cwe-798:src/auth.py:SECRET_KEY",
      "created_at": 1730360000
    }
  }
}
```

## Tick procedure

### 0. Gate Check & Lock
1. Run preflight gate:
   ```bash
   python3 "$SKILL_DIR/scripts/producer_gate.py" --next --lock
   ```
   - If output is `BUSY`: another tick is currently in progress. Stop immediately. Never unlock a lock this tick did not acquire.
   - If output is `NOOP`: all tasks are within their scheduled periods. Output `Idle: no tasks due.` and exit.
   - If a task name is returned (e.g. `security`): proceed with that task.
   - If a specific task was requested via CLI (`/producer-loop <task>`):
     ```bash
     python3 "$SKILL_DIR/scripts/producer_gate.py" --task <task> --lock
     ```

### 1. Execute Task Audit
1. Load task guidance from [references/tasks.md](references/tasks.md).
2. If mapped in `devloops.config.yaml` (`producer.task_skills.<task>`), invoke the mapped skill or command.
3. Inspect repository read-only. For `e2e`, run unsafe actions strictly in a temporary scratch directory.
4. Prepare candidate findings following [references/issue-template.md](references/issue-template.md):
   - English, ASD-STE100 style.
   - High-level requirements plus evidence.
   - **Never disclose secrets, tokens, or private paths**.
   - Stable finding key: unique string per distinct defect.

### 2. Deduplicate & File Issues
1. For each finding, compute fingerprint = `sha256(task + stable finding key)`.
2. File issues using `scripts/file_issue.py`:
   ```bash
   python3 "$SKILL_DIR/scripts/file_issue.py" \
     --task <task> \
     --key "<stable-finding-key>" \
     --title "<ASD-STE100 title>" \
     --body "<body formatted from template>" \
     --create
   ```
   Or in batch:
   ```bash
   python3 "$SKILL_DIR/scripts/file_issue.py" --task <task> --findings findings.json --create
   ```
   - The script skips if the fingerprint is already in `state.json`.
   - The script skips if any open or closed issue contains `<!-- producer:fp=<sha> -->`.
   - The script enforces `max_new_issues_per_run` (default 3, overridable per task in config).
   - The script attaches labels `labels.producer` (default `auto:producer`) and `auto:<task>`.

### 3. Complete Task & Release Lock
1. Mark task completed and release lock:
   ```bash
   python3 "$SKILL_DIR/scripts/producer_gate.py" --done <task> --unlock
   ```
2. Report summary: 1–2 sentences stating task name, number of candidate issues created, and number skipped.

## Hard safety rules

- **Issues only**: Producers only create GitHub issues. Never edit code, push commits, create pull requests, merge, close, delete, comment, or modify labels on existing items.
- **Protected paths**: For paths configured in `protected_paths`, report findings only.
- **Cleanup restrictions**:
  - `cleanup` only proposes; it never deletes branches or closes issues.
  - Generates at most **one rolling issue per week**.
  - Never proposes deleting the default or base branches (`project.default_branch`, `project.base_branch`).
  - Never proposes deleting long-lived branches matching `cleanup.protected_branches` from config.
- **Scratch sandbox**: `e2e` and test execution must run unsafe actions only in a dedicated scratch directory (`.devloops/scratch/` or OS temp).
- **No secrets in issues**: Findings must omit credentials, access tokens, auth headers, and private network addresses.
