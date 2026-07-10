# Server Deploy Guide (Ubuntu)

Step-by-step manual setup. For a one-shot automated install, use `bash setup.sh` instead.

---

## 0. What this bot does

- `falcon_parse.py` scans large text datasets for a search term and extracts credentials.
- `bot.py` is a Telegram bot: accepts commands, queues jobs, runs Falcon in the background, streams live progress, and uploads result files.
- `tg-private-bot.service` keeps the bot always running as a systemd service.

---

## 1. Prerequisites

- Ubuntu server with SSH access
- A Telegram bot token — get one from [@BotFather](https://t.me/BotFather)
- Your Telegram user ID — get it from [@userinfobot](https://t.me/userinfobot)

---

## 2. SSH into the server

```bash
ssh youruser@your-server-ip
```

---

## 3. Clone the repo

```bash
cd ~
git clone https://github.com/ayoubharraby/falcon-tg.git
cd falcon-tg
```

---

## 4. Install system packages

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git ripgrep
```

---

## 5. Create data directories

```bash
sudo mkdir -p /data/textset /data/archives
sudo chown $USER:$USER /data/textset /data/archives
```

---

## 6. Python virtualenv

```bash
cd ~/falcon-tg
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

---

## 7. Configure `.env`

```bash
cp env.example .env
nano .env
```

Fill in:

```env
TELEGRAM_BOT_TOKEN=your-real-token
ALLOWED_CHAT_IDS=your-telegram-user-id
SOURCE_DIR=/data/textset
OUT_DIR=/data/archives
PYTHON_BIN=python3
```

Save: `Ctrl+O` → `Enter` → `Ctrl+X`

---

## 8. Quick manual test

```bash
source venv/bin/activate
python3 bot.py
```

Expected output:
```
Bot started. Polling... (source=/data/textset, out=/data/archives)
```

Open Telegram, send `/help` to your bot. You should get the command list back.
Kill with `Ctrl+C` when confirmed working.

---

## 9. Install the systemd service

```bash
sudo cp tg-private-bot.service /etc/systemd/system/tg-private-bot.service
sudo nano /etc/systemd/system/tg-private-bot.service
```

Replace every `%i` with your actual Linux username, and update the paths if your project directory is not `~/falcon-tg`:

```ini
[Service]
User=ayoub
WorkingDirectory=/home/ayoub/falcon-tg
ExecStart=/home/ayoub/falcon-tg/venv/bin/python /home/ayoub/falcon-tg/bot.py
EnvironmentFile=/home/ayoub/falcon-tg/.env
Restart=always
RestartSec=5
```

Save and exit.

---

## 10. Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable tg-private-bot
sudo systemctl start tg-private-bot
sudo systemctl status tg-private-bot
```

Expected: `Active: active (running)`

---

## 11. Live logs

```bash
sudo journalctl -u tg-private-bot -f
```

---

## 12. Updating the bot

Every time you push changes to GitHub, update your server with:

```bash
bash ~/falcon-tg/update.sh
```

This single command:
1. Runs `git pull origin main` — fetches and applies latest code
2. Re-runs `pip install -r requirements.txt` in the venv — picks up any new deps
3. Runs `sudo systemctl restart tg-private-bot` — gracefully kills the old process, starts fresh
4. Prints `systemctl status` + last 20 journal lines so you can confirm it started correctly

> **Note:** `update.sh` must be run as the same user that owns the project (e.g. `ayoub`). It calls `sudo systemctl restart` internally, so you may be prompted for your sudo password once.

---

## 13. Troubleshooting

| Symptom | Fix |
|---|---|
| `Active: failed` after start | Check `journalctl -u tg-private-bot -n 50` for the error |
| Bot does nothing in Telegram | Wrong `TELEGRAM_BOT_TOKEN` in `.env` — fix and restart |
| `Permission denied` on `/data/...` | Re-run `sudo chown $USER:$USER /data/textset /data/archives` |
| `No such file or directory` in service | Wrong path in `WorkingDirectory` or `ExecStart` |
| Deps missing after update | Run `source venv/bin/activate && pip install -r requirements.txt` manually |

---

## 14. Service quick-reference

```bash
# Update bot from GitHub
bash ~/falcon-tg/update.sh

# Restart manually
sudo systemctl restart tg-private-bot

# Stop
sudo systemctl stop tg-private-bot

# Live logs
sudo journalctl -u tg-private-bot -f

# Last 50 log lines
sudo journalctl -u tg-private-bot -n 50 --no-pager
```
