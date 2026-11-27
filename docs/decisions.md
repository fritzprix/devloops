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

Source: issue #102.

## Decision catalog

### D-001

- Area: Install
- Status: Added
- Date: 2026-11-27
- Decision: The install agent shows the scaffold and SSOT draft. It commits the draft after the owner approves it.
- Reason: The owner must see and accept the scaffold before it is committed.

### D-002

- Area: Install
- Status: Added
- Date: 2026-11-27
- Decision: The inter-loop contract is fixed in core. The contract covers states, labels, markers, and the forge adapter. The contract is not part of the install interview.
- Reason: Every project must use the same inter-loop contract.

### D-003

- Area: Install
- Status: Added
- Date: 2026-11-27
- Decision: In an existing workspace, the install agent reads the workspace and infers values first. It then asks only for confirmation. In an empty workspace, the agent proposes the template defaults.
- Reason: An existing workspace already has signals. An empty workspace does not.

### D-004

- Area: Install
- Status: Added
- Date: 2026-11-27
- Decision: The interview ends when the required fields are filled and have no contradictions. The required fields are the rubric, the verification commands, the target branch, and the per-stage triggers. Optional fields keep their defaults. The draft marks those defaults.
- Reason: The interview must stop when the required fields are complete and consistent.

### D-005

- Area: Install
- Status: Added
- Date: 2026-11-27
- Decision: Forge setup runs automatically at install. It covers labels, a token permission check, and branch protection. These changes are included in the draft. If a permission is missing, the agent stops and reports it.
- Reason: Forge setup is part of install. A missing permission must not be ignored.

### D-006

- Area: Install
- Status: Added
- Date: 2026-11-27
- Decision: A subagent launch is only for checking the first tick. Ongoing runs use a scheduler.
- Reason: One check is enough to prove the first tick. Ongoing runs belong to a scheduler.

### D-007

- Area: Branch model
- Status: Added
- Date: 2026-11-27
- Decision: Core defines three layers: work, staging, and release. The scaffold maps the actual branch names to these layers.
- Reason: The layers are core concepts. Branch names are a project mapping.

### D-008

- Area: Branch model
- Status: Added
- Date: 2026-11-27
- Decision: Core also fixes these rules. Work branches use the name `<type>/<slug>-<issue#>`. Each issue has one branch and one pull request. The work branch is rebased onto its base. The base is not merged into the work branch. Updates of a work branch use `--force-with-lease`. Merged branches are deleted. Stacked pull requests are not used.
- Reason: These rules are the same in every project. One issue maps to one branch and one pull request.

### D-009

- Area: Branch model
- Status: Added
- Date: 2026-11-27
- Decision: The scaffold holds these fields: the merge method, signing, whether direct commits are allowed, and the release conditions.
- Reason: These choices vary by project.

### D-010

- Area: Branch model
- Status: Added
- Date: 2026-11-27
- Decision: The staging branch and the release branch are always different branches.
- Reason: A release must be an explicit step from staging.

### D-011

- Area: Branch model
- Status: Added
- Date: 2026-11-27
- Decision: A project may have more than one staging branch, for parallel release lines. The scaffold lists the allowed staging branches and one default staging branch. An issue states its base. An empty base means the default staging branch. A base that is not in the list is rejected. The gatekeeper checks that the pull request base matches the issue base.
- Reason: A project can keep more than one release line at the same time. The allowed list and the default keep the base value reliable.

### D-012

- Area: Branch model
- Status: Added
- Date: 2026-11-27
- Decision: Every change that is merged into a lower staging line must reach the higher staging lines. This happens once, when the lower line ships. At that time, pm-loop opens one merge-up issue from the lower staging branch to the higher staging branch. That issue goes through worker, gatekeeper, and qa like any other issue. The worker resolves conflicts.
- Reason: A fix in a lower line must not be missing in a higher line. One merge-up at ship time keeps the work in one issue and one pull request.

### D-013

- Area: Branch model
- Status: Added
- Date: 2026-11-27
- Decision: A hotfix goes to staging first and ships through a release pull request. There are no direct commits to release. An urgent fix ships as a patch release of the lower line. The normal merge-up then carries it to the higher lines.
- Reason: All changes follow the same staging and release path. A short patch release of the lower line avoids a special urgent path.

### D-014

- Area: Loop roles
- Status: Added
- Date: 2026-11-27
- Decision: The core loops are pm-loop, worker-loop, gatekeeper-loop, qa-loop, and producer-loop. There is no separate release loop.
- Reason: Release work is a schedule and merge decision. It belongs to pm-loop.

### D-015

- Area: Loop roles
- Status: Added
- Date: 2026-11-27
- Decision: pm-loop owns release decisions and merges. It merges a pull request when gatekeeper and qa both pass at the same head. A release pull request and a merge-up are issues. They go through worker, gatekeeper, and qa. pm-loop does not bypass review.
- Reason: pm-loop already owns order and timing. It must not bypass review.

### D-016

- Area: Owner
- Status: Added
- Date: 2026-11-27
- Decision: There is exactly one human owner. The design goal is that the owner is never a bottleneck. The loops do routine merges, releases, and merge-ups. The owner receives only blocked items and decision items.
- Reason: Routine work must not wait on the owner.

### D-017

- Area: Owner
- Status: Added
- Date: 2026-11-27
- Decision: The label `needsUser` is renamed to `blocked:owner`. Work with this label stops until the owner acts. The label `needs-owner` is renamed to `decision:owner`. This is a request for a decision, and it includes a concrete draft. The loops continue on the draft. The count of `blocked:owner` items shows how much the owner is a bottleneck.
- Reason: The names must show the difference. One label stops work. The other label asks for a decision and does not stop the loops.

## Implementation status

D-015 and D-017 are recorded decisions. They are not yet implemented in the skills.
The skill files on `main` still say that gatekeeper, qa, and pm never merge, and they still use `needsUser` and `needs-owner`.
This was checked on 2026-11-27 against these files:

- `skills/pm-loop/SKILL.md`: "Never add `approved`, never merge, never enable auto-merge."
- `skills/gatekeeper-loop/SKILL.md`: The loop never merges. "Approve means 'ready for a human merge', not 'merged'."
- `skills/qa-loop/SKILL.md`: "Never merge". A pull request is ready for a human merge only when gatekeeper and qa both sign off at the current head.
- `skills/worker-loop/SKILL.md`: "Never merge or auto-merge". "Merging is always the human user's call." Worker state and escalation use `needsUser`.
- `skills/gatekeeper-loop/SKILL.md`: Ambiguous judgment criteria are filed as an issue with a concrete draft and the label `needs-owner`. Review continues on the draft.
