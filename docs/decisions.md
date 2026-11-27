# Design decision log

This file is the record of design decisions for devloops.
Use this file to see which decisions stay, which are new, and which changed.

## How to use this log

Each decision has:

- a stable ID (`D-001`, `D-002`, and so on)
- an area
- a status: `Kept`, `Added`, `Changed`, or `Superseded`
- a date
- the decision text
- a source, so a later reader can open what supports it
- a reason, when the reason is known

Do not delete old decision text.
When a decision changes, mark the old entry `Superseded` and link it to the new ID.
Put the new text under the new ID.

After each discussion session, add a section named `Session YYYY-MM-DD`.
In that section, list:

- IDs that were Added
- IDs that were Changed, in the form `old ID -> new ID`
- IDs that were Superseded
- IDs that were reviewed and Kept

The catalog below holds the text of every ID.

## Session 2026-11-27

- Added: D-001, D-002, D-003, D-004, D-005, D-006, D-007, D-008, D-009, D-010, D-011, D-012, D-013, D-014, D-015, D-016, D-017
- Changed: none
- Superseded: none
- Reviewed and Kept: none

Sources are per decision, in the `Source` line of each catalog entry.
D-001 to D-006 come from the install-scenario comment on issue #102.
D-007 to D-017 have no written source on the tracker; they are recorded here for the first time.
D-015 and D-017 conflict with issue #102 and are pending owner confirmation (see their `Conflict` lines).

## Decision catalog

### D-001

