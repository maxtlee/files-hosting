#!/usr/bin/env bash
# Run the files host (static drop box + public albums). Loads .env if present.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

# Keep multipart spool files on the SSD data volume (bounded by the upload cap),
# never in a potentially small /tmp.
ALBUMS_DATA_DIR="${ALBUMS_DATA_DIR:-$PWD/data}"
mkdir -p "$ALBUMS_DATA_DIR/tmp"
export ALBUMS_DATA_DIR TMPDIR="$ALBUMS_DATA_DIR/tmp"

exec .venv/bin/uvicorn app:asgi_app \
  --host "${FILES_HOST:-127.0.0.1}" \
  --port "${FILES_PORT:-8760}" \
  "$@"
