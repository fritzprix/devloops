# Scheduling & Invocation

## Commands

| Command | Behavior |
| --- | --- |
| `/worker-loop` | Run one tick now. |
| `/worker-loop 10m` | Arm recurring loop (10m interval), then run one tick now. |
| `/worker-loop status` | Print state file summary (`current`, `needsUser`, `skipped`); do no work. |
| `/worker-loop stop` | Disarm the recurring loop. |

## Schedulers

### 1. Shell Loop Adapter (Default)
Run a background shell process that periodically triggers the tick via the skill's tick wrapper, `scripts/tick.py` (the same as `devloops tick worker` when the `devloops` command is installed):

- **Bash (Linux/macOS)**:
  ```bash
  while true; do python3 "$SKILL_DIR/scripts/tick.py" --agent-cmd 'agent "/worker-loop"'; sleep 600; done
  ```
- **PowerShell (Windows)**:
  ```powershell
  while ($true) { python3 "$SKILL_DIR/scripts/tick.py" --agent-cmd 'agent "/worker-loop"'; Start-Sleep -Seconds 600 }
  ```

The wrapper owns the lock for the whole agent run and releases it, with the tick's PR claims, on every exit path, including an agent error exit. It renews the lock in the background (every third of the TTL), so a tick longer than the TTL keeps it. It exports the lock token as `DEVLOOPS_TICK_TOKEN`: the agent's `--lock` recognizes the wrapper's own lock by that token (a normal call inside the wrapper, not a duplicate tick), prints it, and `--unlock` leaves the lock to the wrapper. If the wrapper is killed, its lock goes stale at once (its pid is dead), and an orphaned agent's `guard.py` and `claim.py` calls answer `LOST`, so it cannot push after the next tick starts.

The gatekeeper and producer use the same form with their own skill's wrapper: `gatekeeper-loop/scripts/tick.py --agent-cmd 'agent "/gatekeeper-loop"'` and `producer-loop/scripts/tick.py --agent-cmd 'agent "/producer-loop"'`. Only the worker gets the wrapper's fingerprint check. The producer selects its task inside the agent, with `producer_gate.py`.

### 2. Cron Schedule
Schedule via cron using the tick wrapper (which performs the gate check, holds the lock, runs the command, and unlocks on exit). Use the path where the skill is installed, for example `.agents/skills/`:
```cron
# Trigger worker-loop every 10 minutes
*/10 * * * * cd /path/to/project && python3 .agents/skills/worker-loop/scripts/tick.py --agent-cmd '/path/to/agent-runner "/worker-loop"'
```

### Agent IDs
`--agent-id <id>` on `tick.py` (or `devloops tick`) or on a loop script gives one agent its own state, lock, and worktree:

| Path | Default | With `--agent-id a` |
| --- | --- | --- |
| State and lock | `<state_dir>/worker-loop/` | `<state_dir>/worker-loop/agents/a/` |
| Worktree | `<project>.worktrees/worker` | `<project>.worktrees/worker-a` |

The flag sets `DEVLOOPS_AGENT_ID`, which the agent started by the tick wrapper inherits. All agents keep the same GitHub account. An id can contain letters, digits, `.`, `_`, and `-`, and it must start and end with a letter or digit.

### Slots, tick locks, and claims

| Concept | What it is | Held by | Answers |
| --- | --- | --- | --- |
| Slot | One worker, named by `--agent-id` (no id = the default slot) | You, when you start a worker | Whose state, lock, and worktree |
| Tick lock | `<slot dir>/tick.lock`, one tick per slot | The running tick, proven by its token | Is a tick of this slot running? |
| Claim | One slot per PR or issue | The tick (PR) or slot (issue) working on it | Which slot works on this item? |

**`BUSY` from the gate is about the tick lock of the same slot only.** It means a duplicate start, or this slot's own earlier tick that is still running or crashed recently. It never means that another worker exists: other slots have their own locks and never make this gate `BUSY`. The message describes the holder: pid (alive or dead), start time, last renewal.

**Lock ownership.** `--lock` writes a random token into the lock and prints `TOKEN <token>`. Renewal (`--renew`, the wrapper's heartbeat, and every `claim.py` and `guard.py` call), unlock, claims, and guarded commands all require that token:

- A lock is stale when its owner's pid is dead (same host), or when it was not renewed within the TTL (`DEVLOOPS_LOCK_TTL_SECONDS`, default 1800). Only a stale lock is reclaimed.
- A late `--unlock` from a tick that lost its lock prints `LOST` (exit 4) and touches neither the new owner's lock nor the state.
- `--unlock` without a token is an error; `--force-unlock` is for humans only.
- A tick that lost ownership cannot push or run destructive git commands: `guard.py` answers `LOST` without running them.

### Per-item claims
Workers on one repo use claims so that they do not take the same item:

| Item | Claim | Scope | Taken over when | Ends |
| --- | --- | --- | --- | --- |
| PR | `<state_dir>/claims/pr-N`, linked to the owner tick's lock and token | One machine, because the state directory is local | The owner tick's lock is stale or released | When the tick unlocks (also on error or crash), or `claim.py release-pr N` |
| Issue | Remote branch `wip/issue-N`: claim commits pushed as fast-forwards only | Any machine | The claim was not refreshed for `worker.issue_claim_ttl_minutes` (default 120) | `claim.py release-issue N` once the PR is open or the issue is abandoned |

- A live tick never loses its PR claim, however long it runs, and a crashed tick's claims free as soon as its lock is stale; there is no separate claim timeout.
- Each issue claim change (claim, refresh, release, takeover) pushes one small commit whose trailers record agent, host, state, time, and work branch. The push is a fast-forward, so it succeeds only if nobody changed the claim meanwhile: two slots never both win. No force push or branch deletion is involved.
- The owner refreshes the issue claim through `guard.py run --issue N` and `claim.py issue N`, so an active worker keeps it. A slot that takes over a stale claim gets the previous work branch and continues from it.
- `OVER LIMIT` releases the new issue claim at once, so it does not block other slots.
- A worker running alone is never blocked forever by earlier work:
  - issue claims of its own slot id on the same host are its own, and `claim.py issue` refreshes them at once;
  - issue claims of another slot id that no longer runs become available after the TTL, and stop counting toward `open_pr_limit` at the same time;
  - PR claims of an earlier tick free as soon as that tick's lock is stale.
- Limits come from `devloops.config.yaml`: `worker.open_pr_limit` (default 5) and `worker.issue_claim_ttl_minutes` (default 120).

**Two workers on different machines can still take the same PR**, because PR claims are local. Run multiple workers on one machine, or share the state directory.

Scope limit: claims cover the worker only. The gatekeeper and producer have no claims, so run one of each.

### 3. Harness-Specific Adapter
A harness-specific scheduler adapter (such as an IDE agent callback, persistent worker daemon, or webhook runner) can be integrated by setting `scheduler` in `devloops.config.yaml` and implementing the corresponding callback hook.
