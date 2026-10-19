#!/usr/bin/env bash
# Runs importer/ytmusic_import.py in the same locked-down container as run.sh.
# Usage: ytmusic.sh check | match | import [--apply]
set -euo pipefail

BASE="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$BASE/run/.ytmusic.env}"
OUT="$BASE/out"

# Only check and import --apply sign in; match and the dry run never see the cookie
login=()
if [[ "${1:-}" == "check" || " $* " == *" --apply "* ]]; then
  if [[ ! -f "$ENV_FILE" ]] || grep -q 'paste_here' "$ENV_FILE"; then
    echo "Copy run/ytmusic.env.example to $ENV_FILE and put your music.youtube.com cookie into it." >&2
    exit 1
  fi
  login=(--env-file "$ENV_FILE")
fi

# --init: stopping this script also stops the container
# -E -s rather than -I: -I also leaves /app off sys.path, and the script imports apple_import from it
# -B: /app is read-only, so do not try to write bytecode there
# -u: show progress as it happens
# ${login[@]+...}: an empty array is "unbound" to set -u in macOS's bash 3.2
docker run --rm --init \
  --read-only --tmpfs /tmp \
  --cap-drop ALL --security-opt no-new-privileges \
  --user "$(id -u):$(id -g)" \
  ${login[@]+"${login[@]}"} \
  -v "$BASE/importer/apple_import.py:/app/apple_import.py:ro" \
  -v "$BASE/importer/ytmusic_import.py:/app/ytmusic_import.py:ro" \
  -v "$OUT:/out" -w /out \
  python:3.13-slim python -E -s -B -u /app/ytmusic_import.py "$@"
