# Comment Protocol

Worker and gatekeeper share one GitHub account, and GitHub rejects Approve / Request changes on your own PR. The protocol therefore uses plain PR comments with a hidden marker on the first line. `review_queue.py` (gatekeeper) and `pr_status.py` (worker) only trust markers on comments authored by the authenticated user.

## Gatekeeper verdict

`head` = full SHA of the PR head that was reviewed. `base` = the PR's base branch at review time (a later change forces re-review). `round` starts at 1 and increments per verdict (`clean` markers do not count).

### Score (required on every verdict and every `clean` marker)

Every review posts a **Score** before Required/Suggestions/Checked. Scale is integer **1–5**:

| Score | Meaning |
| --- | --- |
| **5** | Ready to approve once CI (when it applies) is green; no open required findings. |
| **4** | Solid; only non-blocking suggestions remain. |
| **3** | Clear path forward; at least one required finding or missing proof. |
| **2** | Major gaps (wrong approach, missing tests, unsafe path) — not mergeable soon. |
| **1** | Out of scope, unsafe, or does not match the linked issue. |

One line of justification after the number. Score is judgment for humans and later rounds; it does not replace the verdict or labels.

```markdown
<!-- gatekeeper-loop: round=2 verdict=changes-requested head=<sha> base=<branch> -->

## Gatekeeper · round 2 · Changes requested

Scope: <one line — what the PR claims and whether the diff matches it>

### Score

**3/5** — <one line: why this score>

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

### Score

**5/5** — <one line: why this score>

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
> The `### Checked` section is **mandatory**. Every acceptance criterion from the linked issue, every finding from earlier rounds and every owner concern must be listed with concrete verification evidence (`file:line`). For PRs that, after reading the files, actually change shell policy, tool approval, permission, secret, or sandbox code or config, also list the bypass scenarios checked. For OS-sensitive changes also list the per-OS CI job results (ubuntu / windows / macos) for the reviewed head. If this section is missing or empty, an `approve` verdict cannot be issued.

Clean, waiting for CI (sets `gatekeeper:review-clean` and removes `gatekeeper:changes-requested` via `set-verdict clean`; not a round; lets any tick or machine see the review is done):

```markdown
<!-- gatekeeper-loop: verdict=clean head=<sha> base=<branch> -->

## Gatekeeper · reviewed clean · waiting for CI

### Score

**5/5** — <one line: why this score>

<1–2 lines: what was reviewed, zero open required findings>
```

Needs owner (not a verdict, does not count as a round; post once per reason+head, never duplicate):

```markdown
<!-- gatekeeper-loop: kind=needs-owner reason=ambiguous-review-criteria head=<sha> -->

## Gatekeeper · needs human · ambiguous review criteria

<Concrete draft rubric or checklist the owner must confirm or edit — not open-ended questions.>
```

Reasons: `changes loop skill files` only. A path-name match is never a reason. Missing repo CI is not a needs-owner reason: the gatekeeper files a gap issue instead and keeps reviewing. `changes loop skill files` does not block review or approval — loop-skill PRs are reviewed normally and can be approved; the marker and the `gatekeeper:needs-human` label only signal that the merge itself stays with a human. There is no `kind=needs-human` marker; `kind=needs-owner` is the only human-attention marker kind for this loop.

Escalate (round limit of 5, or an unresolved product/security/data owner decision). A second dispute on the same `G` item is **not** an escalate: decide with `file:line` evidence in the next verdict. Protocol: add `gatekeeper:escalated` first, then post this marker. The only exit is the owner removing the label.

```markdown
<!-- gatekeeper-loop: round=5 verdict=escalate head=<sha> -->

## Gatekeeper · round 5 · Needs maintainer decision

<why the round limit or owner decision cannot be settled here>
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
| `gatekeeper:approved`          | gatekeeper | Approved at the current head; ready for gatekeeper merge once `qa:accepted` and required checks are green at the same head |
| `gatekeeper:escalated`         | gatekeeper | Round limit or unresolved product/security/data owner decision; only the owner removing the label resumes review |
| `gatekeeper:needs-human`       | gatekeeper | Visibility signal for loop-skill changes. It never stops review or approval, and is never set from a path-name match. Kept alongside the verdict label; removed when its reason no longer applies |
| `gatekeeper:review-clean`      | gatekeeper | Extra state label (not a verdict): set by the gatekeeper account on `verdict=clean` while CI runs; kept on `approve`; removed when the PR head changes (before re-review starts), on `changes-requested`, or on `escalate` |

### `gatekeeper:review-clean` label contract (for gated CI)

Projects with expensive CI jobs (e.g. macOS runners) can gate those jobs on the `gatekeeper:review-clean` label and list them under `gatekeeper.required_checks` in `devloops.config.yaml`. The label is added and removed by the gatekeeper account:

- **Set**: when the gatekeeper posts `verdict=clean` (via `pr label set-verdict N clean`, which ensures the label exists first).
- **Kept**: when the gatekeeper posts `approve`.
- **Removed**: when the PR head changes (before re-review starts), or on `changes-requested` / `escalate`.

Example GitHub Actions trigger (documentation only; projects own their CI workflows):

```yaml
on:
  pull_request:
    types: [opened, synchronize, labeled]

jobs:
  macos-tests:
    if: contains(github.event.pull_request.labels.*.name, 'gatekeeper:review-clean')
    runs-on: macos-latest
    steps:
      - uses: actions/checkout@v4
      - run: pytest
```

Unmarked comments from the repo owner (or `@me`) posted after the last marker are read as required items and trigger a review.

Keep exactly one of the first three labels on a PR; swap them with each verdict (`python3 "$SKILL_DIR/scripts/forge.py" pr label set-verdict N <verdict>`). Create missing labels once with `python3 "$SKILL_DIR/scripts/forge.py" label ensure <name> --color <hex> --description <text>`.
