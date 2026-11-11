# Comment Protocol

Worker and gatekeeper share one GitHub account, and GitHub rejects Approve / Request changes on your own PR. The protocol therefore uses plain PR comments with a hidden marker on the first line. `review_queue.py` (gatekeeper) and `pr_status.py` (worker) only trust markers on comments authored by the authenticated user.

## Gatekeeper verdict

`head` = full SHA of the PR head that was reviewed. `base` = the PR's base branch at review time (a later change forces re-review). `round` starts at 1 and increments per verdict (`clean` markers do not count).

```markdown
<!-- gatekeeper-loop: round=2 verdict=changes-requested head=<sha> base=<branch> -->

## Gatekeeper · round 2 · Changes requested

Scope: <one line — what the PR claims and whether the diff matches it>

### Required

- **G2.1** `src/auth/service.py:212` — <problem>. Expected: <concrete fix or acceptance check>.
- **G2.2** <missing test> — Expected: a unit test covering <case>.
- **G2.3** `CI failure (<job-name>)` — <diagnosed root cause from CI logs>. Expected: <concrete fix>.

### Suggestions (non-blocking)

- **S2.1** <optional improvement>

### Previous findings

- G1.1 ✅ fixed in <short-sha> · G1.2 ✅ decline accepted (<reason>)
```

Approve:

```markdown
<!-- gatekeeper-loop: round=3 verdict=approve head=<sha> base=<branch> -->

## Gatekeeper · round 3 · ✅ Approve

<2–3 lines: what was verified (diff, tests, CI green at <short-sha>), residual risks, suggestions left open>

### Criteria

| Criterion (from rubric / issue) | Result | Evidence |
| --- | --- | --- |
| … | pass / fail | `path:line` — brief note |

### Checked

- `<acceptance criterion 1 from linked issue>` — verified in `<file>:<line>` (<brief explanation>).
- `<acceptance criterion 2 from linked issue>` — verified in `<file>:<line>` (<brief explanation>).
```

> [!IMPORTANT]
> The `### Checked` section is **mandatory**. Every acceptance criterion from the linked issue, every finding from earlier rounds and every owner concern must be listed with concrete verification evidence (`file:line`). For security-sensitive PRs (shell policy, tool approval, permissions, secrets) also list the bypass scenarios checked. For OS-sensitive changes also list the per-OS CI job results (ubuntu / windows / macos) for the reviewed head. If this section is missing or empty, an `approve` verdict cannot be issued.

Clean, waiting for CI (no label change, not a round; lets any tick or machine see the review is done):

```markdown
<!-- gatekeeper-loop: verdict=clean head=<sha> base=<branch> -->

## Gatekeeper · reviewed clean · waiting for CI

<1–2 lines: what was reviewed, zero open required findings>
```

Needs human (not a verdict, does not count as a round; post once per reason+head, never duplicate):

```markdown
<!-- gatekeeper-loop: kind=needs-human reason=ambiguous-review-criteria head=<sha> -->

## Gatekeeper · needs human · ambiguous review criteria

<Concrete draft rubric or checklist the owner must confirm or edit — not open-ended questions.>
```

Reasons: `ambiguous review criteria`, `changes loop skill files`, `security-sensitive paths`. Missing repo CI is **not** needs-human: the gatekeeper opens a bootstrap CI PR instead.

Escalate (round limit or second dispute on the same `G` item):

```markdown
<!-- gatekeeper-loop: round=5 verdict=escalate head=<sha> -->

## Gatekeeper · round 5 · Needs maintainer decision

<the repeated disagreement on the same finding, both positions, and the decision needed>
```

## Worker response

Posted after addressing a `changes-requested` verdict, in the same round number.

```markdown
<!-- worker-loop: response round=2 head=<new-sha> -->

## Worker response · round 2

- **G2.1** fixed in <short-sha> — <one line>
- **G2.2** declined — <reason grounded in code, issue scope, or project rules>
- **S2.1** deferred — <reason or follow-up issue link>
```

Every `G` item must be answered as **fixed** or **declined**. Suggestions may be skipped silently.

## Labels

| Label                          | Set by     | Meaning                                                                                  |
| ------------------------------ | ---------- | ---------------------------------------------------------------------------------------- |
| `gatekeeper:changes-requested` | gatekeeper | Latest verdict requests changes                                                          |
| `gatekeeper:approved`          | gatekeeper | Approved at the current head; ready for a human merge                                    |
| `gatekeeper:escalated`         | gatekeeper | Round limit or repeated dispute on the same finding; maintainer picks direction          |
| `gatekeeper:needs-human`       | gatekeeper | Extra scrutiny before approve: security-sensitive or `protected_paths`, loop-skill change, or clean head with no CI—not a worker stop. Kept alongside the verdict label; removed when its reason no longer applies |

Unmarked comments from the repo owner (or `@me`) posted after the last marker are read as required items and trigger a review.

Keep exactly one of the first three labels on a PR; swap them with each verdict. Create missing labels once with `gh label create <name> --color <hex> --description <text>`.
