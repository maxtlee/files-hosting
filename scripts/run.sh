#!/usr/bin/env bash
# Run the files static host. Loads .env if present.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

exec .venv/bin/uvicorn app:asgi_app \
  --host "${FILES_HOST:-127.0.0.1}" \
  --port "${FILES_PORT:-8760}" \
  "$@"
