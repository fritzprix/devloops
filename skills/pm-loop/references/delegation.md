# Delegation budget

The user sets the **total concurrent delegate cap** with `slots: N` on `/pm-loop`. The PM allocates worker, gatekeeper, and producer ticks inside that cap.

## Backends

`delegation: cursor | claude | harness` is stored in `pm-loop` state. If the chosen backend cannot run (missing binary, auth, or quota), **stop and ask**; do not silently fall back to another backend.

## Caps

- Do not assign two worker delegates to the same PR in one PM tick.
- Do not start producer when the open-issue board is at capacity (`boardFull` in the pipeline JSON passed to `plan_tick.py`).
- Prefer gatekeeper ticks when review is the bottleneck; prefer worker ticks when approved issues pile up; prefer base-branch CI follow-up before new feature work when CI is blocked on the base.
