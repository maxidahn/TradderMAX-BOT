#!/bin/bash
# ─── Celerity Trader Bot — Local / Server Launcher ───

set -e

# Load .env if present (server mode)
if [ -f ".env" ]; then
    set -a; source .env; set +a
fi

echo "================================================"
echo "  CELERITY TRADER BOT — Setup & Launch"
echo "================================================"
echo ""

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "ERROR: Python 3 not found. Install it first."
    exit 1
fi

echo "Python: $(python3 --version)"
echo ""

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
    echo "Virtual environment created."
fi

# Activate
source venv/bin/activate
echo "Virtual environment activated."
echo ""

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt --quiet
echo "Dependencies installed."
echo ""

# Create logs directory
mkdir -p logs

# Check for Binance API keys
if [ -z "$BINANCE_API_KEY" ] || [ -z "$BINANCE_API_SECRET" ]; then
    echo "WARNING: Binance API keys not set."
    echo "  The bot will start but cannot execute trades."
    echo "  Set them with:"
    echo "    export BINANCE_API_KEY=\"your_key\""
    echo "    export BINANCE_API_SECRET=\"your_secret\""
    echo ""
fi

# Show mode
if [ "${BINANCE_TESTNET}" = "false" ]; then
    echo "MODE: LIVE TRADING"
else
    echo "MODE: TESTNET (safe)"
fi
echo ""

echo "Starting bot..."
echo "Dashboard: http://localhost:5000"
echo "Press Ctrl+C to stop"
echo "================================================"
echo ""

python3 app.py
