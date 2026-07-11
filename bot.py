#!/usr/bin/env python3
"""
Falcon Telegram Bot

Commands:
  /s <term>    search — ULP mode  (full cleaned hits)
  /c <term>    search — COMBO mode (user:pass only)
  /cancel      cancel the running search (queued jobs stay)
  /queue       show the current job queue
  /status      show server disk usage + saved result files
  /ram         show server RAM usage
  /clean       delete all saved result files from OUT_DIR
  /help        show this help

Config: copy env.example to .env and fill in your values.
"""
__version__ = "2.0.0"

import os
import re
import time
import queue
import traceback
import subprocess
import threading
import collections
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ── load .env ────────────────────────────────────────────────────────────────
def _load_env():
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

_load_env()

def _require(key):
    val = os.environ.get(key, "").strip()
    if not val:
        raise SystemExit(
            f"[ERROR] Required env var '{key}' is not set.\n"
            "Copy env.example to .env and fill in your values."
        )
    return val

TOKEN            = _require("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_IDS = {int(x.strip()) for x in _require("ALLOWED_CHAT_IDS").split(",") if x.strip()}
SOURCE_DIR       = os.environ.get("SOURCE_DIR", "/data/textset")
OUT_DIR          = os.environ.get("OUT_DIR",    "/data/archives")
PYTHON_BIN       = os.environ.get("PYTHON_BIN", "python3")
FALCON_SCRIPT    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "falcon_parse.py")

API          = f"https://api.telegram.org/bot{TOKEN}"
TG_MAX_BYTES = 45 * 1024 * 1024

# ── persistent HTTP session with retry + connection pooling ──────────────────
def _build_session():
    """
    Reusing a session avoids a full TCP + TLS handshake on every API call.
    Retry adapter handles transient Telegram 5xx errors automatically.
    """
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
    s.mount("https://", adapter)
    s.mount("http://",  adapter)
    return s

_SESSION = _build_session()

# ── helpers ──────────────────────────────────────────────────────────────────
def _fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"

# ── job queue & cancel state ──────────────────────────────────────────────────
JOB_QUEUE    = queue.Queue()
QUEUE_LIST   = collections.deque()   # O(1) append/remove vs list O(n)
QUEUE_LOCK   = threading.Lock()
CANCEL_EVENT = threading.Event()
RUNNING_JOB  = None
RUNNING_LOCK = threading.Lock()
UPDATE_LOCK  = threading.Lock()      # protects LAST_UPDATE_ID

PROGRESS_RE = re.compile(
    r'PROGRESS phase=(\d+)\s+(?:hits=(\d+)\s+ulp=(\d+)|combos=(\d+))\s+elapsed=([\d.]+)'
)
DONE_RE = re.compile(
    r'DONE hits=(\d+) ulp=(\d+) combos=(\d+) elapsed=([\d.]+)'
    r'(?:\s+ulp_bytes=(\d+))?(?:\s+combo_bytes=(\d+))?'
)

LAST_UPDATE_ID = 0

# ── df cache: avoid subprocess on every /status call ─────────────────────────
_DF_CACHE      = ("", 0.0)   # (output, timestamp)
_DF_CACHE_TTL  = 10.0        # seconds
_DF_LOCK       = threading.Lock()

def _get_disk_info():
    global _DF_CACHE
    with _DF_LOCK:
        output, ts = _DF_CACHE
        if time.time() - ts < _DF_CACHE_TTL:
            return output
        try:
            lines = subprocess.check_output(["df", "-h", "/"], text=True).splitlines()
            output = lines[1] if len(lines) >= 2 else ""
        except Exception:
            output = ""
        _DF_CACHE = (output, time.time())
        return output

# ── telegram helpers ──────────────────────────────────────────────────────────
def api_post(method, data=None, files=None, timeout=120):
    return _SESSION.post(f"{API}/{method}", data=data, files=files, timeout=timeout)

def send_message(chat_id, text):
    r = api_post("sendMessage", {"chat_id": chat_id, "text": text})
    try:
        return r.json()["result"]["message_id"]
    except Exception:
        return None

