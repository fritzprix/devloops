# devloops

Reusable agent loop skills for software projects.

The loops form one cycle:

```
producer-loop  -> issue (auto:producer)
gatekeeper     -> validated | rejected
human          -> approved
worker-loop    -> PR
gatekeeper     -> review verdict
human          -> merge
```

## Layout

| Path | Purpose |
| --- | --- |
| `skills/` | Skills. Each skill has `SKILL.md`, `references/`, `scripts/`. |
| `devloops_lib/` | Shared code: tick gate (lock, TTL, NOOP, state), configuration, doctor, PR status. |
| `templates/` | Files that a target project copies, for example `devloops.config.yaml`. |
| `scripts/install.sh` | Copies or links the skills and shared devloops_lib into a target project. |
| `tests/` | Tests for `devloops_lib/` and the skill scripts. |

## Use in a project

```sh
~/my_works/devloops/scripts/install.sh /path/to/project
# then edit /path/to/project/devloops.config.yaml
```

## Testing

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```

## Rules

- Loops never merge, tag, or publish.
- Producers only create issues.
- The gatekeeper only judges validity. A human adds `approved`.
- Project-specific values (commands, protected paths, labels) live in `devloops.config.yaml`, not in the skills.
