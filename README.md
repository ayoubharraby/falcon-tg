# falcon-tg

A private Telegram bot that runs **Falcon** — a fast credential-extraction engine — on a local dataset and delivers results directly in Telegram, with live progress, a job queue, cancel support, and smart large-file splitting.

---

## Commands

| Command | Description |
|---|---|
| `/s <term>` | Search and return full cleaned hits **(ULP format** — full `url:user:pass` lines) |
| `/c <term>` | Search and return clean `user:pass` combos only |
| `/cancel` | Cancel the currently **running** search (queued jobs are unaffected) |
| `/queue` | Show the running job + full pending queue |
| `/status` | Show server disk usage and saved result files |
| `/clean` | Delete all saved result files from `OUT_DIR` |
| `/help` | Show command list |

### What you get after a search

While searching, the bot edits a single message in real time:

```
🔎 Searching 'netflix.com' — ULP (full hits)
Phase 1 — scanning
Raw hits : 1,284,301
Unique   : 948,200
Elapsed  : 14.3s
```

Once done, you receive a **technical summary** message:

```
✅ Search complete for 'netflix.com'

📋 Results
  Raw hits : 1,284,301
  ULP      : 948,200
  Combos   : 621,440
  Time     : 38.7s
  File     : 87.4 MB
```

For files **over 45 MB**, the bot automatically splits them and sends each part:

```
✂️ File is 210.3 MB — splitting into 5 parts...
⬆️ Uploading part 1/5 (45.0 MB)...
⬆️ Uploading part 2/5 (45.0 MB)...
...
✅ Done. 'netflix.com' results sent in 5 parts ⬇️
Total size: 210.3 MB
```

---

## How Falcon Works

Two-phase pipeline:

**Phase 1 — Search**
- Uses `ripgrep` if installed (preferred, much faster than pure Python)
- Falls back to `ProcessPoolExecutor` pure-Python grep if `ripgrep` not available
- Streams hits directly to a temp file on disk — **no RAM accumulation**
- Deduplicates via `sort -u` (disk-based — handles any file size without RAM pressure)
- Output: `ULP_{term}.txt`

**Phase 2 — Combo extraction**
- Reads Phase 1 output, strips URL schemes / bracket prefixes / promo tails / mojibake
- Extracts clean `user:pass` pairs using a multi-core `ProcessPoolExecutor`
- Deduplicates again via `sort -u`
- Output: `COMBO_LP_{term}.txt`

> Every search **overwrites** any previous result file for the same term. No stale data.

---

## Queue Behavior

- Only **one search runs at a time**
- Additional `/s` or `/c` commands are **queued in order** and run automatically when the current job finishes
- `/cancel` kills only the **running** job — queued jobs continue unaffected
- `/queue` shows both the running job and the full pending list

---

## Large File Handling

Telegram bots have a **50 MB upload limit**. Falcon-TG handles this transparently:

- Files **≤ 45 MB** → sent directly as a single document
- Files **> 45 MB** → automatically split into 45 MB chunks and sent as numbered parts (e.g. `ULP_netflix.com.part1of5.txt`)
- If any part fails, you're told exactly which part and the full file path on the server

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
3. Set up Python virtualenv and install deps
4. Ask for your **bot token** and **Telegram user ID**, write them to `.env`
5. Install and start the systemd service

---

## Updating the Bot

Every time you push new code to GitHub, update your server with **one command**:

```bash
bash ~/falcon-tg/update.sh
```

This will:
1. Stash any local changes if needed, then `git pull origin main`
2. Re-install any new/updated Python dependencies
3. `systemctl restart` the service (graceful shutdown → fresh start)
4. Print the service status and last 20 log lines

If `git pull` fails (e.g. a merge conflict), the script stops with a clear error message — it will **never restart the bot with stale code**.

---

## Configuration (`.env`)

All config lives in `.env` (copy from `env.example`):

```env
TELEGRAM_BOT_TOKEN=your-token-here
ALLOWED_CHAT_IDS=your-telegram-user-id
SOURCE_DIR=/data/textset
OUT_DIR=/data/archives
PYTHON_BIN=python3
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | From [@BotFather](https://t.me/BotFather) |
| `ALLOWED_CHAT_IDS` | ✅ | — | Comma-separated Telegram user IDs. Get yours from [@userinfobot](https://t.me/userinfobot) |
| `SOURCE_DIR` | — | `/data/textset` | Directory (or file) containing your dataset |
| `OUT_DIR` | — | `/data/archives` | Where result files are saved |
| `PYTHON_BIN` | — | `python3` | Python binary to use (e.g. path to venv python) |

---

## Dataset

Place your `.txt` files inside `SOURCE_DIR` (default: `/data/textset/`).  
Subdirectories are supported — Falcon scans recursively.  
`ripgrep` is strongly recommended: `sudo apt install ripgrep`

---

## Service Management

```bash
# Live logs
sudo journalctl -u tg-private-bot -f

# Restart after manual changes to bot.py or .env
sudo systemctl restart tg-private-bot

# Stop / Start
sudo systemctl stop tg-private-bot
sudo systemctl start tg-private-bot

# Check status
sudo systemctl status tg-private-bot
```

---

## Requirements

- Ubuntu / Debian Linux
- Python 3.8+
- `ripgrep` (optional but strongly recommended: `sudo apt install ripgrep`)
- GNU `sort` (pre-installed on all Linux distros)
- `requests>=2.31.0`