def edit_message(chat_id, message_id, text):
    try:
        api_post("editMessageText", {
            "chat_id":    chat_id,
            "message_id": message_id,
            "text":       text,
        })
    except Exception:
        pass

def _send_one_document(chat_id, file_path, caption=None):
    try:
        with open(file_path, "rb") as f:
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption
            r = api_post("sendDocument", data=data,
                         files={"document": (Path(file_path).name, f)},
                         timeout=300)
        j = r.json()
        if j.get("ok"):
            return True, ""
        return False, j.get("description", "unknown error")
    except Exception as e:
        return False, str(e)

def _split_and_send(chat_id, file_path, caption, msg_id):
    file_size   = os.path.getsize(file_path)
    stem        = Path(file_path).stem
    ext         = Path(file_path).suffix
    tmp_dir     = Path(file_path).parent
    total_parts = (file_size + TG_MAX_BYTES - 1) // TG_MAX_BYTES
    part_paths  = []

    edit_message(chat_id, msg_id,
        f"\u2702\ufe0f File is {_fmt_bytes(file_size)} — splitting into {total_parts} parts...")

    try:
        with open(file_path, "rb") as src:
            for i in range(total_parts):
                part_name = tmp_dir / f"{stem}.part{i+1}of{total_parts}{ext}"
                with open(part_name, "wb") as dst:
                    # shutil.copyfileobj avoids a single giant read() into RAM
                    import shutil
                    shutil.copyfileobj(src, dst, length=TG_MAX_BYTES)
                part_paths.append(part_name)
    except Exception as e:
        edit_message(chat_id, msg_id, f"\u274c Failed to split file: {e}")
        return False

    all_ok = True
    for i, part in enumerate(part_paths, 1):
        edit_message(chat_id, msg_id,
            f"\u2b06\ufe0f Uploading part {i}/{total_parts} ({_fmt_bytes(os.path.getsize(part))})...")
        ok, err = _send_one_document(chat_id, str(part),
                                     caption=f"{caption} — part {i}/{total_parts}")
        if not ok:
            edit_message(chat_id, msg_id,
                f"\u274c Upload failed on part {i}/{total_parts}: {err}")
            all_ok = False
            break

    for part in part_paths:
        try:
            part.unlink()
        except Exception:
            pass

    return all_ok

def deliver_file(chat_id, file_path, label, term, msg_id):
    file_size = os.path.getsize(file_path)   # read once
    caption   = f"{label} results for: {term}"

    if file_size <= TG_MAX_BYTES:
        edit_message(chat_id, msg_id,
            f"\u2b06\ufe0f Uploading ({_fmt_bytes(file_size)})...")
        ok, err = _send_one_document(chat_id, file_path, caption=caption)
        if not ok:
            edit_message(chat_id, msg_id,
                f"\u274c Upload failed: {err}\nFile saved on server at:\n{file_path}")
    else:
        ok = _split_and_send(chat_id, file_path, caption, msg_id)
        if not ok:
            edit_message(chat_id, msg_id,
                f"\u274c Partial upload failure.\nFull file on server at:\n{file_path}")

def safe_term(term):
    return re.sub(r"[^\w\-\.]", "_", term)

