#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  Celerity Trader Bot — Server Setup Script
#  Tested on Ubuntu 22.04 LTS
#  Run as root:  bash setup-server.sh
# ═══════════════════════════════════════════════════════════════
set -e

BOT_DIR="/opt/celerity-bot"
BOT_USER="celerity"
SERVICE_NAME="celerity-bot"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"   # parent of deploy/

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   CELERITY BOT — SERVER SETUP               ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ── 1. System dependencies ──────────────────────────────────────
echo "▸ Installing system dependencies..."
apt-get update -qq
apt-get install -y python3 python3-pip python3-venv git curl ufw -qq
echo "  ✓ System dependencies installed"

# ── 2. Create dedicated user ────────────────────────────────────
if ! id "$BOT_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /bin/false "$BOT_USER"
    echo "  ✓ User '$BOT_USER' created"
else
    echo "  ✓ User '$BOT_USER' already exists"
fi

# ── 3. Copy bot files ───────────────────────────────────────────
echo "▸ Copying bot files to $BOT_DIR..."
mkdir -p "$BOT_DIR"
rsync -a --exclude='venv' --exclude='__pycache__' --exclude='*.pyc' \
         --exclude='.env' --exclude='logs/*' --exclude='data/*.json' \
         "$REPO_DIR/" "$BOT_DIR/"
mkdir -p "$BOT_DIR/logs" "$BOT_DIR/data" "$BOT_DIR/models"
echo "  ✓ Files copied"

# ── 4. Python virtual environment ───────────────────────────────
echo "▸ Setting up Python virtual environment..."
python3 -m venv "$BOT_DIR/venv"
"$BOT_DIR/venv/bin/pip" install --upgrade pip -q
"$BOT_DIR/venv/bin/pip" install -r "$BOT_DIR/requirements.txt" -q
echo "  ✓ Dependencies installed"

# ── 5. .env file ────────────────────────────────────────────────
if [ ! -f "$BOT_DIR/.env" ]; then
    cp "$BOT_DIR/.env.example" "$BOT_DIR/.env"
    echo ""
    echo "  ⚠️  .env file created from template."
    echo "     Edit it now with your API keys:"
    echo "     nano $BOT_DIR/.env"
    echo ""
fi

# ── 6. Permissions ──────────────────────────────────────────────
chown -R "$BOT_USER:$BOT_USER" "$BOT_DIR"
chmod 600 "$BOT_DIR/.env"          # Only owner can read keys
chmod +x "$BOT_DIR/start.sh"
echo "  ✓ Permissions set (keys protected)"

# ── 7. Systemd service ──────────────────────────────────────────
echo "▸ Installing systemd service..."
cp "$BOT_DIR/deploy/celerity-bot.service" "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
echo "  ✓ Service installed and enabled on boot"

# ── 8. Firewall ─────────────────────────────────────────────────
echo "▸ Configuring firewall..."
ufw allow OpenSSH
# Dashboard only accessible via SSH tunnel — NOT exposed publicly
ufw --force enable
echo "  ✓ Firewall: SSH open, dashboard port (5001) kept internal"

# ── 9. Done ─────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   SETUP COMPLETE                            ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "  Next steps:"
echo ""
echo "  1. Edit your API keys:"
echo "     nano $BOT_DIR/.env"
echo ""
echo "  2. Start the bot:"
echo "     systemctl start $SERVICE_NAME"
echo ""
echo "  3. Check status:"
echo "     systemctl status $SERVICE_NAME"
echo "     journalctl -u $SERVICE_NAME -f"
echo ""
echo "  4. Access dashboard from your Mac:"
echo "     ssh -L 5001:localhost:5001 user@YOUR_SERVER_IP"
echo "     Then open: http://localhost:5001"
echo ""
