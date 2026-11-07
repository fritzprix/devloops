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
Run a background shell process that periodically triggers the tick via the `devloops tick` wrapper:

- **Bash (Linux/macOS)**:
  ```bash
  while true; do devloops tick worker --agent-cmd 'agent "/worker-loop"'; sleep 600; done
  ```
- **PowerShell (Windows)**:
  ```powershell
  while ($true) { devloops tick worker --agent-cmd 'agent "/worker-loop"'; Start-Sleep -Seconds 600 }
  ```

The wrapper owns the lock and releases it safely on every exit path. It exports `DEVLOOPS_TICK_WRAPPER_PID` to the agent, so the `--lock` and `--unlock` gate flags in a SKILL leave the wrapper's lock in place.

The gatekeeper and producer use the same form: `devloops tick gatekeeper --agent-cmd 'agent "/gatekeeper-loop"'` and `devloops tick producer --agent-cmd 'agent "/producer-loop"'`. Only the worker gets the wrapper's fingerprint check. The producer selects its task inside the agent, with `producer_gate.py`.

### 2. Cron Schedule
Schedule via cron using `devloops tick` (which performs the gate check, holds the lock, runs the command, and unlocks on exit):
```cron
# Trigger worker-loop every 10 minutes
*/10 * * * * cd /path/to/project && devloops tick worker --agent-cmd '/path/to/agent-runner "/worker-loop"'
```

### 3. Harness-Specific Adapter
A harness-specific scheduler adapter (such as an IDE agent callback, persistent worker daemon, or webhook runner) can be integrated by setting `scheduler` in `devloops.config.yaml` and implementing the corresponding callback hook.
