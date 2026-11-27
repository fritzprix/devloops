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
Update 2026-11-27: the owner replaced D-015 with D-018 and D-017 with D-019. See the session "Session 2026-11-27 (state-model review)".

## Session 2026-11-27 (state-model review)

- Added: D-018, D-019, D-020, D-021, D-022, D-023, D-024, D-025, D-026, D-027, D-028
- Changed: D-015 -> D-018, D-017 -> D-019
- Superseded: D-015, D-017
- Reviewed and Kept: D-016 (confirmed by the owner)

The owner made these decisions after the review of the state model. The state model and its TLA+ check are in [docs/state-model.md at commit 36d3e8a](https://github.com/fritzprix/devloops/blob/36d3e8a786e2cb0e385036bb75465ba3d37280f6/docs/state-model.md) (branch `docs/state-model`).
The finding numbers (F1 to F15) and the property names in the `Source` lines are those of that document.

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
- Status: Superseded by D-018
- Date: 2026-11-27
- Decision: pm-loop owns release decisions and merges. It merges a pull request when gatekeeper and qa both pass at the same head. A release pull request and a merge-up are issues. They go through worker, gatekeeper, and qa. pm-loop does not bypass review.
- Reason: pm-loop already owns order and timing. It must not bypass review.
- Source: no written source on the tracker; recorded here for the first time (owner design discussion, 2026-11-27).
- Conflict: #102 R3 requires that the scaffolding interview pull request is merged by a human and that no loop merges it. Whether D-015 replaces that requirement or keeps an exception for the scaffolding pull request is not recorded as an owner decision. Asked the owner in PR #106 (2026-11-27). Until the owner answers, this decision is pending owner confirmation.
- Update 2026-11-27: The owner decided that the gatekeeper merges, not pm-loop. See D-018. D-018 replaces this decision.

### D-016

- Area: Owner
- Status: Added
- Date: 2026-11-27
- Decision: There is exactly one human owner. The design goal is that the owner is never a bottleneck. The loops do routine merges, releases, and merge-ups. The owner receives only blocked items and decision items.
- Reason: Routine work must not wait on the owner.
- Source: no written source on the tracker; recorded here for the first time (owner design discussion, 2026-11-27).
- Update 2026-11-27: The owner confirmed this decision. D-018 names the loop that merges.

### D-017

- Area: Owner
- Status: Superseded by D-019
- Date: 2026-11-27
- Decision: The label `needsUser` is renamed to `blocked:owner`. Work with this label stops until the owner acts. The label `needs-owner` is renamed to `decision:owner`. This is a request for a decision, and it includes a concrete draft. The loops continue on the draft. The count of `blocked:owner` items shows how much the owner is a bottleneck.
- Reason: The names must show the difference. One label stops work. The other label asks for a decision and does not stop the loops.
- Source: no written source on the tracker; recorded here for the first time (owner design discussion, 2026-11-27).
- Conflict: #102 R4 names `needs-owner` as a contract label that a project cannot edit, and #102 R3 makes items that depend on an open `needs-owner` question wait, while D-017 renames `needs-owner` to `decision:owner` and lets loops continue on the draft. Whether D-017 replaces those requirements is not recorded as an owner decision. Asked the owner in PR #106 (2026-11-27). Until the owner answers, this decision is pending owner confirmation.
- Update 2026-11-27: The owner decided the label names again. See D-019. D-019 replaces this decision.

### D-018

- Area: Loop roles
- Status: Changed (replaces D-015)
- Date: 2026-11-27
- Decision: The gatekeeper merges pull requests. pm-loop does not merge. A human does not merge routine pull requests. The gatekeeper merges a pull request only when all of these are true at the head commit X:
  - The gatekeeper approved X.
  - qa passed X.
  - All required checks completed green at X. This includes a check that starts only after a label is set.

  The merge is bound to X. If the head moved, the merge must stop. pm-loop still owns the order and timing of work. A release pull request and a merge-up are issues. They go through worker, gatekeeper, and qa. No loop bypasses review.
- Reason: One loop must own the merge. The merge must land only the code that was reviewed and checked. Today the merge gate does not name the head, and approve can come before a label-gated check ran.
- Source: owner decision, 2026-11-27. [State model](https://github.com/fritzprix/devloops/blob/36d3e8a786e2cb0e385036bb75465ba3d37280f6/docs/state-model.md) findings F5, F6, and F14. TLC properties `MergedOnlyWithSignoffs`, `MergedOnlyGreen`, and `ApproveOnlyWithRequiredChecks` fail on the current rules and pass when the merge is bound to the head.
- Conflict: This decision replaces every statement that a human merges. The skills, closed issues #62 and #81, and `docs/decisions.md` D-015 contain such statements. #102 R3 requires that a human merges the scaffolding pull request and that no loop merges it. #102 R1 names "paths where a human does the merge" as a policy setting. The owner did not record an exception for the scaffolding pull request or for any path. #102 R1 and R3 need an amendment. The forge adapter (#43, #46) needs a merge operation that takes the expected head SHA.

### D-019

- Area: Owner
- Status: Changed (replaces D-017)
- Date: 2026-11-27
- Decision: Two labels face the owner.
  - `blocked:owner`: work stopped until the owner acts. Loops do not work on an item with this label. The worker candidate list excludes an issue with this label. When the owner removes the label, the item returns to the queue.
  - `decision:owner`: a loop asked the owner to set a criterion. The request includes a concrete draft. The loops continue on the draft.

  `blocked:owner` replaces `needsUser`, `gatekeeper:escalated`, `gatekeeper:needs-human`, and `needs-owner` where it stops work. `decision:owner` replaces `needs-owner` where it asks for a criterion. The count of `blocked:owner` items shows how much the owner is a bottleneck.
- Reason: The names must show the difference. One label stops work. The other label asks for a decision and does not stop the loops. One stop label replaces four labels that mean "wait for the owner".
- Source: owner decision, 2026-11-27. [State model](https://github.com/fritzprix/devloops/blob/36d3e8a786e2cb0e385036bb75465ba3d37280f6/docs/state-model.md) findings F9, F10, F11, and F14.
- Conflict: #67, #64, #68, #71, and #102 R3 and R4 use `needs-owner` and say that loops wait for the answer. PR #96 implements `needs-owner` as a stopping label. Closed #70 and PRs #92 and #100 implement `gatekeeper:escalated`. D-028 sets the order: PR #96 finishes first. The rename follows in a separate issue.

### D-020

- Area: Loop state
- Status: Added
- Date: 2026-11-27
- Decision: A parked state lives in a label on the issue or on the pull request. It does not live in a state file of one worker slot. Every slot, machine, and loop reads the same state.
- Reason: Today `needsUser` and `skipped` are in the `state.json` file of one slot. Another slot cannot see them and can work on the parked item.
- Source: owner decision, 2026-11-27. [State model](https://github.com/fritzprix/devloops/blob/36d3e8a786e2cb0e385036bb75465ba3d37280f6/docs/state-model.md) finding F9. TLC property `ParkedStaysParked` fails when the state is per slot and passes when it is shared.

### D-021

- Area: Gatekeeper
- Status: Added
- Date: 2026-11-27
- Decision: The gatekeeper has no review round limit. The setting `gatekeeper.max_rounds` and the round-limit escalation are removed. When the gatekeeper raises again a finding that it raised in an earlier round, and the finding is still unresolved, the gatekeeper sets `blocked:owner` on the pull request.
- Reason: The round limit adds complexity that the project does not need. A repeated, unresolved finding is the real sign that the owner must decide.
- Source: owner decision, 2026-11-27. [State model](https://github.com/fritzprix/devloops/blob/36d3e8a786e2cb0e385036bb75465ba3d37280f6/docs/state-model.md) findings F10 and F11. TLC properties `RoundLimit` and `EscalationHappens` fail with the limit. Both findings disappear when the limit is removed.
- Conflict: This decision reverses closed #70 and PRs #92 and #100. It amends #64 ("Decisions already made", item 2, and the child table) and #102 R1 (policy item "The review round limit").

### D-022

- Area: Worker
- Status: Added
- Date: 2026-11-27
- Decision: The worker has no `skipped` state. When the worker cannot finish an issue, it posts one comment that gives the reason and sets `blocked:owner` on the issue. This applies when the implementation fails twice and when the scope is too large. The worker candidate list excludes the issue. When the owner removes the label, the issue is queued again.
- Reason: Today a skipped issue keeps the `approved` label and looks queued. No loop asks the owner.
- Source: owner decision, 2026-11-27. [State model](https://github.com/fritzprix/devloops/blob/36d3e8a786e2cb0e385036bb75465ba3d37280f6/docs/state-model.md) finding F1. TLC properties `NoSilentStall` and `IssueProgress` fail after a skip.

### D-023

- Area: Pull request comments
- Status: Added
- Date: 2026-11-27
- Decision: A pull request comment that needs no commit is resolved when the worker posts a response marker for it. A comment that has a response marker is no longer new. Every comment of the gatekeeper carries a loop marker. All loops use one account, so the marker is the only way to tell a loop comment from an owner comment.
- Reason: Today one comment that needs no commit stays new until the next commit. It blocks other work. An unmarked comment of the gatekeeper counts as an owner comment.
- Source: owner decision, 2026-11-27. [State model](https://github.com/fritzprix/devloops/blob/36d3e8a786e2cb0e385036bb75465ba3d37280f6/docs/state-model.md) findings F2 and F3. TLC property `PRProgress` and `IssueProgress` fail on these paths.

### D-024

- Area: Labels
- Status: Added
- Date: 2026-11-27
- Decision: A short time with two verdict labels on one pull request is acceptable. A loop that needs one verdict reads the latest verdict.
- Reason: A label change is two writes. The cost to make it one write is higher than the risk.
- Source: owner decision, 2026-11-27. [State model](https://github.com/fritzprix/devloops/blob/36d3e8a786e2cb0e385036bb75465ba3d37280f6/docs/state-model.md) finding F7. The TLC property `OneVerdictLabelSettled` holds.

### D-025

- Area: Worker
- Status: Added
- Date: 2026-11-27
- Decision: Before the worker creates a pull request, it checks for an open pull request that closes the issue. If one exists, the worker stops. A small window remains when two workers check at the same moment.
- Reason: Today two workers can open two pull requests for one issue.
- Source: owner decision, 2026-11-27. [State model](https://github.com/fritzprix/devloops/blob/36d3e8a786e2cb0e385036bb75465ba3d37280f6/docs/state-model.md) finding F8. TLC property `NoDuplicatePR` fails today.

### D-026

- Area: pm-loop
- Status: Added
- Date: 2026-11-27
- Decision: When pm-loop has one slot, it starts worker-loop, gatekeeper-loop, and qa-loop in round-robin order. A role with work gets its turn.
- Reason: Today the order is fixed. A role that always has work can keep the other roles from starting.
- Source: owner decision, 2026-11-27. [State model](https://github.com/fritzprix/devloops/blob/36d3e8a786e2cb0e385036bb75465ba3d37280f6/docs/state-model.md) finding F12. TLC property `QaProgress` fails with one slot.

### D-027

- Area: CI
- Status: Added
- Date: 2026-11-27
- Decision: Every loop that judges CI uses only the latest run of each workflow. It ignores cancelled runs and superseded runs. It ignores a run where all jobs were skipped.
- Reason: A new run cancels an older run. The older run must not count as a failure. The gatekeeper already does this for required checks. The worker does not.
- Source: owner decision, 2026-11-27. [State model](https://github.com/fritzprix/devloops/blob/36d3e8a786e2cb0e385036bb75465ba3d37280f6/docs/state-model.md) finding F4. TLC property `IssueProgress` fails when the worker counts a superseded run.

### D-028

- Area: Process
- Status: Added
- Date: 2026-11-27
- Decision: PR #96 finishes in its current scope. It keeps the label name `needs-owner`. The label rename (D-019) and the removal of the round limit (D-021) follow as separate issues.
- Reason: PR #96 is in review. A change of scope would restart the review.
- Source: owner decision, 2026-11-27.

## Implementation status

Checked on 2026-11-27 against `main` (`13d1ca9`). D-018 to D-028 were checked against `main` (`f959993`). Every decision whose text current behavior does not match is listed. The decisions not listed (D-002, D-014, D-024) match current behavior.

Does not match current behavior:

- D-001 to D-006 — there is no install agent or install flow on `main`. None of the interview, draft-approval, automatic forge-setup, or first-tick-check steps exist.
- D-007, D-009, D-010, D-011 — `main` has a single base branch (`main`). There is no work/staging/release layering, no scaffolding branch-policy fields, and no allowed-staging list.
- D-008 (partial) — branch naming, one branch and one pull request per issue, rebase-not-merge, and `--force-with-lease` hold on `main`. Two rules do not: (1) "Stacked pull requests are not used" — `skills/worker-loop/SKILL.md` (the `stack` dependency status) and `skills/worker-loop/references/stacked-prs.md` document and use stacking, and current pull requests stack (for example PR #99 was based on PR #96's branch); (2) "Merged branches are deleted" — the repository setting `delete_branch_on_merge` is `false`, head branches of merged pull requests remain on the remote (for example PR #100, #98, #88, #78), and `README.md` and `skills/worker-loop/SKILL.md` state that the worker does not delete remote branches.
- D-012, D-013 — no merge-up issue flow and no hotfix-via-staging path exist on `main`.
- D-015 (replaced by D-018) — no loop merges. The skill files on `main` still say that gatekeeper, qa, and pm never merge:
  - `skills/pm-loop/SKILL.md`: "Never add `approved`, never merge, never enable auto-merge."
  - `skills/gatekeeper-loop/SKILL.md`: The loop never merges. "Approve means 'ready for a human merge', not 'merged'."
  - `skills/qa-loop/SKILL.md`: "Never merge". A pull request is ready for a human merge only when gatekeeper and qa both sign off at the current head.
  - `skills/worker-loop/SKILL.md`: "Never merge or auto-merge". "Merging is always the human user's call."
- D-016 (partial) — there is exactly one human owner, but the loops do not do routine merges today: every merge is the owner's call, so the owner is a merge bottleneck today.
- D-017 (replaced by D-019) — the renames are not applied anywhere. The skills still use `needsUser` (worker state and escalation) and `needs-owner` (gatekeeper ambiguous-criteria questions and the parked-state contract).
- D-018 — no loop merges. `devloops_lib/forge/base.py` has no merge operation. `devloops_lib/qa.py:130-161` (`signoff`) does not name the head that is merged. It does not check CI.
- D-019 — the labels `blocked:owner` and `decision:owner` do not exist. `devloops_lib/forge/cli.py:117-121` sets and removes `gatekeeper:escalated`.
- D-020 — `devloops_lib/config.py:514-525` gives each worker slot its own `state.json`. `skills/worker-loop/SKILL.md:31` and `:78` keep `needsUser` there.
- D-021 — `devloops_lib/gatekeeper_queue.py:64-72` and `:398-400` set and use the round limit. `skills/gatekeeper-loop/SKILL.md:96` and `:146` describe the escalation.
- D-022 — `skills/worker-loop/SKILL.md:143` and `:160` define `skipped`. The worker candidate filter reads it (`devloops_lib/worker_candidates.py:124-135`).
- D-023 — `devloops_lib/pr_status.py:336-350` keeps a comment new until a later commit exists. `devloops_lib/gatekeeper_queue.py:166-178` counts every unmarked comment of the shared account as an owner comment. `skills/gatekeeper-loop/SKILL.md:93` tells the gatekeeper to post a plain comment.
- D-025 — `devloops_lib/claims.py:276-295` grants a free or released claim and does not look for an open pull request.
- D-026 — `devloops_lib/pm_schedule.py:143-156` uses a fixed order: gatekeeper, worker, qa.
- D-027 — `devloops_lib/pr_status.py:53` and `:293-294` count a `CANCELLED` run as a failure and do not ignore superseded runs. `devloops_lib/gatekeeper_queue.py` already ignores them for required checks.
- D-021 and D-028 — PR #96 still uses `needs-owner`, and its review is in round five.
