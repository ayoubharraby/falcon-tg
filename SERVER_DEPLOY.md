# Server Deploy Guide (Ubuntu + UpCloud)

This document takes you from **zero** to a running, always‑on Falcon Telegram bot.

You do not need to know Linux or Python deeply. Just follow each step in order.

---

## 0. What this bot does

- Falcon (`falcon_parse.py`) scans large text files or folders for a search term and writes cleaned results.
- The Telegram bot (`bot.py`) listens for your commands in Telegram and calls Falcon in the background.
- The systemd service (`tg-private-bot.service`) keeps the bot always running and restarts it if it crashes or the server reboots.

---

## 1. Prerequisites

You need:

1. An Ubuntu server (e.g. UpCloud).
2. A way to log in via SSH.
3. A **Telegram bot token** from BotFather.
4. Your **Telegram user ID** (number).

### 1.1 Get your bot token

In the Telegram app:

1. Search for **BotFather**.
2. Send `/newbot` and follow instructions.
3. At the end, BotFather shows a token like:

   `1234567890:AA...something...`

Copy this token; you will paste it into `bot.py`.

### 1.2 Get your Telegram user ID

Simplest way:

1. Search in Telegram for `@userinfobot`.
2. Start it and send `/start`.
3. It replies with your numeric ID, e.g. `7003173372`.

Keep this number — only this user will be allowed to use the bot.

---

## 2. Upload the zip to the server

On your **own computer**, in the folder where `falcon-telegram-bot.zip` lives, run (replace `youruser` and `your-server-ip`):

```bash
scp falcon-telegram-bot.zip youruser@your-server-ip:~
```

This copies the zip into your home directory on the server.

---

## 3. Log into the server

From your computer:

```bash
ssh youruser@your-server-ip
```

If prompted about authenticity, type `yes`.

You should now see a shell prompt like:

```text
youruser@ubuntu-server:~$
```

---

## 4. Install system packages

Update the package list:

```bash
sudo apt update
```

Install Python and tools:

```bash
sudo apt install -y python3 python3-pip python3-venv unzip
```

Install **ripgrep** for faster Falcon searches (recommended):

```bash
sudo apt install -y ripgrep
```

All of this is one‑time setup.

---

## 5. Unpack the project

Still on the server:

```bash
cd ~
unzip falcon-telegram-bot.zip -d falcon-telegram-bot
cd falcon-telegram-bot
```

Check contents:

```bash
ls
```

You should see:

```text
falcon_parse.py  bot.py  requirements.txt  README.md  SERVER_DEPLOY.md  tg-private-bot.service
```

---

## 6. Create the data paths (important!)

Falcon reads from a **source directory** and writes into an **output directory**, which are configured in `bot.py` as `SOURCE_DIR` and `OUT_DIR`.

In our default setup, we use:

- `/data/textset` — where you place your input files.
- `/data/archives` — where Falcon writes result files.

### 6.1 Create `/data` and subfolders

Run:

```bash
sudo mkdir -p /data/textset /data/archives
```

Give ownership of these folders to your Linux user so the bot can read/write:

```bash
sudo chown youruser:youruser /data/textset /data/archives
```

Replace `youruser` with the username you used to log in.

Check:

```bash
ls -ld /data/textset /data/archives
```

You should see lines that include `youruser youruser` as the owner/group.

### 6.2 Put input files into `/data/textset`

Copy your big text dataset into `/data/textset`. Example:

```bash
cp /path/to/your/local/files/*.txt /data/textset/
```

If the files are elsewhere on the server, adjust the copy command accordingly. The important part: **Falcon must see files inside `/data/textset`**.

---

## 7. Create a Python virtual environment

This keeps dependencies clean and separate from system Python.

From inside the project folder:

