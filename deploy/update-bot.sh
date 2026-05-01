#!/bin/bash
# ─── Celerity Bot — Update Script ───
# Run from your Mac to push code changes to the server.
# Usage: bash deploy/update-bot.sh user@YOUR_SERVER_IP

set -e

SERVER="${1:-}"
BOT_DIR="/opt/celerity-bot"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ -z "$SERVER" ]; then
    echo "Usage: bash deploy/update-bot.sh user@YOUR_SERVER_IP"
    exit 1
fi

echo "▸ Syncing code to $SERVER..."
rsync -az --progress \
    --exclude='venv' --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='.env' --exclude='logs/' --exclude='data/' --exclude='models/' \
    "$LOCAL_DIR/" "${SERVER}:${BOT_DIR}/"

echo "▸ Restarting service..."
ssh "$SERVER" "systemctl restart celerity-bot && systemctl status celerity-bot --no-pager"

echo ""
echo "✅ Update complete. Bot restarted."
echo "   View logs: ssh $SERVER 'journalctl -u celerity-bot -f'"
