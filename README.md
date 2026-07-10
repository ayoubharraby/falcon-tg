# falcon-tg

A private Telegram bot that runs **Falcon** (fast credential-extraction engine) on a local dataset and delivers results directly in Telegram — with live progress, a job queue, and cancel support.

---

## Commands

| Command | Description |
|---|---|
| `/s <term>` | Search and return full cleaned hits (ULP format) |
| `/c <term>` | Search and return `user:pass` combos only |
| `/cancel` | Cancel the currently running search |
| `/queue` | Show running job + pending queue |
| `/help` | Show command list |

---

## First-time Deploy (fresh server)

```bash
git clone https://github.com/ayoubharraby/falcon-tg.git
cd falcon-tg
bash setup.sh
```

`setup.sh` will:
1. Install system packages (`python3`, `ripgrep`, etc.)
2. Create `/data/textset` and `/data/archives`
3. Set up Python virtualenv + install deps
4. Ask for your **bot token** and **Telegram user ID**, write them to `.env`
5. Install and start the systemd service

---

## Updating the bot

Whenever you push new code to GitHub, update your server in **one command**:

```bash
bash ~/falcon-tg/update.sh
```

This will:
1. `git pull` latest code from `origin/main`
2. Re-install any new/updated dependencies
3. `systemctl restart` the service (graceful shutdown → fresh start)
4. Print the service status and last 20 log lines

---

## Manual Setup

See [SERVER_DEPLOY.md](SERVER_DEPLOY.md) for a step-by-step manual guide.

---

## Configuration (`.env`)

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

Place your `.txt` files inside `/data/textset/`.  
Falcon uses `ripgrep` if installed (much faster), otherwise falls back to pure-Python.

---

## Service Management

```bash
# Live logs
sudo journalctl -u tg-private-bot -f

# Restart after manual changes to bot.py or .env
sudo systemctl restart tg-private-bot

# Stop
sudo systemctl stop tg-private-bot

# Check status
sudo systemctl status tg-private-bot
```

---

## Queue Behavior

- Only **one search runs at a time**.
- Additional `/s` or `/c` commands are queued in order and run automatically when the current job finishes.
- `/cancel` kills only the **running** job; queued jobs remain and continue processing.
- `/queue` shows both the running job and the full pending list.

---

## Requirements

- Ubuntu / Debian Linux
- Python 3.8+
- `ripgrep` (optional but strongly recommended: `sudo apt install ripgrep`)
