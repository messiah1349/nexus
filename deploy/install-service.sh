#!/usr/bin/env bash
#
# install-service.sh — register the Nexus bot as a systemd service so it
# survives reboots and auto-restarts on crash. Run after deploy.sh.
#
#   bash deploy/install-service.sh
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(whoami)"
UV_BIN="$(command -v uv)"

UNIT=/etc/systemd/system/nexus-bot.service

echo "==> Writing $UNIT"
sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=Nexus Telegram bot
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$REPO_DIR
# .env is read by the app itself (pydantic-settings), so no EnvironmentFile needed.
ExecStart=$UV_BIN run nexus bot
Restart=always
RestartSec=5
# uv installs to ~/.local/bin; ensure it's on PATH for the service.
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=multi-user.target
EOF

echo "==> Enabling and starting the service"
sudo systemctl daemon-reload
sudo systemctl enable --now nexus-bot

echo
echo "==> Done. Useful commands:"
echo "    sudo systemctl status nexus-bot     # check it's running"
echo "    journalctl -u nexus-bot -f          # tail logs"
echo "    sudo systemctl restart nexus-bot    # after a git pull + uv sync"
