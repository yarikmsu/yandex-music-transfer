#!/usr/bin/env bash
# Runs the reviewed fetch_tracks.py in a throwaway, locked-down container.
# The container sees only the script (read-only) and the output folder.
# Tokens arrive as environment variables and are never written inside it.
set -euo pipefail

BASE="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$BASE/exporter/fetch_tracks.py"
ENV_FILE="${ENV_FILE:-$BASE/run/.env}"
OUT="$BASE/out"
# sha256 of the reviewed fetch_tracks.py: upstream 84c0631 plus the albumId fallback
# (see exporter/README.md). Update it only after reviewing a change to the script.
EXPECTED_SHA="392227a36357e398d6f6c677fd5d69c29db6135c39dd22e8e7356744146aa072"

actual_sha="$(shasum -a 256 "$SCRIPT" | cut -d' ' -f1)"
if [[ "$actual_sha" != "$EXPECTED_SHA" ]]; then
  echo "fetch_tracks.py differs from the reviewed version; refusing to run." >&2
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]] || grep -q 'your_token_here' "$ENV_FILE"; then
  echo "Copy run/env.example to $ENV_FILE and put your token into it." >&2
  exit 1
fi

mkdir -p "$OUT"
docker run --rm \
  --read-only --tmpfs /tmp \
  --cap-drop ALL --security-opt no-new-privileges \
  --user "$(id -u):$(id -g)" \
  --env-file "$ENV_FILE" \
  -v "$SCRIPT:/app/fetch_tracks.py:ro" \
  -v "$OUT:/out" -w /out \
  python:3.13-slim python -I /app/fetch_tracks.py
