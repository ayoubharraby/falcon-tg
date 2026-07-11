# 🦅 Falcon Telegram Bot

A private Telegram bot for high-speed credential log searching and extraction. Navigate entirely through inline buttons — no slash-command memorization needed.

---

## Features

- **Button-driven UI** — full navigation via inline keyboards; `/start` is all you need
- **Fast credential search** — ripgrep (`rg`) with mmap + no-unicode mode for maximum throughput; pure-Python mmap fallback when `rg` is absent
- **Dual output modes** — ULP (full matched lines) or COMBO (clean `user:pass` pairs)
- **Job queue** — multiple searches queue automatically; live queue view with cancel support
- **Dynamic progress bar** — hybrid hits-driven (phase 1) + time-driven (phase 2) bar with adaptive update rate (0.4 s fast-start → 1.0 s steady); visible movement from the first second
- **Paginated archives** — browse, download, or wipe result files directly from the bot
- **Auto file splitting** — files over 45 MB are split into parts and uploaded sequentially
- **RAM & disk monitor** — live `/proc/meminfo` + `df` with 8 s cache
- **Junk filtering** — promo lines, mojibake, null values, URL-only lines stripped before dedup
- **GNU sort dedup** — 512 MB buffer + parallel sort handles arbitrarily large datasets
- **Parallel combo extraction** — multi-core `ProcessPoolExecutor` with balanced chunk sizes
- **Versioned backups** — each stable release is preserved as a `backup/vX.Y.Z-YYYY-MM-DD` branch before updates
- **Resilient HTTP session** — 3-retry adapter with backoff on 429/5xx; adaptive polling backoff on network errors

---

## Quick Start

```bash
git clone https://github.com/ayoubharraby/falcon-tg
cd falcon-tg
cp env.example .env          # fill in your tokens
bash setup.sh                # installs deps + registers systemd service
```

See **SERVER_DEPLOY.md** for full VPS deployment instructions.

---

## Bot Navigation

| Button | Action |
|---|---|
| 🔍 Search | Enter a term → choose ULP or COMBO mode |
| 📋 Queue | View running + pending jobs; cancel current |
| 🖥️ Status | Disk usage, archive count, running job |
| 💾 RAM | Live RAM + swap breakdown |
| 📦 Archives | Paginated file list — tap to download |

### Slash commands (power users)

| Command | Description |
|---|---|
| `/s <term>` | Open mode selector for term |
| `/c <term>` | Enqueue COMBO search directly |
| `/cancel` | Cancel current job |
| `/queue` | Show queue screen |
| `/status` | Show status screen |
| `/ram` | Show RAM screen |
| `/archives` | Show archives screen |
| `/clean` | Delete all archive files |

---

## Configuration (`.env`)

```env
TELEGRAM_BOT_TOKEN=your_token_here
ALLOWED_CHAT_IDS=123456789,987654321
SOURCE_DIR=/data/textset
OUT_DIR=/data/archives
PYTHON_BIN=python3
```

---

## Progress Bar — How It Works

The bar uses a **hybrid model** for smooth, data-driven feedback:

- **Phase 1 (Scanning):** 70 % weight on real hit count vs. estimated total hits (derived from rolling hit-rate), 30 % weight on elapsed time. Gives genuine movement as hits accumulate.
- **Phase 2 (Extracting):** Pure time-based against a 120 s calibration constant.
- **Adaptive update rate:** 0.4 s for the first 30 s (immediate feedback), then 1.0 s steady-state (safely under Telegram's 20 edits/10 s burst limit).
- Bar is capped at 99 % until `DONE` is received, then snaps to 100 %.

---

## Versioning & Backups

Every stable version is backed up as a branch before any major update:

```
backup/v3.1.1-2026-07-11   ← current stable snapshot
main                        ← latest (v3.2.0)
```

To restore a backup:
```bash
git fetch origin
git checkout backup/v3.1.1-2026-07-11
```

---

## File Structure

```
falcon-tg/
├── bot.py              # Telegram bot + job runner
├── falcon_parse.py     # Search engine (ripgrep / mmap fallback)
├── setup.sh            # Automated install + systemd setup
├── update.sh           # Pull + restart helper
├── tg-private-bot.service  # systemd unit file
├── env.example         # Config template
├── requirements.txt    # Python deps
└── SERVER_DEPLOY.md    # VPS deployment guide
```

---

## Version History

| Version | Date | Notes |
|---|---|---|
| v3.2.0 | 2026-07-11 | Dynamic hybrid progress bar; adaptive edit interval; full audit |
| v3.1.1 | 2026-07-11 | RAM fixes, sort-u dedup, /status, /clean; technical done msg |
| v3.0.0 | — | ripgrep mmap integration, job queue, archives pagination |
| v4.0.0 | — | falcon_parse: parallel combo extraction, GNU sort dedup |
