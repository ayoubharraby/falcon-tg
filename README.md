# falcon-tg

A private Telegram bot that runs **Falcon** (a fast credential-extraction engine) on a large local dataset and sends you results directly in Telegram — with live progress updates.

---

## Features

- `/s <term>` — search and return full cleaned hits (ULP format)
- `/c <term>` — search and return `user:pass` combos only
- Live progress edited into a single message as Falcon runs
- Whitelisted chat IDs — only you can use it
- Runs as a systemd service (auto-restart on crash/reboot)
- Config loaded from `.env` — no secrets in code

---

## Quick Deploy (Linux / Ubuntu)

```bash
git clone https://github.com/ayoubharraby/falcon-tg.git
cd falcon-tg
bash setup.sh
```

`setup.sh` will:
1. Install system packages (`python3`, `ripgrep`, etc.)
2. Create `/data/textset` and `/data/archives`
3. Set up a Python virtualenv and install deps
4. Ask you for your **bot token** and **Telegram user ID**, save them to `.env`
5. Install and start the systemd service automatically

---

## Manual Setup

If you prefer to do it manually, see [SERVER_DEPLOY.md](SERVER_DEPLOY.md).

---

## Configuration

All config lives in `.env` (copied from `env.example`):

```env
TELEGRAM_BOT_TOKEN=your-token-here
ALLOWED_CHAT_IDS=your-telegram-user-id
SOURCE_DIR=/data/textset
OUT_DIR=/data/archives
PYTHON_BIN=python3
```

Get your bot token from [@BotFather](https://t.me/BotFather).  
Get your user ID by messaging [@userinfobot](https://t.me/userinfobot).

---

## Dataset

Place your text files inside `/data/textset/`.  
Falcon will use `ripgrep` (if installed) or fall back to pure-Python scanning.

---

## Service Management

```bash
# View live logs
sudo journalctl -u tg-private-bot -f

# Restart after code changes
sudo systemctl restart tg-private-bot

# Stop
sudo systemctl stop tg-private-bot
```

---

## Requirements

- Ubuntu / Debian Linux
- Python 3.8+
- `ripgrep` (optional but strongly recommended)
