#!/usr/bin/env python3
"""
Private Telegram bot for Falcon searches.

Commands:
  /s <term>   -> run Falcon in ULP mode (cleaned raw hit lines), send ULP_<term>.txt
  /c <term>   -> run Falcon in COMBO mode (user:pass only), send COMBO_LP_<term>.txt

Shows LIVE progress by editing a single Telegram message while Falcon runs.

Config: edit the constants below before running.
"""
import os
import re
import time
import requests
import subprocess
import threading
from datetime import datetime

# ---------------- CONFIG ----------------
TOKEN = "YOUR_BOT_TOKEN"
ALLOWED_CHAT_IDS = {123456789}   # your Telegram user id(s) only
SOURCE_DIR = "/data/textset"     # the big dataset directory to search
OUT_DIR = "/data/archives"       # where Falcon writes result files
FALCON_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "falcon_parse.py")
PYTHON_BIN = "python3"
# -----------------------------------------

API = f"https://api.telegram.org/bot{TOKEN}"
LAST_UPDATE_ID = 0

PROGRESS_RE = re.compile(
    r'PROGRESS phase=(\d+)\s+(?:hits=(\d+)\s+ulp=(\d+)|combos=(\d+))\s+elapsed=([\d.]+)'
)
DONE_RE = re.compile(
    r'DONE hits=(\d+) ulp=(\d+) combos=(\d+) elapsed=([\d.]+)'
)


def api_post(method, data=None, files=None):
    return requests.post(f"{API}/{method}", data=data, files=files, timeout=60)


def send_message(chat_id, text):
    r = api_post("sendMessage", {"chat_id": chat_id, "text": text})
    try:
        return r.json()["result"]["message_id"]
    except Exception:
        return None


def edit_message(chat_id, message_id, text):
    try:
        api_post("editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        })
    except Exception:
        pass


def send_document(chat_id, file_path, caption=None):
    with open(file_path, "rb") as f:
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        api_post("sendDocument", data=data, files={"document": f})


def safe_term(term):
    return re.sub(r"[^\w\-\.]", "_", term)


def run_falcon(chat_id, term, mode):
    """Runs falcon_parse.py, streams progress into one live-edited Telegram message,
    then sends the resulting file."""
    st = safe_term(term)
    label = "ULP (full hits)" if mode == "ulp" else "COMBO (user:pass)"
    out_file = os.path.join(OUT_DIR, f"ULP_{st}.txt" if mode == "ulp" else f"COMBO_LP_{st}.txt")

    msg_id = send_message(chat_id, f"🔎 Searching '{term}' — mode: {label}\nStarting...")
    if msg_id is None:
        return

    cmd = [PYTHON_BIN, FALCON_SCRIPT, "--term", term, "--source", SOURCE_DIR,
           "--out", OUT_DIR, "--mode", mode]

    t0 = time.time()
    last_edit = 0
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             bufsize=1, text=True, errors="ignore")

    last_text = ""
    try:
        for line in proc.stdout:
            line = line.strip()
            now = time.time()

            m = PROGRESS_RE.search(line)
            d = DONE_RE.search(line)

            if m:
                phase = m.group(1)
                elapsed = m.group(5)
                if phase == "1":
                    hits, ulp = m.group(2), m.group(3)
                    text = (f"🔎 Searching '{term}' — {label}\n"
                            f"Phase 1: scanning\n"
                            f"Hits: {hits}\nUnique cleaned: {ulp}\n"
                            f"Elapsed: {elapsed}s")
                else:
                    combos = m.group(4)
                    text = (f"🔎 Searching '{term}' — {label}\n"
                            f"Phase 2: extracting combos\n"
                            f"Combos: {combos}\n"
                            f"Elapsed: {elapsed}s")
                if now - last_edit >= 2.0 and text != last_text:
                    edit_message(chat_id, msg_id, text)
                    last_edit = now
                    last_text = text
            elif d:
                hits, ulp, combos, elapsed = d.groups()
                text = (f"✅ Done searching '{term}'\n"
                        f"Raw hits: {hits}\nUnique ULP: {ulp}\nUnique combos: {combos}\n"
                        f"Total time: {elapsed}s\nUploading file...")
                edit_message(chat_id, msg_id, text)
        proc.wait()
    except Exception as e:
        edit_message(chat_id, msg_id, f"❌ Error while running search: {e}")
        return

    if proc.returncode != 0:
        edit_message(chat_id, msg_id, f"❌ Falcon exited with error code {proc.returncode}")
        return

    if not os.path.exists(out_file) or os.path.getsize(out_file) == 0:
        edit_message(chat_id, msg_id, f"⚠️ No results found for '{term}' ({label}).")
        return

    try:
        send_document(chat_id, out_file, caption=f"{label} results for: {term}")
        edit_message(chat_id, msg_id, f"✅ Done. Results for '{term}' sent below ⬇️")
    except Exception as e:
        edit_message(chat_id, msg_id, f"❌ Search finished but upload failed: {e}")


def handle_command(chat_id, text):
    text = text.strip()
    if text.startswith("/s "):
        term = text[3:].strip()
        if not term:
            send_message(chat_id, "Usage: /s website.com")
            return
        threading.Thread(target=run_falcon, args=(chat_id, term, "ulp"), daemon=True).start()
    elif text.startswith("/c "):
        term = text[3:].strip()
        if not term:
            send_message(chat_id, "Usage: /c website.com")
            return
        threading.Thread(target=run_falcon, args=(chat_id, term, "combo"), daemon=True).start()
    elif text.strip() in ("/start", "/help"):
        send_message(chat_id,
            "Commands:\n"
            "/s <term>  -> full cleaned hits (ULP)\n"
            "/c <term>  -> user:pass combos only (COMBO)")


def main():
    global LAST_UPDATE_ID
    print("Bot started. Polling...")
    while True:
        try:
            r = requests.get(f"{API}/getUpdates", params={
                "offset": LAST_UPDATE_ID + 1,
                "timeout": 30,
                "allowed_updates": '["message"]'
            }, timeout=40).json()
        except Exception as e:
            print(f"getUpdates error: {e}")
            time.sleep(2)
            continue

        if r.get("ok"):
            for upd in r["result"]:
                LAST_UPDATE_ID = upd["update_id"]
                msg = upd.get("message", {})
                chat_id = msg.get("chat", {}).get("id")
                text = msg.get("text", "")

                if chat_id not in ALLOWED_CHAT_IDS:
                    continue
                if text:
                    handle_command(chat_id, text)

        time.sleep(0.5)


if __name__ == "__main__":
    main()
