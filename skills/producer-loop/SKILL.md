---
name: producer-loop
description: 'Autonomous producer loop that discovers improvements and files candidate issues. Invocation: /producer-loop, /producer-loop <interval>, /producer-loop status, /producer-loop stop.'
---

# Producer Loop

TODO: Planned design for the autonomous issue producer loop.

## Planned Design
- **Tasks**: Periodically scans the codebase across specialized audit areas: `security`, `e2e`, `uiux`, `regression`, `usage-logs`, `doc-drift`, and `cleanup`.
- **Issue Filing**: Files candidate issues carrying the producer label (`labels.producer`, default `auto:producer`).
- **Validation Workflow**:
  1. `producer-loop` creates candidate issues with label `auto:producer`.
  2. `gatekeeper-loop` validates the issues, adding `validated` or `rejected` labels.
  3. A human maintainer reviews validated issues and adds the `approved` label.
  4. `worker-loop` picks and implements only approved issues (`auto:producer` + `approved`).
- **Invocation**: `/producer-loop`, `/producer-loop <interval>`, `/producer-loop status`, `/producer-loop stop`.
