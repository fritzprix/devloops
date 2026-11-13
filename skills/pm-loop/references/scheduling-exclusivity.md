# PM schedule exclusivity

Only **pm-loop** may arm a recurring schedule (shell loop, cron, or harness callback).

When `pm-loop` state has an active `scheduler`:

- Do **not** arm `worker-loop`, `gatekeeper-loop`, or `producer-loop` on their own timers.
- The PM tick calls each peer's `scripts/tick.py` (or `devloops tick <loop>`) when the board needs that role.
- Peer state may record `schedule.pm.pmCascadeActive: true` so operators know independent crons are disabled.

When `/pm-loop stop` clears the PM scheduler, peer loops may use the adapters documented in [worker-loop scheduling](../../worker-loop/references/scheduling.md) again.

Detect conflicts:

```bash
python3 "$SKILL_DIR/scripts/pm_invoke.py" "/pm-loop status"
```

`exclusivity.peerConflicts` lists peer loops that still have an armed scheduler.
