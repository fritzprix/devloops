# qa-loop comment protocol

qa uses the same method as gatekeeper-loop: a PR comment with a hidden marker on the first line, plus one `qa:*` label. The scripts trust only markers in comments by the authenticated user.

## Verdict

`head` is the full SHA that qa reviewed. `round` starts at 1 and increments per verdict. `base` is the PR's base branch at review time; after a retarget, only a verdict carrying the new `base=` counts for the merge gate.

```markdown
<!-- qa-loop: kind=verdict round=1 verdict=changes-requested head=<sha> base=<base-branch> -->

## QA · round 1 · Changes requested (testability)

Scope: <one line: what changes, and which behaviors need a test path>

### Required

- **Q1.1** <criterion or behavior> — no test path with the qa tools. Demand: a test in `<path>` that fails without the change.
- **Q1.2** <behavior> — not observable. Demand: test hook <event / API field / log marker> so that `<tool>` can assert it.

### Test plan (for the parts that are testable)

- <criterion> — `<tool>`: <steps> → <expected result>
```

Accepted (the `### Test plan` is mandatory; post-merge testing uses it):

```markdown
<!-- qa-loop: kind=verdict round=2 verdict=accepted head=<sha> base=<base-branch> -->

## QA · round 2 · ✅ Accepted (testable)

### Test plan

| Criterion / behavior | Test path | Tool | Command or steps | Expected |
| --- | --- | --- | --- | --- |
| … | PR test / existing test / qa tool check | `unit` | `…` | … |

### Checked

- Q1.1 ✅ fixed in <short-sha>: `<test file>:<line>` fails without the change (checked locally / reasoned from the diff).
```

Needs human (a verdict; it blocks the sign-off until a human answers):

```markdown
<!-- qa-loop: kind=verdict round=2 verdict=needs-human reason=<manual-only|tool-blocked|repeated-decline|loop-skill> head=<sha> -->

## QA · round 2 · Needs human

<What cannot be tested, why, and the decision that is necessary.
For tool-blocked: the blocked tool, its tracking issue, and the test qa will run when it works.
For manual-only: the steps from the PR's "Manual verification" section.>
```

## Worker response

The worker answers a qa `changes-requested` verdict in one comment:

```markdown
<!-- worker-loop: qa-response round=1 head=<new-sha> -->

## Worker response to QA · round 1

- **Q1.1** fixed in <short-sha>: <test name>
- **Q1.2** declined: <reason>
```

## Labels

| Label | Meaning |
| --- | --- |
| `qa:accepted` | qa accepted the current head. One half of the sign-off. |
| `qa:changes-requested` | The worker must add a test, a test hook, or a manual-verification note. |
| `qa:needs-human` | qa cannot decide alone. A human answers on the PR. |

Keep exactly one `qa:*` label on a PR. Swap it with each verdict:

```bash
python3 "$SKILL_DIR/scripts/forge.py" pr comment N --body-file <tmp>
python3 "$SKILL_DIR/scripts/forge.py" pr label add N qa:accepted && python3 "$SKILL_DIR/scripts/forge.py" pr label remove N qa:changes-requested qa:needs-human
python3 "$SKILL_DIR/scripts/forge.py" pr label add N qa:changes-requested && python3 "$SKILL_DIR/scripts/forge.py" pr label remove N qa:accepted qa:needs-human
python3 "$SKILL_DIR/scripts/forge.py" pr label add N qa:needs-human && python3 "$SKILL_DIR/scripts/forge.py" pr label remove N qa:accepted qa:changes-requested
```

Create missing labels once with `python3 "$SKILL_DIR/scripts/forge.py" label ensure <name> --color <hex> --description <text>`.

The `qa:*` labels and the `gatekeeper:*` labels are separate axes. qa never changes `gatekeeper:*` labels, and the gatekeeper never changes `qa:*` labels.

## Post-merge issue

Each failure issue body starts with a marker so the next tick does not file it again:

```markdown
<!-- qa-loop: kind=post-merge pr=<merged PR> build=<sha> check=<test or check id> -->
```

Before filing, search for open issues with the same `pr=` and `check=`. If one exists, add a comment with the new build SHA instead.
