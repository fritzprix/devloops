---
name: pm-loop
description: 'PM orchestrator for GitHub or Jira kanban projects. Owns the board schedule, cascades ticks to worker-loop, gatekeeper-loop, and producer-loop, and stops at merge-ready. Use when the user types "/pm-loop", "/pm-loop 10m", "/pm-loop 10m slots: 3 delegation: claude", "pm loop", or asks to orchestrate devloops loops on a project.'
---

# PM Loop

Generic **project manager** loop for software, research, writing, or mixed repositories.
The PM **does not** implement code, review PRs, or file producer issues itself: it references the existing role skills and decides **when** to start them inside the user's delegate budget.

**Hard limits (never violate)**

- Only **pm-loop** arms a cron / shell scheduler. Peer loops tick when the PM cascades; their own schedulers must stay disarmed while the PM is armed.
- Never add `approved`, never merge, never enable auto-merge.
- Start **producer-loop** only after a duplicate search, only with `labels.producer` (default `auto:producer`). Do not file issues every tick; never add `approved` or `validated` on create.
- Auto-handle `needsHuman` that does not need a person (out-of-diff base CI follow-up, PR to fix the base, rebase on conflict). Ask a person only for approval, merge, and genuine requirement judgment. Batch questions; do not re-ask while waiting. Draft outgoing contact; do not send unasked.
- Redistribute capacity inside the **user-set slot budget** only. Do not stack two workers on the same PR; do not spawn producers when the board is full.

**Path convention**: `$SKILL_DIR` is this skill's directory. Scripts live under `"$SKILL_DIR/scripts/..."`.

## Commands

| Command | Behavior |
| --- | --- |
| `/pm-loop` | Run one orchestrator tick now. |
| `/pm-loop 10m` | Arm the **PM-only** recurring schedule (`10m` is the PM interval), then run one tick. Disarm peer-loop crons. |
| `/pm-loop 10m slots: 3 delegation: claude` | Arm schedule and persist `slots` (total concurrent delegate cap) and `delegation` (`cursor`, `claude`, or `harness`). |
| `/pm-loop status` | Print budget, scheduler, exclusivity, and profile summary; do no work. |
| `/pm-loop stop` | Disarm the PM scheduler. Peer loops may use their own schedules again. |

`slots` is the **total** concurrent delegate cap (worker + gatekeeper + producer). `delegation` is the default backend; if it is unavailable, ask again — do not silently switch.

If `slots` or `delegation` are omitted, ask **once**, store the answer in state, and ask again only when the budget is exhausted or the method should change.

## State

`<state_dir>/pm-loop/state.json` (default `.devloops/pm-loop/state.json`):

```json
{
  "scheduler": { "kind": "shell", "intervalSeconds": 600 },
  "budget": { "intervalSeconds": 600, "slots": 3, "delegation": "claude" },
  "projectProfile": { "projectType": "software", "policyOrder": ["skill", "config", "profile", "folklore"] },
  "needsHuman": [],
  "history": []
}
```

Lock file: `<state_dir>/pm-loop/tick.lock` (same TTL conventions as other loops).

## Meta-guide (project induction)

Do **not** hardcode project-specific rules in this skill (branch templates, CI commands, score thresholds).

Policy source, in order:

1. **Role skills** — `worker-loop`, `gatekeeper-loop`, `producer-loop` (changes belong there, not here).
2. **`devloops.config.yaml`**
3. **Durable `projectProfile`** inferred once from git, CI, branches, labels, the board, and how issues/PRs are used.
4. Comment folklore is **not** policy; if it conflicts with a skill, follow the skill.

On the **first tick**, write `projectProfile` and reuse it. Re-confirm only when ambiguity would change behavior. Never re-infer every tick.

Never infer from the repo: slot budget, delegation backend, or issue approval / merge authority.

See [references/induction.md](references/induction.md) and [references/delegation.md](references/delegation.md).

## Schedule exclusivity

While the PM scheduler is armed, **worker-loop**, **gatekeeper-loop**, and **producer-loop** must not run their own cron or shell loops. The PM cascades ticks through each skill's `scripts/tick.py` (or `devloops tick <loop>`).

```bash
python3 "$SKILL_DIR/scripts/plan_tick.py"
```

The JSON plan includes `exclusivity.peerConflicts` when a peer still has an armed scheduler. Disarm peer schedulers before arming the PM.

See [references/scheduling-exclusivity.md](references/scheduling-exclusivity.md).

## Tick procedure

### 0. Parse invocation and budget

```bash
python3 "$SKILL_DIR/scripts/pm_invoke.py" "/pm-loop …"
```

Persist interval, slots, and delegation. If `promptsNeeded` is non-empty, ask the user once, then:

```bash
python3 "$SKILL_DIR/scripts/pm_budget_answer.py" --field slots --value 3
python3 "$SKILL_DIR/scripts/pm_budget_answer.py" --field delegation --value cursor
```

### 1. Lock

```bash
python3 "$SKILL_DIR/scripts/tick_lock.py" --lock
```

Under `scripts/tick.py` / `devloops tick pm`, the wrapper owns the lock (same pattern as gatekeeper-loop).

### 2. Induction (first tick or ambiguous change)

```bash
python3 "$SKILL_DIR/scripts/pm_profile.py" --write [--signals-json '{...}'] [--confirm]
```

### 3. Plan board and cascade

```bash
python3 "$SKILL_DIR/scripts/plan_tick.py" [--pipeline-json '{...}']
```

Read the plan:

- `cascade`: ordered delegate runs inside the slot cap (bottleneck-aware).
- `constraints`: PM must not approve, merge, or over-fill the board.
- `roleSkills`: open those SKILL.md files for implementation/review/producer rules.

For each cascade item, start the role loop in a **separate** agent session when possible (especially gatekeeper). Use the configured `delegation` backend; if it fails to start, stop and ask — do not switch silently.

Example cascade (paths depend on install layout):

```bash
python3 "$SKILL_DIR/../worker-loop/scripts/tick.py" --agent-cmd 'agent "/worker-loop"'
python3 "$SKILL_DIR/../gatekeeper-loop/scripts/tick.py" --agent-cmd 'agent "/gatekeeper-loop"'
```

Run **producer-loop** only when the plan includes it and duplicate search is clean.

### 4. needsHuman and customer contact

Update `needsHuman` in state for true human gates (approval, merge, divergent requirements). Batch one message per blocking set. Draft customer or stakeholder email/chat; never send without explicit approval.

### 5. Unlock

```bash
python3 "$SKILL_DIR/scripts/tick_lock.py" --unlock --token "$TOKEN"
```

After productive work on an idle board, use `tick_lock.py --update` only when a future NOOP gate is desired (same semantics as worker fingerprint updates are **not** used here; the PM always plans from the board).

## Jira and mixed projects

`pm.board_provider` in config may be `github` or `jira`. Board column mapping and credentials belong in **project config**, not in this skill. Classification (`software` / `research` / `writing` / `mixed`) is a default for practices, not an identity.
