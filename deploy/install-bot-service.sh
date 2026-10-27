#!/usr/bin/env bash
# Install the Telegram review bot as a systemd service (Ubuntu/Debian).
# Run from the project folder:  bash deploy/install-bot-service.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(id -un)"
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
[ -x "$UV" ] || { echo "uv not found - install it first: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
[ -f "$DIR/.env" ] || { echo "Missing $DIR/.env (copy .env.example and fill it in)"; exit 1; }

sudo tee /etc/systemd/system/youth-feedback-bot.service >/dev/null <<UNIT
[Unit]
Description=Youth feedback Telegram review bot
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=$UV run --frozen youth-feedback bot
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now youth-feedback-bot
sleep 3
systemctl --no-pager status youth-feedback-bot | head -5
echo
echo "Logs: journalctl -u youth-feedback-bot -f"
