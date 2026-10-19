#!/usr/bin/env bash
# Runs importer/apple_import.py in the same locked-down container as run.sh.
# Usage: apple.sh check | match | import [--apply [--playlists]]
set -euo pipefail

BASE="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$BASE/run/.apple.env}"
OUT="$BASE/out"

# Comments mention the placeholder too, so only settings count
if [[ ! -f "$ENV_FILE" ]] || grep -q '^[^#]*paste_here' "$ENV_FILE"; then
  echo "Copy run/apple.env.example to $ENV_FILE and put both Apple Music tokens into it." >&2
  exit 1
fi

# --init: stopping this script also stops the container
# -u: show progress as it happens (-I ignores PYTHONUNBUFFERED)
docker run --rm --init \
  --read-only --tmpfs /tmp \
  --cap-drop ALL --security-opt no-new-privileges \
  --user "$(id -u):$(id -g)" \
  --env-file "$ENV_FILE" \
  -v "$BASE/importer/apple_import.py:/app/apple_import.py:ro" \
  -v "$OUT:/out" -w /out \
  python:3.13-slim python -I -u /app/apple_import.py "$@"
