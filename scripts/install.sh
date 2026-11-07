#!/usr/bin/env bash
# Install devloops skills into a target project.
# Usage: install.sh <project-dir> [--link]
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:?usage: install.sh <project-dir> [--link]}"
MODE="${2:-}"

if [ "$MODE" = "--link" ]; then
  mkdir -p "$DEST/.agents/skills"
  for s in "$SRC"/skills/*/; do
    name="$(basename "$s")"
    target="$DEST/.agents/skills/$name"
    if [ -e "$target" ]; then echo "skip $name (exists)"; continue; fi
    ln -s "$s" "$target"
    echo "installed $name"
  done
  target_lib="$DEST/.agents/devloops_lib"
  if [ ! -e "$target_lib" ]; then
    ln -s "$SRC/devloops_lib" "$target_lib"
    echo "installed devloops_lib"
  fi
  [ -e "$DEST/devloops.config.yaml" ] || cp "$SRC/templates/devloops.config.yaml" "$DEST/"
else
  # Delegate to devloops CLI installer
  PYTHONPATH="$SRC" python3 -m devloops_lib.cli install --target "$DEST"
fi