# ── worker ────────────────────────────────────────────────────────────────────
def run_falcon(chat_id, term, mode):
    global RUNNING_JOB

    st       = safe_term(term)
    label    = "ULP (full hits)" if mode == "ulp" else "COMBO (user:pass)"
    out_file = os.path.join(OUT_DIR,
        f"ULP_{st}.txt" if mode == "ulp" else f"COMBO_LP_{st}.txt")

    with RUNNING_LOCK:
        RUNNING_JOB = {"chat_id": chat_id, "term": term, "mode": mode}
    CANCEL_EVENT.clear()

    msg_id = send_message(chat_id,
        f"\U0001f50e Searching '{term}' — {label}\nStarting...")
    if msg_id is None:
        with RUNNING_LOCK:
            RUNNING_JOB = None
        return

    cmd = [PYTHON_BIN, FALCON_SCRIPT,
           "--term", term, "--source", SOURCE_DIR,
           "--out", OUT_DIR, "--mode", mode]

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=1, text=True, errors="ignore")

    last_text  = ""
    last_edit  = 0
    cancelled  = False
    done_stats = None

    try:
        for line in proc.stdout:
            if CANCEL_EVENT.is_set():
                proc.kill()
                cancelled = True
                break

            line = line.strip()
            now  = time.time()
            m    = PROGRESS_RE.search(line)
            d    = DONE_RE.search(line)

            if m:
                phase   = m.group(1)
                elapsed = m.group(5)
                if phase == "1":
                    hits, ulp = m.group(2), m.group(3)
                    text = (f"\U0001f50e Searching '{term}' — {label}\n"
                            f"Phase 1 — scanning\n"
                            f"Raw hits : {int(hits):,}\n"
                            f"Unique   : {int(ulp):,}\n"
                            f"Elapsed  : {elapsed}s")
                else:
                    combos = m.group(4)
                    text = (f"\U0001f50e Searching '{term}' — {label}\n"
                            f"Phase 2 — extracting combos\n"
                            f"Combos so far : {int(combos):,}\n"
                            f"Elapsed       : {elapsed}s")
                if now - last_edit >= 2.0 and text != last_text:
                    edit_message(chat_id, msg_id, text)
                    last_edit = now
                    last_text = text

            elif d:
                hits, ulp, combos, elapsed = d.group(1), d.group(2), d.group(3), d.group(4)
                ulp_bytes   = int(d.group(5) or 0)
                combo_bytes = int(d.group(6) or 0)
                done_stats  = (hits, ulp, combos, elapsed, ulp_bytes, combo_bytes)
                text = (f"\U0001f4ca Done '{term}'\n"
                        f"Raw hits : {int(hits):,}\n"
                        f"ULP      : {int(ulp):,}\n"
                        f"Combos   : {int(combos):,}\n"
                        f"Time     : {elapsed}s\n"
                        f"Preparing upload...")
                edit_message(chat_id, msg_id, text)

        proc.wait()
    except Exception as e:
        edit_message(chat_id, msg_id, f"\u274c Error: {e}")
        with RUNNING_LOCK:
            RUNNING_JOB = None
        return
    finally:
        with RUNNING_LOCK:
            RUNNING_JOB = None

    if cancelled:
        edit_message(chat_id, msg_id, f"\u26d4 Search for '{term}' was cancelled.")
        return

    if proc.returncode != 0:
        edit_message(chat_id, msg_id,
            f"\u274c Falcon exited with code {proc.returncode}")
        return

    if not os.path.exists(out_file) or os.path.getsize(out_file) == 0:
        edit_message(chat_id, msg_id,
            f"\u26a0\ufe0f No results for '{term}' ({label}).")
        return

    deliver_file(chat_id, out_file, label, term, msg_id)

    if done_stats:
        hits, ulp, combos, elapsed, ulp_bytes, combo_bytes = done_stats
        file_size   = os.path.getsize(out_file) if os.path.exists(out_file) else 0
        total_parts = max(1, (file_size + TG_MAX_BYTES - 1) // TG_MAX_BYTES)
        summary_lines = [
            f"\u2705 Search complete for '{term}'",
            f"",
            f"\U0001f4cb Results",
            f"  Raw hits : {int(hits):,}",
            f"  ULP      : {int(ulp):,}",
            f"  Combos   : {int(combos):,}",
            f"  Time     : {elapsed}s",
        ]
        if mode == "ulp" and ulp_bytes:
            summary_lines.append(f"  File     : {_fmt_bytes(ulp_bytes)}")
        elif mode == "combo" and combo_bytes:
            summary_lines.append(f"  File     : {_fmt_bytes(combo_bytes)}")
        if total_parts > 1:
            summary_lines.append(f"  Parts    : {total_parts} × 45 MB")
        send_message(chat_id, "\n".join(summary_lines))


def queue_worker():
    while True:
        chat_id, term, mode = JOB_QUEUE.get()
        with QUEUE_LOCK:
            try:
                QUEUE_LIST.remove((chat_id, term, mode))
            except ValueError:
                pass
        try:
            run_falcon(chat_id, term, mode)
        except Exception:
            print(f"[queue_worker] unhandled error:\n{traceback.format_exc()}")
        finally:
            JOB_QUEUE.task_done()


# ── command handling ──────────────────────────────────────────────────────────
def enqueue(chat_id, term, mode):
    with QUEUE_LOCK:
        position = len(QUEUE_LIST) + 1
        QUEUE_LIST.append((chat_id, term, mode))
    JOB_QUEUE.put((chat_id, term, mode))

    with RUNNING_LOCK:
        busy = RUNNING_JOB is not None
    if busy:
        send_message(chat_id,
            f"\U0001f4cb Queued '{term}' — position {position}\n"
            f"Starts automatically when the current job finishes.\n"
            f"/cancel — cancel running job | /queue — see full list")
    else:
        send_message(chat_id, f"\U0001f50e Starting '{term}'...")


def cmd_cancel(chat_id):
    with RUNNING_LOCK:
        job = RUNNING_JOB
    if job is None:
        send_message(chat_id, "\u2139\ufe0f No search is running right now.")
        return
    CANCEL_EVENT.set()
    send_message(chat_id, f"\u23f9 Cancelling '{job['term']}' — please wait...")


def cmd_queue(chat_id):
    with RUNNING_LOCK:
        job = RUNNING_JOB
    with QUEUE_LOCK:
        pending = list(QUEUE_LIST)

    lines = []
    if job:
        lbl = "ULP" if job["mode"] == "ulp" else "COMBO"
        lines.append(f"\U0001f7e2 Running : [{lbl}] {job['term']}")
    else:
        lines.append("\u26aa Idle")

    if pending:
        lines.append(f"\U0001f4cb Queue ({len(pending)}):")
        for i, (_, t, m) in enumerate(pending, 1):
            lbl = "ULP" if m == "ulp" else "COMBO"
            lines.append(f"  {i}. [{lbl}] {t}")
    else:
        lines.append("\U0001f4cb Queue: empty")

    send_message(chat_id, "\n".join(lines))


def cmd_status(chat_id):
    lines = ["\U0001f5a5 Server Status", ""]

    out_path = Path(OUT_DIR)
    if out_path.exists():
        files = sorted(
            [f for f in out_path.iterdir() if f.is_file() and f.suffix == ".txt"],
            key=lambda f: f.stat().st_mtime, reverse=True
        )
        total_size = sum(f.stat().st_size for f in files)
        lines.append(f"\U0001f4c2 Result files in {OUT_DIR}")
        lines.append(f"  Count      : {len(files)}")
        lines.append(f"  Total size : {_fmt_bytes(total_size)}")
        if files:
            lines.append("")
            lines.append("Recent files:")
            for f in files[:8]:
                lines.append(f"  {f.name} ({_fmt_bytes(f.stat().st_size)})")
            if len(files) > 8:
                lines.append(f"  ... and {len(files) - 8} more")
    else:
        lines.append(f"\u26a0\ufe0f OUT_DIR not found: {OUT_DIR}")

    # cached disk usage
    df_line = _get_disk_info()
    if df_line:
        lines.append("")
        lines.append("\U0001f4be Disk usage (/)")
        lines.append(f"  {df_line}")

    lines.append("")
    with RUNNING_LOCK:
        job = RUNNING_JOB
    if job:
        lbl = "ULP" if job["mode"] == "ulp" else "COMBO"
        lines.append(f"\U0001f7e2 Running: [{lbl}] {job['term']}")
    else:
        lines.append("\u26aa Bot is idle")

    send_message(chat_id, "\n".join(lines))


def cmd_ram(chat_id):
    """Show RAM usage from /proc/meminfo (no psutil dependency)."""
    lines = ["\U0001f4be RAM Usage", ""]
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    info[parts[0].rstrip(":")] = int(parts[1])
        total     = info.get("MemTotal", 0)
        available = info.get("MemAvailable", 0)
        used      = total - available
        pct       = (used / total * 100) if total else 0
        lines.append(f"  Total     : {_fmt_bytes(total * 1024)}")
        lines.append(f"  Used      : {_fmt_bytes(used  * 1024)} ({pct:.1f}%)")
        lines.append(f"  Available : {_fmt_bytes(available * 1024)}")
        swap_total = info.get("SwapTotal", 0)
        swap_free  = info.get("SwapFree",  0)
        if swap_total:
            swap_used = swap_total - swap_free
            lines.append(f"  Swap used : {_fmt_bytes(swap_used * 1024)} / {_fmt_bytes(swap_total * 1024)}")
    except Exception as e:
        lines.append(f"\u26a0\ufe0f Could not read /proc/meminfo: {e}")
    send_message(chat_id, "\n".join(lines))


def cmd_clean(chat_id):
    out_path = Path(OUT_DIR)
    if not out_path.exists():
        send_message(chat_id, f"\u26a0\ufe0f OUT_DIR not found: {OUT_DIR}")
        return

    files = [f for f in out_path.iterdir()
             if f.is_file() and f.suffix == ".txt"
             and (f.name.startswith("ULP_") or f.name.startswith("COMBO_LP_"))]

    if not files:
        send_message(chat_id, "\U0001f9f9 No result files to clean.")
        return

    total_size = sum(f.stat().st_size for f in files)
    deleted = 0
    for f in files:
        try:
            f.unlink()
            deleted += 1
        except Exception:
            pass

    send_message(chat_id,
        f"\U0001f9f9 Cleaned {deleted} file(s) — freed {_fmt_bytes(total_size)}")


def handle_command(chat_id, text):
    text = text.strip()

    if text.startswith("/s "):
        term = text[3:].strip()
        if not term:
            send_message(chat_id, "Usage: /s <term>")
            return
        enqueue(chat_id, term, "ulp")

    elif text.startswith("/c "):
        term = text[3:].strip()
        if not term:
            send_message(chat_id, "Usage: /c <term>")
            return
        enqueue(chat_id, term, "combo")

    elif text == "/cancel":
        cmd_cancel(chat_id)

    elif text == "/queue":
        cmd_queue(chat_id)

    elif text == "/status":
        cmd_status(chat_id)

    elif text == "/ram":
        cmd_ram(chat_id)

    elif text == "/clean":
        cmd_clean(chat_id)

    elif text in ("/start", "/help"):
        send_message(chat_id,
            "\U0001f985 Falcon Bot\n"
            "\n"
            "/s <term>   — search, return ULP (full hits)\n"
            "/c <term>   — search, return COMBO (user:pass)\n"
            "/cancel     — cancel the currently running search\n"
            "/queue      — show running job + pending queue\n"
            "/status     — server disk usage + saved result files\n"
            "/ram        — server RAM usage\n"
            "/clean      — delete all saved result files\n"
            "/help       — this message")


# ── main polling loop ─────────────────────────────────────────────────────────
def main():
    global LAST_UPDATE_ID

    t = threading.Thread(target=queue_worker, daemon=True)
    t.start()

    print(f"Falcon Bot v{__version__} started. Polling... (source={SOURCE_DIR}, out={OUT_DIR})")
    backoff = 2.0
    while True:
        try:
            r = _SESSION.get(f"{API}/getUpdates", params={
                "offset":          LAST_UPDATE_ID + 1,
                "timeout":         30,
                "allowed_updates": '["message"]',
            }, timeout=40).json()
            backoff = 2.0  # reset on success
        except Exception as e:
            print(f"getUpdates error: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)  # exponential backoff, cap 60s
            continue

        if r.get("ok"):
            for upd in r["result"]:
                with UPDATE_LOCK:
                    LAST_UPDATE_ID = upd["update_id"]
                msg     = upd.get("message", {})
                chat_id = msg.get("chat", {}).get("id")
                text    = msg.get("text", "")
                if chat_id not in ALLOWED_CHAT_IDS:
                    continue
                if text:
                    handle_command(chat_id, text)

        time.sleep(0.5)


if __name__ == "__main__":
    main()