```bash
cd ~/falcon-telegram-bot
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

This installs the `requests` library that `bot.py` uses.

You’ll know you’re in the virtualenv if your prompt shows `(venv)` at the start.

---

## 8. Configure the bot

Open `bot.py`:

```bash
cd ~/falcon-telegram-bot
nano bot.py
```

Near the top, you’ll see:

```python
TOKEN = "7721...something..."
ALLOWED_CHAT_IDS = {7003173372}
SOURCE_DIR = "/data/textset"
OUT_DIR = "/data/archives"
FALCON_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "falcon_parse.py")
PYTHON_BIN = "python3"
```

Change:

1. `TOKEN` → set to **your own** bot token from BotFather.
2. `ALLOWED_CHAT_IDS` → set to your own numeric user id (or add more, separated by commas).
3. `SOURCE_DIR` → keep `/data/textset` unless you changed the folder.
4. `OUT_DIR` → keep `/data/archives` unless you changed the folder.

Example:

```python
TOKEN = "1234567890:AAAbbbcccdddeeefff"  # your token
ALLOWED_CHAT_IDS = {7003173372}          # your Telegram user id
SOURCE_DIR = "/data/textset"
OUT_DIR = "/data/archives"
```

**Do not commit your real token to GitHub**; for GitHub you should later move the token into an environment variable or `.env` file. For this server guide, editing `bot.py` is enough for a private deployment.

Save and exit in nano:

- Press `Ctrl+O`, then `Enter` to write.
- Press `Ctrl+X` to exit.

---

## 9. Quick manual test

Before setting up auto‑restart, test the bot in the foreground.

From the project folder:

```bash
cd ~/falcon-telegram-bot
source venv/bin/activate   # if not already active
python3 bot.py
```

You should see:

```text
Bot started. Polling...
```

Now, in the **Telegram app**, open your bot and send:

```text
/help
```

You should get back:

```text
Commands:
/s -> full cleaned hits (ULP)
/c -> user:pass combos only (COMBO)
/cancel -> cancel current search
```

Test a search (assuming you have data in `/data/textset`):

```text
/s netflix.com
```

You should see progress messages and eventually get a `ULP_*` file as a document. Then `/c netflix.com` for combos.

Stop the bot with `Ctrl+C` in the server terminal.

If this works, you’re ready to make it a system service.

---

## 10. Install the systemd service (always‑on)

### 10.1 Copy the service file into `/etc/systemd/system`

From the project folder:

```bash
cd ~/falcon-telegram-bot
sudo cp tg-private-bot.service /etc/systemd/system/tg-private-bot.service
```

### 10.2 Edit the service file with correct paths

Open it:

```bash
sudo nano /etc/systemd/system/tg-private-bot.service
```

Change `User`, `WorkingDirectory`, and `ExecStart` to match **your** setup.

If you use the virtualenv:

```ini
[Service]
User=youruser
WorkingDirectory=/home/youruser/falcon-telegram-bot
ExecStart=/home/youruser/falcon-telegram-bot/venv/bin/python /home/youruser/falcon-telegram-bot/bot.py
Restart=always
RestartSec=5
```

If you prefer system Python:

```ini
[Service]
User=youruser
WorkingDirectory=/home/youruser/falcon-telegram-bot
ExecStart=/usr/bin/python3 /home/youruser/falcon-telegram-bot/bot.py
Restart=always
RestartSec=5
```

Save and exit (`Ctrl+O`, `Enter`, `Ctrl+X`).

### 10.3 Reload systemd and enable auto‑start

Reload configuration:

```bash
sudo systemctl daemon-reload
```

Enable auto‑start on boot:

```bash
sudo systemctl enable tg-private-bot.service
```

Start the service:

```bash
sudo systemctl start tg-private-bot.service
```

Check status:

```bash
sudo systemctl status tg-private-bot.service
```

You want to see:

```text
Active: active (running)
```

If you see errors, read the last lines shown there; fix the paths or config accordingly.

---

## 11. Logs and troubleshooting

To stream logs (live output):

```bash
sudo journalctl -u tg-private-bot.service -n 50 -f
```

Typical problems:

- **Wrong token** → bot does nothing; fix `TOKEN` in `bot.py`, then restart service.
- **Wrong user in service file** (`User=`) → permission errors; ensure it matches your login user.
- **Wrong paths** → systemd says "No such file or directory"; fix `WorkingDirectory` and `ExecStart` so they point to the real locations.

After any change to `bot.py` or `falcon_parse.py`:

```bash
cd ~/falcon-telegram-bot
sudo systemctl restart tg-private-bot.service
sudo systemctl status tg-private-bot.service
```

---

## 12. Stopping, restarting, and reboot behavior

Stop manually:

```bash
sudo systemctl stop tg-private-bot.service
```

Restart manually:

```bash
sudo systemctl restart tg-private-bot.service
```

On reboot, because you ran `enable` earlier, the service will start automatically and your bot will go online without you logging in.

---

## 13. GitHub deploy guide (high‑level)

For someone deploying via GitHub instead of a zip:

1. On your local machine, create a new GitHub repo (e.g. `falcon-telegram-bot`).
2. Commit these files:  
   - `falcon_parse.py`  
   - `bot.py`  
   - `requirements.txt`  
   - `README.md`  
   - `SERVER_DEPLOY.md`  
   - `tg-private-bot.service`  
   - `.env.example` (with placeholder values)
3. On the server, install `git`, clone the repo, and follow the same steps as above from the project directory:

   ```bash
   sudo apt update
   sudo apt install -y git
   cd ~
   git clone https://github.com/youruser/falcon-telegram-bot.git
   cd falcon-telegram-bot
   ```

4. Then repeat:
   - Create `/data/textset` and `/data/archives`
   - Set up venv + `pip install -r requirements.txt`
   - Edit `bot.py` for real token and paths
   - Install and enable `tg-private-bot.service`
