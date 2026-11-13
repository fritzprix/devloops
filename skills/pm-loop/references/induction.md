# Project induction

On the first PM tick, build `projectProfile` in `pm-loop` state and reuse it.

## Policy priority

When sources disagree, apply this order:

1. Role skill (`worker-loop`, `gatekeeper-loop`, `producer-loop`)
2. `devloops.config.yaml`
3. Inferred `projectProfile`
4. Informal issue/PR comment folklore (**never** overrides a skill)

```bash
python3 "$SKILL_DIR/scripts/pm_profile.py" --write
```

## Never infer from the repository

- Slot budget (`slots`)
- Delegation backend (`cursor`, `claude`, `harness`)
- Who may add `approved` or merge PRs (always human)

## Mixed repositories

`projectType` may be `software`, `research`, `writing`, or `mixed`. Mixed repos are not forced into one mode; role skills and config still win over the type default.
