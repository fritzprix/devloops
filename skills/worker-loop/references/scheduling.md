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
Run a background shell process that periodically triggers the tick:

- **Bash (Linux/macOS)**:
  ```bash
  while true; do sleep 600; echo 'AGENT_LOOP_TICK_worker_loop {"prompt":"/worker-loop"}'; done
  ```
- **PowerShell (Windows)**:
  ```powershell
  while ($true) { Start-Sleep -Seconds 600; Write-Output 'AGENT_LOOP_TICK_worker_loop {"prompt":"/worker-loop"}' }
  ```

Record the process PID in state under `scheduler: { "kind": "process", "id": pid }`. Run the first tick immediately.
On notification, run one tick. If a tick is in progress (`python3 skills/worker-loop/scripts/tick_gate.py --lock` returns `BUSY`), ignore the notification.
To stop: terminate the recorded PID.

### 2. Cron Schedule
Alternatively, schedule via cron for headless environments:
```cron
# Trigger worker-loop every 10 minutes
*/10 * * * * cd /path/to/project && python3 skills/worker-loop/scripts/tick_gate.py --lock && /path/to/agent-runner "/worker-loop"
```

### 3. Harness-Specific Adapter
A harness-specific scheduler adapter (such as an IDE agent callback, persistent worker daemon, or webhook runner) can be integrated by setting `scheduler` in `devloops.config.yaml` and implementing the corresponding callback hook.
