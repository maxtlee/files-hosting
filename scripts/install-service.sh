#!/usr/bin/env bash
# Install/refresh the `files` systemd unit for the current user and checkout.
#
# The unit is derived from this checkout (User, WorkingDirectory, repo path) so
# it is reproducible on any machine. The app binds loopback only; public access
# is handled by Tailscale Funnel at /files (see ../README.md).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(id -un)"
HOME_DIR="${HOME}"
UNIT=/etc/systemd/system/files.service

sudo tee "${UNIT}" >/dev/null <<EOF
[Unit]
Description=files — public static host + photo albums (served at /files)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USER_NAME}
Group=${USER_NAME}
WorkingDirectory=${REPO}
ExecStart=${REPO}/scripts/run.sh
Restart=on-failure
RestartSec=3
Environment=HOME=${HOME_DIR}
Environment=FILES_DIR=/mnt/archive-new/files
Environment=FILES_ROOT_PATH=/files
Environment=FILES_HOST=127.0.0.1
Environment=FILES_PORT=8760
Environment=PYTHONUNBUFFERED=1
# --- public photo albums (/albums) -----------------------------------------
# Secrets (SMTP/API keys) belong in the repo .env file, which run.sh sources.
Environment=ALBUMS_DATA_DIR=${REPO}/data
Environment=ALBUMS_HDD_DIR=/mnt/archive-new/files/albums
Environment=ALBUMS_HDD_MOUNT=/mnt/archive-new
Environment=ALBUMS_CACHE_DIR=${REPO}/data/cache
Environment=ALBUMS_DB_PATH=${REPO}/data/albums.sqlite3
Environment=ALBUMS_THUMB_DIR=${REPO}/data/thumbs
Environment=ALBUMS_PREVIEW_DIR=${REPO}/data/previews
Environment=ALBUMS_TMP_DIR=${REPO}/data/tmp
Environment=ALBUMS_CACHE_BUDGET_BYTES=6442450944
Environment=ALBUMS_THUMB_BUDGET_BYTES=536870912
Environment=ALBUMS_PREVIEW_BUDGET_BYTES=2147483648
Environment=ALBUMS_PREVIEW_IMAGE_MAX_PX=2048
Environment=ALBUMS_PREVIEW_VIDEO_HEIGHT=720
Environment=ALBUMS_MIN_FREE_BYTES=5368709120
Environment=ALBUMS_MAX_FILE_BYTES=1073741824
Environment=ALBUMS_MAX_FILES=50
Environment=ALBUMS_MAX_REQUEST_BYTES=2147483648
Environment=ALBUMS_RATE_WINDOW_SECONDS=300
Environment=ALBUMS_RATE_LIMIT_BYTES=10737418240
Environment=ALBUMS_PER_IP_RATE_BYTES=2147483648
Environment=ALBUMS_NOTIFY_ENABLED=0
Environment=ALBUMS_NOTIFY_TO=maxwe@cmu.edu
Environment=ALBUMS_PUBLIC_BASE_URL=https://agentmaxxer.tail6ca935.ts.net/files

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable files.service
sudo systemctl restart files.service
echo "installed ${UNIT}"
echo "  user  : ${USER_NAME}"
echo "  repo  : ${REPO}"
echo "  status: $(systemctl is-active files.service)"
