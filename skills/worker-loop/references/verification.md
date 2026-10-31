# Verification & Challenges

## Verification Rules

Run verification commands from the worktree (`$WT`).

Read test, lint, and build commands from `devloops.config.yaml`:
- **Test**: Run `commands.test` (e.g. `npm test`, `pytest`, `cargo test`, etc.). Run targeted test suites on changed files/modules whenever possible.
- **Lint & Format**: Run `commands.lint` on changed files.
- **Build**: Run `commands.build` when type definitions, schemas, or public APIs change.

If any command is empty (`""`), verify through standard language tools or inspect code changes carefully.

## Challenges to the Loop's Behavior

When the user says the loop did something wrong (e.g. "it is only waiting for PRs"):
1. Check `state.json` `history`, `skipped`, and `needsUser` before answering.
2. If the records contradict the claim, do not agree: explain what the loop actually did, citing the records.
3. Say "I will change X" only when behavior actually changes (a skill edit or a state change); never present existing behavior as a fix.
