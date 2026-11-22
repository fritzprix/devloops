# Testability review

qa answers one question for each acceptance criterion and each changed behavior: **can a qa tool show that it works, and show that it fails when it is broken?**

## Tool inventory

The inventory is `qa.tools` in `devloops.config.yaml` (name: what it proves and how to run it). qa counts on these tools only. Tools in `qa.blocked_tools` do not count until the owner removes them from that list.

Typical tool kinds. The project declares which of them it has:

| Kind | Proves | Notes |
| --- | --- | --- |
| Unit / integration tests | Logic, error paths, boundaries | The cheapest path. Prefer it. |
| End-to-end control API | A running app does the behavior: sessions, approvals, prompts | Usually a debug build only, loopback only, with an isolated data directory. |
| Mock LLM server (OpenAI-compatible) | Model-driven behavior with fixed replies: invalid tool arguments, errors, slow streams, HTTP 4xx/5xx | Makes model output deterministic. Logs every request for evidence. |
| Headless run | The app or CLI path without a display | Use an isolated data directory. |
| Virtual display + screen capture (for example Xvfb + ffmpeg) | Something is visible on screen | Evidence only. Use it when no API or log shows the result. |
| CI | The same tests on each OS | Read results. Do not wait for CI in a tick. |

## Rules

1. **One path per criterion.** Map each criterion to one tool and concrete steps. "Covered by E2E" without steps is not a path.
2. **Must fail when broken.** A test that passes without the change does not count. For a bug fix, the test must fail on the base branch.
3. **Observable result.** The tool must read the result: return value, API field, event, transcript entry, log marker, or exit code. A screenshot is the last choice.
4. **Controllable input.** Time, randomness, network, and model output must be fixed or faked. If not, demand a test hook.
5. **No live paid services.** A check that needs a real external account or paid API is not a qa path. Demand a fake or a mock.
6. **Isolation.** App runs use an isolated data directory and scratch workspace. Never the user's default profile.
7. **Blocked tools.** If the only path needs a blocked tool, the verdict is `needs-human` with reason `tool-blocked`. It is not the worker's fault.
8. **Size.** A docs-only or comment-only change has no behavior to test: accept it with "no behavior change" in `### Checked`.

## Demands (most preferred first)

| Gap | Demand |
| --- | --- |
| Logic with no test | A unit or integration test that fails without the change. |
| Behavior is not observable | A test hook: API field, event, log marker, or transcript entry. |
| Input is not controllable | A seam: fake provider, mock server URL setting, fixed clock or seed. |
| Only a human can check it | A `## Manual verification` section in the PR body. qa then posts `needs-human`. |

Write each demand as a `Q` item: the criterion, why no path exists, and the exact thing to add. Do not write a patch.
