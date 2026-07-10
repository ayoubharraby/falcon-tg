#!/usr/bin/env bash
# setup.sh — one-shot deployment script for falcon-tg
# Usage: bash setup.sh
# Run this from inside the cloned repo directory as your normal user.
set -euo pipefail

USER_NAME="$(whoami)"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="tg-private-bot"

echo "====================================="
echo " Falcon-TG setup"
echo " User    : $USER_NAME"
echo " Project : $PROJECT_DIR"
echo "====================================="
echo ""

# 1. system packages
echo "[1/7] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-pip python3-venv ripgrep

# 2. data directories
echo "[2/7] Creating data directories..."
sudo mkdir -p /data/textset /data/archives
sudo chown "$USER_NAME:$USER_NAME" /data/textset /data/archives

# 3. virtual environment
echo "[3/7] Setting up Python virtualenv..."
cd "$PROJECT_DIR"
python3 -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q

# 4. .env setup
echo "[4/7] Configuring .env..."
if [ ! -f "$PROJECT_DIR/.env" ]; then
    cp "$PROJECT_DIR/env.example" "$PROJECT_DIR/.env"
    echo ""
    echo "--------------------------------------------"
    echo " You need to fill in 2 required values:"
    echo "   TELEGRAM_BOT_TOKEN  — from @BotFather"
    echo "   ALLOWED_CHAT_IDS    — your Telegram user ID"
    echo "     (get it by messaging @userinfobot)"
    echo "--------------------------------------------"
    echo ""
    read -rp " Enter your Telegram bot token: " BOT_TOKEN
    read -rp " Enter your Telegram user ID:   " CHAT_ID
    # write them into .env
    sed -i "s|your-telegram-bot-token-here|$BOT_TOKEN|" "$PROJECT_DIR/.env"
    sed -i "s|your-telegram-user-id-here|$CHAT_ID|" "$PROJECT_DIR/.env"
    echo ""
    echo " .env saved."
else
    echo " .env already exists, skipping."
fi

# 5. systemd service
echo "[5/7] Installing systemd service..."
SVC_SRC="$PROJECT_DIR/tg-private-bot.service"
SVC_DST="/etc/systemd/system/${SERVICE_NAME}.service"

# replace %i placeholder in the service file with real username + path
tmp=$(mktemp)
sed "s|%i|$USER_NAME|g" "$SVC_SRC" \
  | sed "s|/home/$USER_NAME/falcon-tg|$PROJECT_DIR|g" > "$tmp"
sudo cp "$tmp" "$SVC_DST"
rm "$tmp"

# 6. enable and start
echo "[6/7] Enabling and starting service..."
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

# 7. status
echo "[7/7] Status:"
sudo systemctl status "$SERVICE_NAME" --no-pager -l

echo ""
echo "====================================="
echo " Done! Bot is running."
echo " Live logs: sudo journalctl -u $SERVICE_NAME -f"
echo "====================================="
