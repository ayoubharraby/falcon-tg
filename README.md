# Falcon Telegram Bot

This package contains the supporting files needed to deploy and maintain the Falcon Telegram bot project.

## What the project is

The project has two main parts:

1. **Falcon** (`falcon_parse.py`) — the Python parser/processing engine that searches source data and writes cleaned result files.
2. **Telegram bot** (`bot.py`) — the wrapper that receives Telegram commands, runs Falcon, and sends result files back through Telegram.

In short:

- Falcon does the real processing work.
- The bot is the interface layer.
- The systemd service keeps the bot always running.

## Included support files

This handoff package is meant to sit alongside your existing code files:

- `requirements.txt`
- `README.md`
- `SERVER_DEPLOY.md`
- `tg-private-bot.service`
- `.env.example`

Your actual application code files are:

- `falcon_parse.py`
- `bot.py`

If those two files already exist on the server and are the correct latest versions, you do **not** need new copies just to deploy or re-run the bot.

## When you do need the Python files again

You should include fresh copies of `falcon_parse.py` and `bot.py` when:

- you are handing the project to another person,
- you are moving it to another server,
- you want a complete archive backup,
- or the server copy may no longer be trusted as the latest version.

If the current server already has the correct working versions, then the support files in this package are enough for documentation and deployment.

## Python dependency

Install dependencies with:

```bash
pip install -r requirements.txt
```

At the moment, the bot requires:

- `requests` — used by `bot.py` to communicate with the Telegram Bot API.

## Recommended full project layout

```text
falcon-telegram-bot/
  falcon_parse.py
  bot.py
  requirements.txt
  README.md
  SERVER_DEPLOY.md
  tg-private-bot.service
  .env.example
```

## Run locally (basic)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 bot.py
```

## Server deployment

Use the step-by-step file:

- `SERVER_DEPLOY.md`

It explains:

- path creation,
- Python setup,
- data directory setup,
- bot configuration,
- systemd service install,
- auto-restart and reboot persistence.

## systemd service

The included service file is:

- `tg-private-bot.service`

After copying it to `/etc/systemd/system/`, you can enable and start it with:

```bash
sudo systemctl daemon-reload
sudo systemctl enable tg-private-bot.service
sudo systemctl start tg-private-bot.service
sudo systemctl status tg-private-bot.service
```

## Environment example

The file `.env.example` is included only as a template. It shows what values should exist if you later move configuration out of `bot.py`.
