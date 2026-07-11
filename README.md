# falcon-tg

A private Telegram bot that runs **Falcon** — a fast credential-extraction engine — on a local dataset and delivers results directly in Telegram, with a **fully button-driven interface**, live progress, a job queue, cancel support, and smart large-file splitting.

---

## Navigation (BotFather-style)

The bot is fully navigated via **inline buttons**. You never need to type slash commands for normal use.

```
/start  →  Main Menu
```

```
💅 Falcon Bot  v3.0.0
―――――――――――――
⚪ Idle

Choose an action:

[ 🔍 Search ]   [ 📋 Queue  ]
[ 🖥️ Status ]   [ 💾 RAM    ]
[ 📥 Results  ]
```

### Search flow

1. Tap **🔍 Search** → bot asks for your term
2. Type your term (e.g. `netflix.com`) → your message is auto-deleted, the nav message updates in-place
3. **Mode selector** appears:

```
🔍  Term: netflix.com
―――――――――――――
Select search mode:

[ 📄 ULP  (full hits)       ]
[ 🔑 COMBO (user:pass only)  ]
[ 🔙 Back                    ]
```

4. Tap a mode → mode selector disappears, search starts

### Live progress

Progress message updates at **1.5 s intervals** (fastest safe rate without hitting Telegram rate limits). Includes a live **[Cancel]** button:

```
🔎  [ULP]  netflix.com
―――――――――――――
🟡  Phase 1 — Scanning
  [█████░░░░░]  50%
  Hits    : 642,150
  Unique  : 481,200
  Elapsed : 14.3s

[ ⏹ Cancel ]
```

### Done

```
✅  Done — netflix.com
―――――――――――――
  Hits    : 1,284,301
  ULP     : 948,200
  Combos  : 621,440
  Time    : 38.7s
  File    : 87.4 MB

[ 🔍 Search Again ]  [ 🏠 Home ]
```

---

## Slash Commands (still work)

| Command | Description |
|---|---|
| `/start` | Open main menu |
| `/s <term>` | Jump directly to mode selector for a term |
| `/c <term>` | Run COMBO search directly |
| `/cancel` | Cancel the currently running search |
| `/queue` | Show queue screen |
| `/status` | Show status screen |
| `/ram` | Show RAM screen |
| `/clean` | Delete all saved result files |

---

## Features

### GUI & Navigation

- **Fully inline-keyboard driven** — buttons for all actions, no typing needed
- **BotFather-style navigation** — nav messages edit in-place or auto-delete when you move on; the chat stays clean
- **Mode selector** — ULP vs COMBO chosen via buttons, not slash commands
- **Live [Cancel] button** on every progress message
- **[Search Again] + [Home]** buttons on completion
- **[Refresh] + [Back]** on Status, RAM, Queue screens
- **[Clean All]** on Results screen
- User-typed search term auto-deleted to keep chat clean

### Update Speed

- Progress messages update every **1.5 s** — fastest safe interval (Telegram allows ~30 edits/min per message; 1.5 s = 40/min, staying just under with retry tolerance)
- `getUpdates` polling every **0.3 s** — near-instant button response
- `answerCallbackQuery` called immediately on every button press — spinner disappears instantly

### Search Engine (falcon_parse.py)

- `ripgrep` with `--mmap` + `--no-unicode` + `-j <cpus>` for maximum throughput
- mmap-based pure-Python fallback when ripgrep is unavailable
- 4 MB write buffer, GNU sort with `--buffer-size=512M --parallel=<cpus>`
- Balanced phase-2 chunk size (10k–50k per worker)

### Bot Reliability

- Persistent `requests.Session` with connection pooling (`pool_maxsize=10`)
- Auto-retry on Telegram 5xx (3 attempts, backoff×0.4)
- Exponential backoff on polling errors (2s → 60s)
- `collections.deque` job queue (O(1) ops)
- Full traceback logging in `queue_worker`
- `UPDATE_LOCK` protecting `LAST_UPDATE_ID`
- df result cached for 8 s

---

## How Falcon Works

Two-phase pipeline:

**Phase 1 — Search**
- Uses `ripgrep` if installed (preferred) with `--mmap`, `--no-unicode`, and `-j <cpus>`
- Falls back to mmap-based `ProcessPoolExecutor` pure-Python grep
- Streams hits to disk — no RAM accumulation
- Deduplicates via `sort -u --buffer-size=512M --parallel=<cpus>`
- Output: `ULP_{term}.txt`

**Phase 2 — Combo extraction**
- Extracts clean `user:pass` pairs with multi-core `ProcessPoolExecutor`
- Deduplicates via `sort -u`
- Output: `COMBO_LP_{term}.txt`

> Every search **overwrites** any previous result for the same term.

---

## Queue Behavior

- Only **one search runs at a time**
- Additional searches are **queued in order**
- `/cancel` kills only the **running** job — queued jobs continue
- Queue screen shows running job + full pending list with a **[Cancel Job]** button

---

## Large File Handling

- Files **≤ 45 MB** → sent directly
- Files **> 45 MB** → auto-split into 45 MB chunks via `shutil.copyfileobj`
- If any part fails, you’re told exactly which part + file path on server

---

## First-time Deploy

```bash
git clone https://github.com/ayoubharraby/falcon-tg.git
cd falcon-tg
bash setup.sh
```

---

## Updating

```bash
bash ~/falcon-tg/update.sh
```

---

## Configuration (`.env`)

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
| `ALLOWED_CHAT_IDS` | ✅ | — | Comma-separated Telegram user IDs |
| `SOURCE_DIR` | — | `/data/textset` | Dataset directory or file |
| `OUT_DIR` | — | `/data/archives` | Result file output directory |
| `PYTHON_BIN` | — | `python3` | Python binary path |

---

## Service Management

```bash
sudo journalctl -u tg-private-bot -f
sudo systemctl restart tg-private-bot
sudo systemctl stop tg-private-bot
sudo systemctl status tg-private-bot
```

---

## Requirements

- Ubuntu / Debian Linux
- Python 3.8+
- `ripgrep` (strongly recommended: `sudo apt install ripgrep`)
- GNU `sort`
- `requests>=2.31.0`
- `urllib3>=1.26.0`
