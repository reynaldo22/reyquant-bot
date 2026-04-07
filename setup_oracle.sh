#!/bin/bash
# ============================================================
# REYQUANT BOT — Oracle Cloud Setup Script
# Run this once after SSH into your Oracle VM
# Usage: bash setup_oracle.sh
# ============================================================

set -e
echo "⚡ Setting up Rey Quant Trading Bot..."

# ── 1. Update system ────────────────────────────────────────
echo "[1/6] Updating system packages..."
sudo apt-get update -qq
sudo apt-get install -y python3 python3-pip git curl -qq

# ── 2. Install Python dependencies ──────────────────────────
echo "[2/6] Installing Python packages..."
pip3 install --quiet \
    python-telegram-bot \
    pandas \
    numpy \
    finta \
    requests

echo "  Packages installed ✅"

# ── 3. Create app directory ─────────────────────────────────
echo "[3/6] Setting up app directory..."
mkdir -p ~/reyquant
cp *.py ~/reyquant/
echo "  Scripts copied to ~/reyquant/ ✅"

# ── 4. Create environment file ──────────────────────────────
echo "[4/6] Creating environment config..."
cat > ~/reyquant/.env << 'ENVEOF'
TELEGRAM_BOT_TOKEN=8756055689:AAEB36717g1HPnAL7yKSWe3svle40qkQT4Y
TELEGRAM_CHAT_ID=8776067501
ENVEOF
chmod 600 ~/reyquant/.env
echo "  Environment file created ✅"

# ── 5. Create systemd service ────────────────────────────────
echo "[5/6] Installing systemd service..."
sudo tee /etc/systemd/system/reyquant.service > /dev/null << 'SVCEOF'
[Unit]
Description=Rey Quant Telegram Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/reyquant
EnvironmentFile=/home/ubuntu/reyquant/.env
ExecStart=/usr/bin/python3 /home/ubuntu/reyquant/bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF

sudo systemctl daemon-reload
sudo systemctl enable reyquant
sudo systemctl start reyquant
echo "  Service installed and started ✅"

# ── 6. Done ─────────────────────────────────────────────────
echo ""
echo "============================================"
echo "  ⚡ Rey Quant Bot is RUNNING 24/7 ⚡"
echo "============================================"
echo ""
echo "Check status:  sudo systemctl status reyquant"
echo "View logs:     sudo journalctl -u reyquant -f"
echo "Restart:       sudo systemctl restart reyquant"
echo "Stop:          sudo systemctl stop reyquant"
echo ""
echo "Test: Send /plan to @reyquant_bot on Telegram"
