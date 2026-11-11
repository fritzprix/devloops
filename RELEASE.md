# Release checklist

Pre-release run sheet for the `devloops` package (issue #8, task 8).

**This document never publishes anything.** Publishing stays with the existing
GitHub ↔ PyPI integration. Do not add PyPI credentials, API tokens, or a
`twine upload` step to this repository.

Evidence below was collected on Windows, Python 3.12.6, `build` 1.4.0, `uv` 0.5.29,
at `main` @ `b54d050`. Re-run the commands each release; the numbers are dated
evidence, not acceptance criteria.

## 1. Package name

- Declared once, in `pyproject.toml` → `[project] name = "devloops"`.
- Console entry point: `[project.scripts] devloops = "devloops_lib.cli:main"`.
- Confirm the entry point is in the built wheel (`entry_points.txt`, step 5) before
  claiming `uvx devloops install` works.
- Confirm the PyPI name is still free before the first release:

  ```
  curl -s -o /dev/null -w "%{http_code}\n" https://pypi.org/pypi/devloops/json
  ```

  `404` means the name is free. Any other code means someone else holds it — stop and
  pick a different name. Observed `404` at the SHA above.

## 2. Version and metadata

- `version` lives only in `pyproject.toml` `[project] version` (currently `0.1.0`).
  There is no `__version__` to keep in sync; do not add one.
- `requires-python = ">=3.8"` — CI must test at least this floor (issue #8 task 13).
- `readme = "README.md"` becomes the long description in the built metadata; check
  `METADATA` in the wheel (step 5) rather than eyeballing the Markdown.

```
python - <<'PY'
import pathlib, re
text = pathlib.Path("pyproject.toml").read_text(encoding="utf-8")
for key in ("name", "version", "requires-python", "license"):
    m = re.search(r'^%s = .*' % re.escape(key), text, re.M)
    print(m.group(0) if m else "%s: MISSING" % key)
PY
```

## 3. License metadata

- `pyproject.toml` declares `license = "MIT"` (PEP 639 SPDX expression) plus the
  `License :: OSI Approved :: MIT License` classifier. Both land in the built
  metadata as `License-Expression: MIT` with `Metadata-Version: 2.5`.
- **Open blocker before any release:** no `LICENSE` file is tracked in git, so neither
  the wheel nor the sdist ships a license text (verified: zero `licen*` entries in
  both artifacts at the SHA above). PyPI shows only the SPDX expression in that case.
  Add `LICENSE` with the MIT text and the real copyright holder — confirm the holder
  with the repo owner; do not guess it. Hatchling includes `LICENSE` from the project
  root automatically, so no build-config change is needed.
- After adding it, re-verify with the artifact inspection in steps 5 and 6 that a
  `LICENSE` entry appears in both the wheel `.dist-info` and the sdist.

## 4. Build both artifacts

From a clean checkout of the release commit (no local edits under `skills/`,
`devloops_lib/`, `templates/`):

```
python -m build -o dist        # or: uv build
```

Expected (versions follow `pyproject.toml`):

- `dist/devloops-<version>-py3-none-any.whl`
- `dist/devloops-<version>.tar.gz`

Both are required: the sdist is what source-first installs and some mirrors consume.
Observed at the SHA above: wheel 73,037 B (49 entries), sdist 51,652 B (50 entries).

## 5. Verify the wheel

```
python - <<'PY'
import os, zipfile, glob
whl = sorted(glob.glob(os.path.join("dist", "*.whl")))[-1]
z = zipfile.ZipFile(whl)
names = z.namelist()
meta = z.read([n for n in names if n.endswith("METADATA")][0]).decode()
print("wheel:", whl)
print("long-description bytes ->", len(meta.split("\n\n", 1)[1].strip()))
for pat in ("SKILL.md", "assets/templates", "entry_points.txt"):
    print(pat, "->", sorted(n for n in names if pat in n))
print("license entries ->", [n for n in names if "licen" in n.lower()])
print("metadata ->", [ln for ln in meta.splitlines()
                      if ln.startswith(("Metadata-Version", "Name", "Version",
                                        "License-Expression", "Requires-Python"))])
print("entry_points ->", z.read([n for n in names if n.endswith("entry_points.txt")][0]).decode())
PY
```

Required entries in the wheel (exact paths):

```
devloops_lib/assets/skills/worker-loop/SKILL.md
devloops_lib/assets/skills/gatekeeper-loop/SKILL.md
devloops_lib/assets/skills/producer-loop/SKILL.md
devloops_lib/assets/templates/devloops.config.yaml
```

Checklist:

- [ ] Each required entry above is present in the wheel.
- [ ] `devloops_lib/assets/templates/devloops.config.yaml` is present.
- [ ] `entry_points.txt` contains `devloops = devloops_lib.cli:main`.
- [ ] `METADATA` matches `pyproject.toml` for name, version, `Requires-Python`,
      and the license expression.
- [ ] `METADATA` carries a non-empty long description (`long-description bytes` above).
- [ ] A `LICENSE` entry is present in `*.dist-info/` (blocked until step 3 is done).
- [ ] `devloops_lib` modules import from the unpacked wheel.

**Dev-only test wrappers (issue #8 task 2): resolved.** The 8 `skills/*/scripts/test_*.py`
wrappers imported the root `tests/` package, which is not installed, so they were broken
in the installed layout. They were removed; the tests live only in `tests/`.
`scripts/ci_smoke.py` fails if a `scripts/test_*` entry appears in the wheel again, and
`tests/test_install_smoke.py` fails if one is installed.

## 6. Verify the sdist

```
python - <<'PY'
import glob, os, tarfile
sd = sorted(glob.glob(os.path.join("dist", "*.tar.gz")))[-1]
names = tarfile.open(sd).getnames()
print("sdist:", sd)
print("top-level:", sorted({n.split("/")[1] for n in names if len(n.split("/")) > 1}))
print("license entries ->", [n for n in names if "licen" in n.lower()])
PY
```

Checklist:

- [ ] Top level includes `README.md`, `pyproject.toml`, `devloops_lib`, `skills`,
      `templates`, `scripts`, and `PKG-INFO`.
- [ ] `PKG-INFO` carries the same name/version/license as the wheel.
- [ ] `LICENSE` is present (blocked until step 3 is done).
- [ ] Root-level docs such as this `RELEASE.md` are **not** in the sdist: the
      `[tool.hatch.build.targets.sdist] include` list is explicit. Add `/RELEASE.md`
      to that list only if the release notes must ship with the source distribution.

## 7. Release trigger (GitHub ↔ PyPI)

The owner reports a GitHub ↔ PyPI integration already exists. **It is not visible from
the source tree:** the only workflow, `.github/workflows/ci.yml`, tests and builds but
never publishes, and there are no git tags or GitHub Releases. So the trigger cannot be
verified from a clone — verify it in the repository settings and UI before the first
release:

- [ ] Find the workflow that publishes (or confirm the integration is configured
      outside the repo). Record its path and its `on:` trigger here.
- [ ] The trigger is a tag push or a published GitHub Release, not `push` on `main`.
- [ ] Authentication uses OIDC trusted publishing with no long-lived token. If a
      `PYPI_TOKEN`-style secret exists, flag it before releasing.
- [ ] The PyPI project the integration targets is exactly `devloops`.
- [ ] The tag version matches `pyproject.toml` `version`; a mismatch is the most
      common silent release failure.
- [ ] Publishing remains out of `pull_request` jobs, and PR jobs stay safe for fork
      PRs with no secrets (issue #8 tasks 14–15).

## 8. Relocated-install smoke test (must pass before release)

CI runs this on every PR head and on `main`, on Linux, Windows, and macOS with Python
3.8 and 3.13: `uv build --out-dir dist && python scripts/ci_smoke.py --dist dist`. The
script inspects both artifacts, runs `tests/test_install_smoke.py` against
`uvx --from <wheel> devloops` in temp directories whose paths contain spaces with no
`PYTHONPATH`, and checks the missing-asset failure from a venv install of the wheel.
Confirm that the CI run on the release commit is green; the manual form is:

```
WHEEL="$CHECKOUT/dist/devloops-<version>-py3-none-any.whl"   # absolute path, resolved before cd
mkdir -p "$TMP/dl rel" && cd "$TMP"
uvx --from "$WHEEL" devloops install --target "$TMP/dl rel/proj"
```

PowerShell form for the Windows run:

```powershell
$wheel = "$CHECKOUT\dist\devloops-<version>-py3-none-any.whl"   # absolute path, resolved before Set-Location
New-Item -ItemType Directory -Force "$TMP\dl rel" | Out-Null
Set-Location "$TMP"
uvx --from $wheel devloops install --target "$TMP\dl rel\proj"
```

- [ ] `.agents/skills/{worker-loop,gatekeeper-loop,producer-loop}/SKILL.md`, each
      skill's `scripts/devloops_lib/`, and `devloops.config.yaml` all exist.
- [ ] `python3 scripts/sync_skill_lib.py --check` passes, so the skills ship the
      released `devloops_lib`.
- [ ] Every script path referenced by the installed `SKILL.md` files resolves and
      runs `--help` from an unrelated current directory.
- [ ] The documented library-import one-liners work outside the source tree.
- [ ] A second `devloops install` keeps edited skill files and the existing config,
      and reports them as skipped or in conflict.
- [ ] With a required asset missing, the installer exits non-zero with a clear
      message and never reports partial success.
- [ ] Stdout lists real installed/skipped destination paths only.

## 9. Pre-release run sheet

In order; every box must be checked by the person tagging the release.

- [ ] Full test suite green on the release commit (`python -m pytest tests/ -q` or
      `python -m unittest discover -s tests`). Do not hard-code the test count.
- [ ] `version` bumped in `pyproject.toml`, and the tag to be pushed matches it.
- [ ] `LICENSE` tracked in git and shipped in both artifacts (step 3).
- [x] Dev-only `test_*.py` asset decision resolved (step 5): removed.
- [ ] Both artifacts rebuilt from a clean checkout (step 4).
- [ ] Wheel and sdist inspected (steps 5 and 6).
- [ ] Relocated-install smoke test green in CI on Linux, Windows, and macOS for the release commit (step 8).
- [ ] Release trigger and PyPI target confirmed (step 7).
- [ ] Publish happens only through the existing integration — no local upload,
      no credentials added to the repo.
- [ ] Tag pushed / Release published, then the published project metadata re-checked
      on PyPI for name, version, license, and supported Python.

## Out of scope for this checklist

Publishing to PyPI, adding CI credentials, and setting up a new PyPI integration.
CI (`.github/workflows/ci.yml`) runs on `pull_request` and `push` to `main` with
`permissions: contents: read` and no secrets, so fork PRs are safe.
