# Verification & Challenges

## Verification Rules

Run verification commands from the worktree (`$WT`).

Read test, lint, and build commands from `devloops.config.yaml`. Before every push, run the full local verification for the change:
- **Test**: Run the full `commands.test` suite (e.g. `npm test`, `pytest`, `cargo test`). Targeted runs are for iterating; the full suite runs before the push.
- **Lint & Format**: Run `commands.lint`.
- **Build**: Run `commands.build`.

If any command is empty (`""`), verify through standard language tools or inspect code changes carefully.

## Failures that come and go

- A test that fails only sometimes (race, timing, test order, shared state) is a defect. Find the cause and fix it; do not rerun until it passes, locally or in CI.
- A green full suite is not evidence of concurrency safety. Concurrency claims need a test that runs the competing processes and checks the outcome.

## Challenges to the Loop's Behavior

When the user says the loop did something wrong (e.g. "it is only waiting for PRs"):
1. Check `state.json` `history`, `skipped`, and `needsUser` before answering. Open PR limit throttles without `needsUser` ([escalation-policy.md](escalation-policy.md)).
2. If the records contradict the claim, do not agree: explain what the loop actually did, citing the records.
3. Say "I will change X" only when behavior actually changes (a skill edit or a state change); never present existing behavior as a fix.
