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
Description=files — public static file host (HDD drop box, served at /files)
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
