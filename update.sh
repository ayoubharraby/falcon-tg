#!/usr/bin/env bash
# update.sh — pull latest code and restart the bot service.
# Usage (from anywhere on the server):
#   bash ~/falcon-tg/update.sh
#
# What it does:
#   1. git pull latest changes from origin/main
#   2. activate venv and pip install any new/updated deps
#   3. systemctl restart the service
#   4. show live status + last 20 log lines
set -euo pipefail

SERVICE="tg-private-bot"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "===================================="
echo " Falcon-TG updater"
echo " Project : $PROJECT_DIR"
echo "===================================="
echo ""

cd "$PROJECT_DIR"

echo "[1/4] Pulling latest code..."
git pull origin main

echo ""
echo "[2/4] Installing / upgrading dependencies..."
# shellcheck disable=SC1091
source "$PROJECT_DIR/venv/bin/activate"
pip install --upgrade pip -q
pip install -r requirements.txt -q

echo ""
echo "[3/4] Restarting service ($SERVICE)..."
sudo systemctl restart "$SERVICE"

echo ""
echo "[4/4] Status:"
sudo systemctl status "$SERVICE" --no-pager -l

echo ""
echo "---- Last 20 log lines ----"
sudo journalctl -u "$SERVICE" -n 20 --no-pager
echo ""
echo "Done. Tail live logs with:"
echo "  sudo journalctl -u $SERVICE -f"
