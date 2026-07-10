#!/usr/bin/env bash
# update.sh — pull latest code and restart the bot.
#
# Usage (from anywhere on the server):
#   bash ~/falcon-tg/update.sh
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
# abort if there are uncommitted local changes that would block the pull
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "[WARN] You have local uncommitted changes. Stashing them first..."
    git stash
fi
if ! git pull origin main; then
    echo ""
    echo "[ERROR] git pull failed. Possible merge conflict."
    echo "        Run 'git status' to inspect, fix manually, then retry."
    exit 1
fi

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
