# Post-merge testing

qa tests what was merged, on the integrated base branch, not on the PR branch.

## Worktree

Use one qa worktree. Do not use the main checkout or a worker worktree. Keep it between ticks so caches stay warm.

```bash
MAIN=$(git rev-parse --show-toplevel)
WT="$MAIN/../$(basename "$MAIN").worktrees/qa${DEVLOOPS_AGENT_ID:+-$DEVLOOPS_AGENT_ID}"
git -C "$MAIN" fetch origin <base_branch> --quiet
git -C "$MAIN" worktree list --porcelain | grep -qx "worktree $WT" || git -C "$MAIN" worktree add --detach "$WT" "origin/<base_branch>"
git -C "$WT" checkout --detach --force "origin/<base_branch>" && git -C "$WT" clean -fd
```

Record `BUILD=$(git -C "$WT" rev-parse HEAD)`. For each pending PR, check `git -C "$WT" merge-base --is-ancestor <mergeCommit> HEAD`. A PR that is not in the build stays pending (`record N --result blocked`).

## Order

1. **Build** with `qa.post_merge.build` (or `commands.build`). A build failure is one issue for the build, linked to every pending PR in the build. Mark the PRs `fail`.
2. **Tests** with `qa.post_merge.test` (or `commands.test`).
3. **End-to-end** with `qa.post_merge.e2e`, if set. Start only processes that this tick owns, with an isolated data directory. Stop them at the end of the tick. Never stop a process that this tick did not start.
4. **Per-PR checks** from the `### Test plan` of each PR's last qa `accepted` verdict.

Keep the evidence (command, exit code, short log excerpt, screenshot path) in a scratch directory outside the repository.

## Results

| Result | When | Next |
| --- | --- | --- |
| `pass` | All checks for the PR pass on this build. | `record N --result pass --build $BUILD` |
| `fail` | A check for the PR fails on this build. | File an issue, then `record N --result fail --build $BUILD --issue M` |
| `blocked` | The PR is not in the build, or a needed tool is blocked or missing. | `record N --result blocked --build $BUILD`; it stays pending. |

A failure that also fails on the build before the merge is not caused by the PR. File it once, without a PR link.

## Filing an issue

1. Search first for an open issue with the same `<!-- qa-loop: kind=post-merge pr=N ... check=X -->` marker. If one exists, comment with the new build SHA. Do not file again.
2. Dry run: write the title and body and read them. Body sections: Summary, Steps to reproduce, Expected, Actual (short log excerpt), Build SHA, Merged PR link. English, ASD-STE100 style, no secrets, no private paths, no personal names.
3. Create it with the labels in `qa.issue_labels`. Respect `qa.max_new_issues_per_tick`.
4. Add it to the project board when `board.provider` is set, and set each field in `qa.board_fields`.
5. **Dependencies (hard rule).** If the failure must wait for another open issue, link it as a real GitHub dependency and verify it:
   ```bash
   python3 "$SKILL_DIR/scripts/qa_queue.py" link-blocked-by --issue <new> --blocked-by <other>
   ```
   The script re-queries the API and prints the verified `blockedBy` list. Exit 1 means the link is not there: report it as not linked. Never report a dependency that the re-query does not show. A "Depends on #N" line in the body is not enough.
6. Never add the `approved` label. A human approves the work.
