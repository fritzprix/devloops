# Escalation policy

Loops are autonomous when work is **clear** and the **declared environment** can do it. Stop and record `needsUser` (worker) when the work is **ambiguous**, needs a **missing capability/secret/permission**, or is an **irreversible out-of-repo action**. Do not guess that something is impossible—check `environment.capabilities` in `devloops.config.yaml` (and any harness-provided capability list) explicitly.

## Environment capabilities

`devloops.config.yaml`:

```yaml
environment:
  capabilities:
    - browser
    - screen_capture
    - cli
    - network
    - public_web
    - diagrams    # Mermaid, PlantUML, ASCII architecture sketches
```

- **Capability check**: Before `needsUser` for “cannot do X”, verify X maps to a capability name and that name is **not** listed. In `needsUser.question`, cite the missing capability (e.g. `missing capability: screen_capture`).
- **Harness override**: If the runtime documents extra capabilities (agent tool list), treat the union of config + harness as available. Never assume a capability that is not declared.
- **Secrets/permissions**: Credentials are not capabilities. A listed `github_cli` capability does not imply push rights or org secrets—those are separate; record `needsUser` with the concrete secret or permission name.

## Do the work yourself (no `needsUser`)

When the issue/PR requirement is **unambiguous** and each needed capability is declared:

1. Use browser, screen capture, shell, `gh`/`git`, network, and public docs.
2. Reproduce locally; stub or mock only when the issue allows or when verifying the real path needs a missing capability.
3. Prefer Mermaid/PlantUML/text diagrams when that satisfies the requirement.

**Not** a reason to stall: the task merely touches an “external” product, needs a screenshot of a public UI, or uses a live API—if `browser` / `network` / `screen_capture` are declared, do it.

## Worker: `needsUser`

Record `state.json` `needsUser` when **any** of these apply:

| Trigger | What to ask |
| --- | --- |
| **(a) Ambiguity** | Unclear intent, competing valid product/behavior choices, or missing requirements that change the outcome (e.g. issue lists options with no `Decision`/recommendation and the choice alters implementation; rebase conflict on non-mechanical hunks; security/auth/privacy/billing/data/concurrency approach undefined in the issue) |
| **(b) Missing environment** | A required capability, secret, or permission is not in the declared environment (name it explicitly) |
| **(c) Irreversible external** | Action outside the repo the agent must not simulate (production data deletion, payments, sending to real users, registry publish without access, legal sign-off) |

**Not** `needsUser`:

- **Open PR limit** or **stack depth** → idle this tick only (worker SKILL); never throttle via `needsUser`.
- Clear issue with declared capabilities → execute; no “external services” catch-all.

When blocked on (a), quote the ambiguous lines in `question` and stop work that depends on the answer. When blocked on (b), state the missing capability/secret/permission by name.

## Sensitive paths and CI config

- **Clear scope** in the issue/PR: edit `protected_paths`, `.github/workflows`, or release config as required; expect stricter gatekeeper review (bypass scenarios in `### Checked`). Worker does not stall for sensitivity alone.
- **Ambiguous scope or approach** for those areas → **(a) Ambiguity** → `needsUser` before large edits.

## Gatekeeper: `escalate` and `gatekeeper:needs-human`

- **`gatekeeper:needs-human`**: Human attention for ambiguous review criteria or loop-skill edits. Never applied from a path-name match; real permission/secret/policy/sandbox changes get stricter gatekeeper review instead. Worker may still implement.
- **`verdict=escalate`**: Round limit (5); second dispute on the same `G` item; or an unresolved **product/security/data owner decision** the worker flagged and the thread does not settle—hand to maintainer with both positions.

## Producer

File issues with evidence and a **Recommendation** when one default is reasonable—but do not hide ambiguity. If multiple valid approaches exist, list options and ask for a `Decision` rather than implying the worker should guess. Do not ask owners for steps declared capabilities cover (screenshots, browser repro, local tests, diagrams).
