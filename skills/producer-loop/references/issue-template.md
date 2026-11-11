# Issue Template (ASD-STE100 Standard)

Use this template for all issues created by `producer-loop`. Write in Simplified Technical English. Keep sentences short and direct. Do not include passwords, secret keys, access tokens, or private path names.

```markdown
# [Producer: <Task>] <Short Description of Problem>

<!-- producer:fp=<FINGERPRINT_HEX> -->

## Description
State the problem clearly. Use one idea per sentence.
Explain the condition that requires change.

## Evidence
Provide the verified facts found during inspection:
- Location: `<file_path>:<line_number>`
- Identified condition: `<factual description without secrets or private paths>`
- Impact: `<concrete failure mode or risk>`

## High-Level Requirements
Define the requirements to resolve this issue:
- [ ] Requirement 1: <Action required to fix root cause>
- [ ] Requirement 2: <Add regression test or validation guard>
- [ ] Requirement 3: <Verify surrounding components remain compatible>

## Recommendation
If one approach is clearly best, state it here. If multiple valid approaches change the outcome, list them under **Options** and ask for an owner **Decision**—do not imply the worker should pick. Prefer steps covered by `environment.capabilities` (tests, browser, diagrams)—not owner-only busywork.

## Verification Steps
State the test procedure to confirm the solution:
1. Run `<test command or reproduction script>`.
2. Confirm the expected result occurs.

## Notice
This issue was generated automatically by the devloops producer loop.
Maintainers: do not post credentials, secrets, or internal server paths in comments.
```