- Area: Install
- Status: Added
- Date: 2026-11-27
- Decision: The install agent shows the scaffold and SSOT draft. It commits the draft after the owner approves it.
- Reason: The owner must see and accept the scaffold before it is committed.
- Source: [#102 install scenario, item 5](https://github.com/fritzprix/devloops/issues/102#issuecomment-7207621942).

### D-002

- Area: Install
- Status: Added
- Date: 2026-11-27
- Decision: The inter-loop contract is fixed in core. The contract covers states, labels, markers, and the forge adapter. The contract is not part of the install interview.
- Reason: Every project must use the same inter-loop contract.
- Source: [#102 install scenario, item 2](https://github.com/fritzprix/devloops/issues/102#issuecomment-7207621942) and #102 R4.

### D-003

- Area: Install
- Status: Added
- Date: 2026-11-27
- Decision: In an existing workspace, the install agent reads the workspace and infers values first. It then asks only for confirmation. In an empty workspace, the agent proposes the template defaults.
- Reason: An existing workspace already has signals. An empty workspace does not.
- Source: [#102 install scenario, item 3](https://github.com/fritzprix/devloops/issues/102#issuecomment-7207621942).

### D-004

- Area: Install
- Status: Added
- Date: 2026-11-27
- Decision: The interview ends when the required fields are filled and have no contradictions. The required fields are the rubric, the verification commands, the target branch, and the per-stage triggers. Optional fields keep their defaults. The draft marks those defaults.
- Reason: The interview must stop when the required fields are complete and consistent.
- Source: [#102 install scenario, item 4](https://github.com/fritzprix/devloops/issues/102#issuecomment-7207621942).

### D-005

- Area: Install
- Status: Added
- Date: 2026-11-27
- Decision: After the owner approves the draft, forge setup runs automatically. It covers labels, a token permission check, and branch protection. These changes are shown in the draft before approval. If a permission is missing, the agent stops and reports it.
- Reason: Forge setup is part of install. A missing permission must not be ignored.
- Source: [#102 install scenario, item 6](https://github.com/fritzprix/devloops/issues/102#issuecomment-7207621942).

### D-006

- Area: Install
- Status: Added
- Date: 2026-11-27
- Decision: A subagent launch is only for checking the first tick. Ongoing runs use a scheduler.
- Reason: One check is enough to prove the first tick. Ongoing runs belong to a scheduler.
- Source: [#102 install scenario, item 7](https://github.com/fritzprix/devloops/issues/102#issuecomment-7207621942).

### D-007

- Area: Branch model
- Status: Added
- Date: 2026-11-27
- Decision: Core defines three layers: work, staging, and release. The scaffold maps the actual branch names to these layers.
- Reason: The layers are core concepts. Branch names are a project mapping.
- Source: no written source on the tracker; recorded here for the first time (owner design discussion, 2026-11-27).

### D-008

- Area: Branch model
- Status: Added
- Date: 2026-11-27
- Decision: Core also fixes these rules. Work branches use the name `<type>/<slug>-<issue#>`. Each issue has one branch and one pull request. The work branch is rebased onto its base. The base is not merged into the work branch. Updates of a work branch use `--force-with-lease`. Merged branches are deleted. Stacked pull requests are not used.
- Reason: These rules are the same in every project. One issue maps to one branch and one pull request.
- Source: no written source on the tracker; recorded here for the first time (owner design discussion, 2026-11-27).

### D-009

- Area: Branch model
- Status: Added
- Date: 2026-11-27
- Decision: The scaffold holds these fields: the merge method, signing, whether direct commits are allowed, and the release conditions.
- Reason: These choices vary by project.
- Source: no written source on the tracker; recorded here for the first time (owner design discussion, 2026-11-27).

### D-010

- Area: Branch model
- Status: Added
- Date: 2026-11-27
- Decision: The staging branch and the release branch are always different branches.
- Reason: A release must be an explicit step from staging.
- Source: no written source on the tracker; recorded here for the first time (owner design discussion, 2026-11-27).

### D-011

- Area: Branch model
- Status: Added
- Date: 2026-11-27
- Decision: A project may have more than one staging branch, for parallel release lines. The scaffold lists the allowed staging branches and one default staging branch. An issue states its base. An empty base means the default staging branch. A base that is not in the list is rejected. The gatekeeper checks that the pull request base matches the issue base.
- Reason: A project can keep more than one release line at the same time. The allowed list and the default keep the base value reliable.
- Source: no written source on the tracker; recorded here for the first time (owner design discussion, 2026-11-27).

### D-012

- Area: Branch model
- Status: Added
- Date: 2026-11-27
- Decision: Every change that is merged into a lower staging line must reach the higher staging lines. This happens once, when the lower line ships. At that time, pm-loop opens one merge-up issue from the lower staging branch to the higher staging branch. That issue goes through worker, gatekeeper, and qa like any other issue. The worker resolves conflicts.
- Reason: A fix in a lower line must not be missing in a higher line. One merge-up at ship time keeps the work in one issue and one pull request.
- Source: no written source on the tracker; recorded here for the first time (owner design discussion, 2026-11-27).

### D-013

- Area: Branch model
- Status: Added
- Date: 2026-11-27
- Decision: A hotfix goes to staging first and ships through a release pull request. There are no direct commits to release. An urgent fix ships as a patch release of the lower line. The normal merge-up then carries it to the higher lines.
- Reason: All changes follow the same staging and release path. A short patch release of the lower line avoids a special urgent path.
- Source: no written source on the tracker; recorded here for the first time (owner design discussion, 2026-11-27).

### D-014

- Area: Loop roles
- Status: Added
- Date: 2026-11-27
- Decision: The core loops are pm-loop, worker-loop, gatekeeper-loop, qa-loop, and producer-loop. There is no separate release loop.
- Reason: Release work is a schedule and merge decision. It belongs to pm-loop.
- Source: no written source on the tracker; recorded here for the first time (owner design discussion, 2026-11-27).

### D-015

- Area: Loop roles
- Status: Added
- Date: 2026-11-27
- Decision: pm-loop owns release decisions and merges. It merges a pull request when gatekeeper and qa both pass at the same head. A release pull request and a merge-up are issues. They go through worker, gatekeeper, and qa. pm-loop does not bypass review.
- Reason: pm-loop already owns order and timing. It must not bypass review.
- Source: no written source on the tracker; recorded here for the first time (owner design discussion, 2026-11-27).
- Conflict: #102 R3 requires that the scaffolding interview pull request is merged by a human and that no loop merges it. Whether D-015 replaces that requirement or keeps an exception for the scaffolding pull request is not recorded as an owner decision. Asked the owner in PR #106 (2026-11-27). Until the owner answers, this decision is pending owner confirmation.

### D-016

- Area: Owner
- Status: Added
- Date: 2026-11-27
- Decision: There is exactly one human owner. The design goal is that the owner is never a bottleneck. The loops do routine merges, releases, and merge-ups. The owner receives only blocked items and decision items.
- Reason: Routine work must not wait on the owner.
- Source: no written source on the tracker; recorded here for the first time (owner design discussion, 2026-11-27).

### D-017

- Area: Owner
- Status: Added
- Date: 2026-11-27
- Decision: The label `needsUser` is renamed to `blocked:owner`. Work with this label stops until the owner acts. The label `needs-owner` is renamed to `decision:owner`. This is a request for a decision, and it includes a concrete draft. The loops continue on the draft. The count of `blocked:owner` items shows how much the owner is a bottleneck.
- Reason: The names must show the difference. One label stops work. The other label asks for a decision and does not stop the loops.
- Source: no written source on the tracker; recorded here for the first time (owner design discussion, 2026-11-27).
- Conflict: #102 R4 names `needs-owner` as a contract label that a project cannot edit, and #102 R3 makes items that depend on an open `needs-owner` question wait, while D-017 renames `needs-owner` to `decision:owner` and lets loops continue on the draft. Whether D-017 replaces those requirements is not recorded as an owner decision. Asked the owner in PR #106 (2026-11-27). Until the owner answers, this decision is pending owner confirmation.

## Implementation status

Checked on 2026-11-27 against `main` (`13d1ca9`). Every decision whose text current behavior does not match is listed. The decisions not listed (D-002, D-014) match current behavior.

Does not match current behavior:

- D-001 to D-006 — there is no install agent or install flow on `main`. None of the interview, draft-approval, automatic forge-setup, or first-tick-check steps exist.
- D-007, D-009, D-010, D-011 — `main` has a single base branch (`main`). There is no work/staging/release layering, no scaffolding branch-policy fields, and no allowed-staging list.
- D-008 (partial) — branch naming, one branch and one pull request per issue, rebase-not-merge, `--force-with-lease`, and delete-on-merge hold on `main`. "Stacked pull requests are not used" does not: `skills/worker-loop/SKILL.md` (the `stack` dependency status) and `skills/worker-loop/references/stacked-prs.md` document and use stacking, and current pull requests stack (for example PR #99 was based on PR #96's branch).
- D-012, D-013 — no merge-up issue flow and no hotfix-via-staging path exist on `main`.
- D-015 — no loop merges. The skill files on `main` still say that gatekeeper, qa, and pm never merge:
  - `skills/pm-loop/SKILL.md`: "Never add `approved`, never merge, never enable auto-merge."
  - `skills/gatekeeper-loop/SKILL.md`: The loop never merges. "Approve means 'ready for a human merge', not 'merged'."
  - `skills/qa-loop/SKILL.md`: "Never merge". A pull request is ready for a human merge only when gatekeeper and qa both sign off at the current head.
  - `skills/worker-loop/SKILL.md`: "Never merge or auto-merge". "Merging is always the human user's call."
- D-016 (partial) — there is exactly one human owner, but the loops do not do routine merges today: every merge is the owner's call, so the owner is a merge bottleneck today.
- D-017 — the renames are not applied anywhere. The skills still use `needsUser` (worker state and escalation) and `needs-owner` (gatekeeper ambiguous-criteria questions and the parked-state contract).
