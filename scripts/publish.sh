#!/usr/bin/env bash
# Publish one local folder to files.maxwe.me as a tar.gz.
#
# Usage:
#   scripts/publish.sh <local-folder> [remote-name]
#
# Token setup (once):
#   printf 'FILES_PUBLISH_TOKEN=%s\n' "$(openssl rand -hex 32)" >> ~/Projects/files/.env
#   sudo systemctl restart files.service
#   export FILES_PUBLISH_TOKEN=...        # for the CLI (or leave it in .env)
#
# Optional environment:
#   FILES_PUBLISH_URL   default https://files.maxwe.me/publish
#                       (use http://127.0.0.1:8760/publish for local tests)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "usage: $0 <local-folder> [remote-name]" >&2
  exit 2
fi

if [[ ! -d "$1" ]]; then
  echo "error: not a directory: $1" >&2
  exit 2
fi

# Load the service's .env (gitignored) so the CLI uses the same token.
if [[ -f "$REPO/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$REPO/.env"
  set +a
fi

SRC="$(cd "$1" && pwd)"
NAME="${2:-$(basename "$SRC")}"
NAME="${NAME%/}"
URL="${FILES_PUBLISH_URL:-https://files.maxwe.me/publish}"
TOKEN="${FILES_PUBLISH_TOKEN:-}"

if [[ -z "$TOKEN" ]]; then
  cat >&2 <<'MSG'
error: FILES_PUBLISH_TOKEN is not set.

Create the token on the server:
  printf 'FILES_PUBLISH_TOKEN=%s\n' "$(openssl rand -hex 32)" >> ~/Projects/files/.env
  sudo systemctl restart files.service
Then export it in your shell (or keep it in ~/Projects/files/.env):
  export FILES_PUBLISH_TOKEN=...
MSG
  exit 2
fi

if [[ "$NAME" == */* || "$NAME" == "." || "$NAME" == ".." || "$NAME" == .* ]]; then
  echo "error: remote name must be a single, non-hidden path component" >&2
  exit 2
fi

TMP="$(mktemp "${TMPDIR:-/tmp}/publish-${NAME//[^A-Za-z0-9._-]/_}-XXXXXX.tar.gz")"
RESP="$(mktemp "${TMPDIR:-/tmp}/publish-response-XXXXXX.json")"
cleanup() { rm -f "$TMP" "$RESP"; }
trap cleanup EXIT

PARENT="$(dirname "$SRC")"
BASE="$(basename "$SRC")"

echo "packing $SRC ..." >&2
tar -czf "$TMP" -C "$PARENT" \
  --exclude='.git' --exclude='.git/*' \
  --exclude='node_modules' --exclude='node_modules/*' \
  --exclude='__pycache__' --exclude='__pycache__/*' \
  --exclude='.venv' --exclude='.venv/*' \
  --exclude='*.pyc' --exclude='*.swp' --exclude='.DS_Store' \
  "$BASE"

echo "uploading as '$NAME' to $URL ..." >&2
HTTP="$(curl -sS -o "$RESP" -w '%{http_code}' \
  -X POST "$URL" \
  -H "Authorization: Bearer $TOKEN" \
  --form-string "folder=$NAME" \
  -F "archive=@$TMP" || true)"

if [[ "$HTTP" != 2* ]]; then
  echo "error: publish failed (HTTP ${HTTP:-000})" >&2
  cat "$RESP" >&2 || true
  echo >&2
  exit 1
fi

python3 - "$RESP" "$NAME" <<'PY'
import json
import sys

path, name = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception:
    sys.stderr.write(open(path, encoding="utf-8", errors="replace").read())
    raise SystemExit(1)
if not data.get("ok"):
    sys.stderr.write(json.dumps(data, indent=2) + "\n")
    raise SystemExit(1)
print(data.get("url") or f"/{name}/")
print(
    f"published '{data.get('folder', name)}': "
    f"{data.get('files', 0)} files, {data.get('bytes', 0)} bytes",
    file=sys.stderr,
)
PY
