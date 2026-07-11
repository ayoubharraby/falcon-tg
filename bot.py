#!/usr/bin/env python3
"""
Falcon Telegram Bot v3

Navigate with buttons. No slash-command typing needed.

Flow:
  /start or 🏠 Home  →  Main Menu (inline buttons)
  → 🔍 Search         →  ask for term  →  mode selector buttons
  → 📊 Status          →  live status card with [Refresh] [Close]
  → 🖥️ RAM              →  live RAM card with [Refresh] [Close]
  → 📥 Results          →  list saved files with [Clean]
  → 📋 Queue            →  queue card with [Cancel Job]

Navigation messages auto-delete when user moves on (BotFather-style).
Progress updates at fastest safe interval: 1.5 s (Telegram allows ~30 edits/min per message).

Config: copy env.example to .env and fill in your values.
"""
__version__ = "3.0.0"

import os, re, time, queue, traceback, subprocess, threading, collections, json, shutil, io
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════
def _load_env():
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

_load_env()

def _require(key):
    v = os.environ.get(key, "").strip()
    if not v:
        raise SystemExit(f"[ERROR] '{key}' not set. Copy env.example → .env")
    return v

TOKEN            = _require("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_IDS = {int(x.strip()) for x in _require("ALLOWED_CHAT_IDS").split(",") if x.strip()}
SOURCE_DIR       = os.environ.get("SOURCE_DIR", "/data/textset")
OUT_DIR          = os.environ.get("OUT_DIR",    "/data/archives")
PYTHON_BIN       = os.environ.get("PYTHON_BIN", "python3")
FALCON_SCRIPT    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "falcon_parse.py")

API          = f"https://api.telegram.org/bot{TOKEN}"
TG_MAX_BYTES = 45 * 1024 * 1024

# Fastest safe edit interval — Telegram allows ~30 edits/min per message (2 s)
# We go 1.5 s to stay well under the per-message limit and never hit 429s.
EDIT_INTERVAL = 1.5

# ════════════════════════════════════════════════════════════
# HTTP SESSION
# ════════════════════════════════════════════════════════════
def _build_session():
    s = requests.Session()
    retry = Retry(
        total=3, backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=10)
    s.mount("https://", adapter)
    s.mount("http://",  adapter)
    return s

_SESSION = _build_session()

# ════════════════════════════════════════════════════════════
# STATE MACHINE  (per-chat, thread-safe)
# ════════════════════════════════════════════════════════════
# States
ST_IDLE          = "idle"
ST_AWAIT_TERM    = "await_term"   # waiting for search term text

_STATES     = {}   # chat_id -> state string
_STATE_LOCK = threading.Lock()

def get_state(chat_id):
    with _STATE_LOCK:
        return _STATES.get(chat_id, ST_IDLE)

def set_state(chat_id, state):
    with _STATE_LOCK:
        _STATES[chat_id] = state

# ════════════════════════════════════════════════════════════
# NAV MESSAGE TRACKER  (auto-delete on navigation)
# ════════════════════════════════════════════════════════════
# Stores the last "navigation" message id per chat.
# When the user opens a new nav screen, the old one is deleted first.
_NAV_MSG    = {}   # chat_id -> message_id
_NAV_LOCK   = threading.Lock()

def _store_nav(chat_id, msg_id):
    with _NAV_LOCK:
        _NAV_MSG[chat_id] = msg_id

def _pop_nav(chat_id):
    """Return and clear the stored nav message id, or None."""
    with _NAV_LOCK:
        return _NAV_MSG.pop(chat_id, None)

def _delete_nav(chat_id):
    """Delete the previous nav message for this chat, if any."""
    old = _pop_nav(chat_id)
    if old:
        delete_message(chat_id, old)

# ════════════════════════════════════════════════════════════
# JOB QUEUE
# ════════════════════════════════════════════════════════════
JOB_QUEUE    = queue.Queue()
QUEUE_LIST   = collections.deque()
QUEUE_LOCK   = threading.Lock()
CANCEL_EVENT = threading.Event()
RUNNING_JOB  = None
RUNNING_LOCK = threading.Lock()
UPDATE_LOCK  = threading.Lock()

PROGRESS_RE = re.compile(
    r'PROGRESS phase=(\d+)\s+(?:hits=(\d+)\s+ulp=(\d+)|combos=(\d+))\s+elapsed=([\d.]+)'
)
DONE_RE = re.compile(
    r'DONE hits=(\d+) ulp=(\d+) combos=(\d+) elapsed=([\d.]+)'
    r'(?:\s+ulp_bytes=(\d+))?(?:\s+combo_bytes=(\d+))?'
)
LAST_UPDATE_ID = 0

# ════════════════════════════════════════════════════════════
# DISK / RAM CACHE
# ════════════════════════════════════════════════════════════
_DF_CACHE = ("", 0.0)
_DF_LOCK  = threading.Lock()
_DF_TTL   = 8.0

def _get_disk_info():
    global _DF_CACHE
    with _DF_LOCK:
        out, ts = _DF_CACHE
        if time.time() - ts < _DF_TTL:
            return out
        try:
            lines = subprocess.check_output(["df", "-h", "/"], text=True).splitlines()
            out = lines[1] if len(lines) >= 2 else ""
        except Exception:
            out = ""
        _DF_CACHE = (out, time.time())
        return out

def _get_ram_lines():
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for ln in f:
                p = ln.split()
                if len(p) >= 2:
                    info[p[0].rstrip(":")] = int(p[1])
        total     = info.get("MemTotal", 0)
        avail     = info.get("MemAvailable", 0)
        used      = total - avail
        pct       = (used / total * 100) if total else 0
        swap_t    = info.get("SwapTotal", 0)
        swap_f    = info.get("SwapFree", 0)
        lines = [
            f"  Total     : {_fmt_bytes(total * 1024)}",
            f"  Used      : {_fmt_bytes(used  * 1024)} ({pct:.1f}%)",
            f"  Free      : {_fmt_bytes(avail * 1024)}",
        ]
        if swap_t:
            lines.append(f"  Swap      : {_fmt_bytes((swap_t-swap_f)*1024)} / {_fmt_bytes(swap_t*1024)}")
        return lines
    except Exception as e:
        return [f"  ⚠️ {e}"]

# ════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════
def _fmt_bytes(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"

def safe_term(t):
    return re.sub(r"[^\w\-\.]", "_", t)

# ════════════════════════════════════════════════════════════
# TELEGRAM API WRAPPERS
# ════════════════════════════════════════════════════════════
def api_post(method, data=None, files=None, timeout=120):
    return _SESSION.post(f"{API}/{method}", data=data, files=files, timeout=timeout)

def api_get(method, params=None, timeout=40):
    return _SESSION.get(f"{API}/{method}", params=params, timeout=timeout)

def answer_callback(callback_id, text=None):
    """Answer a callback query (removes the loading spinner instantly)."""
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    try:
        api_post("answerCallbackQuery", payload)
    except Exception:
        pass

def delete_message(chat_id, msg_id):
    try:
        api_post("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})
    except Exception:
        pass

def send_message(chat_id, text, reply_markup=None, parse_mode=None):
    data = {"chat_id": chat_id, "text": text}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    if parse_mode:
        data["parse_mode"] = parse_mode
    r = api_post("sendMessage", data)
    try:
        return r.json()["result"]["message_id"]
    except Exception:
        return None

def edit_message(chat_id, msg_id, text, reply_markup=None, parse_mode=None):
    data = {"chat_id": chat_id, "message_id": msg_id, "text": text}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    if parse_mode:
        data["parse_mode"] = parse_mode
    try:
        api_post("editMessageText", data)
    except Exception:
        pass

def edit_reply_markup(chat_id, msg_id, reply_markup):
    """Edit only the keyboard on an existing message (no text change)."""
    try:
        api_post("editMessageReplyMarkup", {
            "chat_id":      chat_id,
            "message_id":   msg_id,
            "reply_markup": json.dumps(reply_markup),
        })
    except Exception:
        pass

def send_document(chat_id, file_path, caption=None):
    try:
        with open(file_path, "rb") as f:
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption
            r = api_post("sendDocument", data=data,
                         files={"document": (Path(file_path).name, f)},
                         timeout=300)
        j = r.json()
        return j.get("ok"), j.get("description", "")
    except Exception as e:
        return False, str(e)

# ════════════════════════════════════════════════════════════
# KEYBOARD BUILDERS
# ════════════════════════════════════════════════════════════
def _kb(*rows):
    """Build an InlineKeyboardMarkup from rows of (text, callback_data) tuples."""
    return {"inline_keyboard": [
        [{"text": t, "callback_data": d} for t, d in row]
        for row in rows
    ]}

KB_MAIN = _kb(
    [("\U0001f50d Search", "nav:search"),  ("\U0001f4cb Queue", "nav:queue")],
    [("\U0001f5a5\ufe0f Status",  "nav:status"), ("\U0001f4be RAM",   "nav:ram")],
    [("\U0001f4e5 Results", "nav:results")],
)

KB_MODE = lambda term: _kb(
    [("\U0001f4c4 ULP  (full hits)",       f"run:ulp:{term}")],
    [("\U0001f511 COMBO (user:pass only)",  f"run:combo:{term}")],
    [("\U0001f519 Back",                     "nav:main")],
)

KB_BACK       = _kb([("\U0001f519 Back", "nav:main")])
KB_CLOSE      = _kb([("\u274c Close",    "nav:close")])
KB_REFRESH_BACK = _kb(
    [("\U0001f504 Refresh", "refresh:self"), ("\U0001f519 Back", "nav:main")],
)
KB_QUEUE = lambda has_job: _kb(
    *(([("\u23f9 Cancel Job", "do:cancel")],) if has_job else ()),
    [("\U0001f504 Refresh", "refresh:self"), ("\U0001f519 Back", "nav:main")],
)
KB_RESULTS_CLEAN = _kb(
    [("\U0001f9f9 Clean All", "do:clean"), ("\U0001f519 Back", "nav:main")],
)

# ════════════════════════════════════════════════════════════
# SCREEN BUILDERS
# ════════════════════════════════════════════════════════════
def _screen_main():
    with RUNNING_LOCK:
        job = RUNNING_JOB
    with QUEUE_LOCK:
        q = len(QUEUE_LIST)
    if job:
        lbl = "ULP" if job["mode"] == "ulp" else "COMBO"
        status_line = f"\U0001f7e2 Running: [{lbl}] {job['term']}"
    else:
        status_line = "\u26aa Idle"
    pending = f" · {q} queued" if q else ""
    text = (
        f"\U0001f985 Falcon Bot  v{__version__}\n"
        f"\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\n"
        f"{status_line}{pending}\n\n"
        f"Choose an action:"
    )
    return text, KB_MAIN

def _screen_status():
    out_path = Path(OUT_DIR)
    lines = ["\U0001f5a5\ufe0f  Server Status", ""]
    if out_path.exists():
        files = sorted(
            [f for f in out_path.iterdir() if f.is_file() and f.suffix == ".txt"],
            key=lambda f: f.stat().st_mtime, reverse=True
        )
        total = sum(f.stat().st_size for f in files)
        lines += [
            f"\U0001f4c2  Results   : {len(files)} files  ({_fmt_bytes(total)})",
        ]
        if files:
            lines.append(f"   Latest   : {files[0].name}")
    else:
        lines.append(f"\u26a0\ufe0f  {OUT_DIR} not found")
    df = _get_disk_info()
    if df:
        lines += ["", f"\U0001f4be  Disk (/)  : {df}"]
    with RUNNING_LOCK:
        job = RUNNING_JOB
    lines.append("")
    if job:
        lbl = "ULP" if job["mode"] == "ulp" else "COMBO"
        lines.append(f"\U0001f7e2  Running   : [{lbl}] {job['term']}")
    else:
        lines.append("\u26aa  Bot is idle")
    lines.append(f"\n\U0001f552  {time.strftime('%H:%M:%S')}")
    return "\n".join(lines), KB_REFRESH_BACK

def _screen_ram():
    lines = ["\U0001f4be  RAM Usage", ""] + _get_ram_lines()
    lines.append(f"\n\U0001f552  {time.strftime('%H:%M:%S')}")
    return "\n".join(lines), KB_REFRESH_BACK

def _screen_queue():
    with RUNNING_LOCK:
        job = RUNNING_JOB
    with QUEUE_LOCK:
        pending = list(QUEUE_LIST)
    lines = ["\U0001f4cb  Job Queue", ""]
    if job:
        lbl = "ULP" if job["mode"] == "ulp" else "COMBO"
        lines.append(f"\U0001f7e2  Running : [{lbl}] {job['term']}")
    else:
        lines.append("\u26aa  Idle — no job running")
    if pending:
        lines.append(f"\n\U0001f4cc  Pending ({len(pending)}):")
        for i, (_, t, m) in enumerate(pending, 1):
            lbl = "ULP" if m == "ulp" else "COMBO"
            lines.append(f"   {i}.  [{lbl}]  {t}")
    else:
        lines.append("\n\U0001f4cc  Queue is empty")
    return "\n".join(lines), KB_QUEUE(job is not None)

def _screen_results():
    out_path = Path(OUT_DIR)
    lines = ["\U0001f4e5  Saved Results", ""]
    if not out_path.exists():
        lines.append(f"\u26a0\ufe0f  {OUT_DIR} not found")
        return "\n".join(lines), KB_BACK
    files = sorted(
        [f for f in out_path.iterdir()
         if f.is_file() and f.suffix == ".txt"
         and (f.name.startswith("ULP_") or f.name.startswith("COMBO_LP_"))],
        key=lambda f: f.stat().st_mtime, reverse=True
    )
    if not files:
        lines.append("  No result files saved.")
        return "\n".join(lines), KB_BACK
    total = sum(f.stat().st_size for f in files)
    lines.append(f"  {len(files)} files  ·  {_fmt_bytes(total)} total\n")
    for f in files[:12]:
        lines.append(f"  \U0001f4c4  {f.name}  ({_fmt_bytes(f.stat().st_size)})")
    if len(files) > 12:
        lines.append(f"  ... and {len(files)-12} more")
    return "\n".join(lines), KB_RESULTS_CLEAN

# ════════════════════════════════════════════════════════════
# NAV ACTIONS  (send or edit-in-place, auto-delete old nav)
# ════════════════════════════════════════════════════════════
def _show_main(chat_id, edit_msg_id=None):
    text, kb = _screen_main()
    if edit_msg_id:
        edit_message(chat_id, edit_msg_id, text, reply_markup=kb)
        _store_nav(chat_id, edit_msg_id)
    else:
        _delete_nav(chat_id)
        mid = send_message(chat_id, text, reply_markup=kb)
        _store_nav(chat_id, mid)

def _show_screen(chat_id, screen_fn, edit_msg_id=None):
    text, kb = screen_fn()
    if edit_msg_id:
        edit_message(chat_id, edit_msg_id, text, reply_markup=kb)
        _store_nav(chat_id, edit_msg_id)
    else:
        _delete_nav(chat_id)
        mid = send_message(chat_id, text, reply_markup=kb)
        _store_nav(chat_id, mid)

def _show_search_prompt(chat_id, edit_msg_id=None):
    text = (
        "\U0001f50d  Search\n"
        "\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\n"
        "Type your search term and send it:\n"
        "(e.g.  netflix.com  or  @gmail.com)"
    )
    if edit_msg_id:
        edit_message(chat_id, edit_msg_id, text, reply_markup=KB_BACK)
        _store_nav(chat_id, edit_msg_id)
    else:
        _delete_nav(chat_id)
        mid = send_message(chat_id, text, reply_markup=KB_BACK)
        _store_nav(chat_id, mid)
    set_state(chat_id, ST_AWAIT_TERM)

def _show_mode_select(chat_id, term, edit_msg_id=None):
    text = (
        f"\U0001f50d  Term: {term}\n"
        f"\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\n"
        f"Select search mode:"
    )
    kb = KB_MODE(term)
    if edit_msg_id:
        edit_message(chat_id, edit_msg_id, text, reply_markup=kb)
        _store_nav(chat_id, edit_msg_id)
    else:
        _delete_nav(chat_id)
        mid = send_message(chat_id, text, reply_markup=kb)
        _store_nav(chat_id, mid)
    set_state(chat_id, ST_IDLE)

# ════════════════════════════════════════════════════════════
# FILE DELIVERY
# ════════════════════════════════════════════════════════════
def _split_and_send(chat_id, file_path, caption, msg_id):
    file_size   = os.path.getsize(file_path)
    stem        = Path(file_path).stem
    ext         = Path(file_path).suffix
    tmp_dir     = Path(file_path).parent
    total_parts = (file_size + TG_MAX_BYTES - 1) // TG_MAX_BYTES
    part_paths  = []

    edit_message(chat_id, msg_id,
        f"\u2702\ufe0f  File is {_fmt_bytes(file_size)} — splitting into {total_parts} parts...")
    try:
        with open(file_path, "rb") as src:
            for i in range(total_parts):
                pname = tmp_dir / f"{stem}.part{i+1}of{total_parts}{ext}"
                with open(pname, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=TG_MAX_BYTES)
                part_paths.append(pname)
    except Exception as e:
        edit_message(chat_id, msg_id, f"\u274c  Failed to split: {e}")
        return False

    all_ok = True
    for i, part in enumerate(part_paths, 1):
        edit_message(chat_id, msg_id,
            f"\u2b06\ufe0f  Uploading part {i}/{total_parts} ({_fmt_bytes(os.path.getsize(part))})...")
        ok, err = send_document(chat_id, str(part),
                                caption=f"{caption} — part {i}/{total_parts}")
        if not ok:
            edit_message(chat_id, msg_id,
                f"\u274c  Upload failed on part {i}/{total_parts}: {err}")
            all_ok = False
            break
    for p in part_paths:
        try:
            p.unlink()
        except Exception:
            pass
    return all_ok

def deliver_file(chat_id, file_path, label, term, msg_id):
    fsize   = os.path.getsize(file_path)
    caption = f"{label} — {term}"
    if fsize <= TG_MAX_BYTES:
        edit_message(chat_id, msg_id,
            f"\u2b06\ufe0f  Uploading ({_fmt_bytes(fsize)})...")
        ok, err = send_document(chat_id, file_path, caption=caption)
        if not ok:
            edit_message(chat_id, msg_id,
                f"\u274c  Upload failed: {err}\n{file_path}")
    else:
        ok = _split_and_send(chat_id, file_path, caption, msg_id)
        if not ok:
            edit_message(chat_id, msg_id,
                f"\u274c  Partial failure. File on server:\n{file_path}")

# ════════════════════════════════════════════════════════════
# FALCON WORKER
# ════════════════════════════════════════════════════════════
def run_falcon(chat_id, term, mode):
    global RUNNING_JOB

    st       = safe_term(term)
    label    = "ULP" if mode == "ulp" else "COMBO"
    out_file = os.path.join(OUT_DIR,
        f"ULP_{st}.txt" if mode == "ulp" else f"COMBO_LP_{st}.txt")

    with RUNNING_LOCK:
        RUNNING_JOB = {"chat_id": chat_id, "term": term, "mode": mode}
    CANCEL_EVENT.clear()

    # Progress message — separate from the nav message, never deleted
    msg_id = send_message(chat_id,
        f"\U0001f50e  [{label}]  {term}\n"
        f"\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\n"
        f"Starting...")
    if msg_id is None:
        with RUNNING_LOCK:
            RUNNING_JOB = None
        return

    # Add a live cancel button to the progress message
    edit_message(chat_id, msg_id,
        f"\U0001f50e  [{label}]  {term}\n"
        f"\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\n"
        f"Starting...",
        reply_markup=_kb([("\u23f9 Cancel", "do:cancel")]))

    cmd = [PYTHON_BIN, FALCON_SCRIPT,
           "--term", term, "--source", SOURCE_DIR,
           "--out", OUT_DIR, "--mode", mode]

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=1, text=True, errors="ignore")

    last_text  = ""
    last_edit  = 0.0
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
                    bar = _progress_bar(int(hits), int(hits) + 500_000)
                    text = (
                        f"\U0001f50e  [{label}]  {term}\n"
                        f"\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\n"
                        f"\U0001f7e1  Phase 1 — Scanning\n"
                        f"{bar}\n"
                        f"  Hits    : {int(hits):,}\n"
                        f"  Unique  : {int(ulp):,}\n"
                        f"  Elapsed : {elapsed}s"
                    )
                else:
                    combos = m.group(4)
                    text = (
                        f"\U0001f50e  [{label}]  {term}\n"
                        f"\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\n"
                        f"\U0001f7e2  Phase 2 — Extracting\n"
                        f"  Combos  : {int(combos):,}\n"
                        f"  Elapsed : {elapsed}s"
                    )

                # Fastest safe update: 1.5 s interval
                if now - last_edit >= EDIT_INTERVAL and text != last_text:
                    edit_message(chat_id, msg_id, text,
                                 reply_markup=_kb([("\u23f9 Cancel", "do:cancel")]))
                    last_edit = now
                    last_text = text

            elif d:
                hits, ulp, combos, elapsed = d.group(1), d.group(2), d.group(3), d.group(4)
                ulp_bytes   = int(d.group(5) or 0)
                combo_bytes = int(d.group(6) or 0)
                done_stats  = (hits, ulp, combos, elapsed, ulp_bytes, combo_bytes)
                edit_message(chat_id, msg_id,
                    f"\U0001f4ca  [{label}]  {term}\n"
                    f"\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\n"
                    f"  Hits    : {int(hits):,}\n"
                    f"  ULP     : {int(ulp):,}\n"
                    f"  Combos  : {int(combos):,}\n"
                    f"  Time    : {elapsed}s\n"
                    f"\u2b06\ufe0f  Preparing upload...")
        proc.wait()
    except Exception as e:
        edit_message(chat_id, msg_id, f"\u274c  Error: {e}")
        with RUNNING_LOCK:
            RUNNING_JOB = None
        return
    finally:
        with RUNNING_LOCK:
            RUNNING_JOB = None

    if cancelled:
        edit_message(chat_id, msg_id,
            f"\u26d4  Cancelled: {term}\n"
            f"\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015",
            reply_markup=_kb([("\U0001f3e0 Home", "nav:main")]))
        return

    if proc.returncode != 0:
        edit_message(chat_id, msg_id,
            f"\u274c  Falcon exited {proc.returncode}",
            reply_markup=_kb([("\U0001f3e0 Home", "nav:main")]))
        return

    if not os.path.exists(out_file) or os.path.getsize(out_file) == 0:
        edit_message(chat_id, msg_id,
            f"\u26a0\ufe0f  No results for: {term}",
            reply_markup=_kb([("\U0001f3e0 Home", "nav:main")]))
        return

    deliver_file(chat_id, out_file, label, term, msg_id)

    if done_stats:
        hits, ulp, combos, elapsed, ulp_bytes, combo_bytes = done_stats
        fsize       = os.path.getsize(out_file) if os.path.exists(out_file) else 0
        total_parts = max(1, (fsize + TG_MAX_BYTES - 1) // TG_MAX_BYTES)
        lines = [
            f"\u2705  Done — {term}",
            f"\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015",
            f"  Hits    : {int(hits):,}",
            f"  ULP     : {int(ulp):,}",
            f"  Combos  : {int(combos):,}",
            f"  Time    : {elapsed}s",
        ]
        if mode == "ulp" and ulp_bytes:
            lines.append(f"  File    : {_fmt_bytes(ulp_bytes)}")
        elif mode == "combo" and combo_bytes:
            lines.append(f"  File    : {_fmt_bytes(combo_bytes)}")
        if total_parts > 1:
            lines.append(f"  Parts   : {total_parts} × 45 MB")
        send_message(chat_id, "\n".join(lines),
                     reply_markup=_kb(
                         [("\U0001f50d Search Again", "nav:search"),
                          ("\U0001f3e0 Home",          "nav:main")]
                     ))

def _progress_bar(current, total, width=10):
    """Compact ASCII progress bar."""
    frac  = min(current / max(total, 1), 1.0)
    filled = int(frac * width)
    bar   = "\u2588" * filled + "\u2591" * (width - filled)
    return f"  [{bar}]  {int(frac*100)}%"

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
            print(f"[queue_worker] {traceback.format_exc()}")
        finally:
            JOB_QUEUE.task_done()

# ════════════════════════════════════════════════════════════
# ENQUEUE
# ════════════════════════════════════════════════════════════
def enqueue(chat_id, term, mode):
    with QUEUE_LOCK:
        pos = len(QUEUE_LIST) + 1
        QUEUE_LIST.append((chat_id, term, mode))
    JOB_QUEUE.put((chat_id, term, mode))
    with RUNNING_LOCK:
        busy = RUNNING_JOB is not None
    if busy:
        send_message(chat_id,
            f"\U0001f4cb  Queued at position {pos}\n"
            f"  [{('ULP' if mode=='ulp' else 'COMBO')}]  {term}\n"
            f"Starts when current job finishes.",
            reply_markup=_kb(
                [("\U0001f4cb Queue", "nav:queue"),
                 ("\u23f9 Cancel Job", "do:cancel")]
            ))

# ════════════════════════════════════════════════════════════
# CALLBACK QUERY HANDLER
# ════════════════════════════════════════════════════════════
def handle_callback(chat_id, msg_id, callback_id, data):
    answer_callback(callback_id)  # removes spinner immediately

    # ── navigation ──────────────────────────────
    if data == "nav:main":
        _show_main(chat_id, edit_msg_id=msg_id)

    elif data == "nav:search":
        _show_search_prompt(chat_id, edit_msg_id=msg_id)

    elif data == "nav:status":
        _show_screen(chat_id, _screen_status, edit_msg_id=msg_id)

    elif data == "nav:ram":
        _show_screen(chat_id, _screen_ram, edit_msg_id=msg_id)

    elif data == "nav:queue":
        _show_screen(chat_id, _screen_queue, edit_msg_id=msg_id)

    elif data == "nav:results":
        _show_screen(chat_id, _screen_results, edit_msg_id=msg_id)

    elif data == "nav:close":
        delete_message(chat_id, msg_id)
        _pop_nav(chat_id)

    # ── refresh (edit in place) ──────────────────────
    elif data == "refresh:self":
        # Determine which screen we are on by inspecting the current message text
        # We use the nav msg id already stored — just re-render the same screen.
        # We don’t track which screen, so we fall back to status (most useful to refresh).
        # A smarter approach tracks the last screen per chat:
        last = _NAV_MSG.get(chat_id)
        if last and last == msg_id:
            # Can’t easily know which screen; re-send status as safe default
            _show_screen(chat_id, _screen_status, edit_msg_id=msg_id)

    # ── mode selection ──────────────────────────
    elif data.startswith("run:"):
        _, mode, *term_parts = data.split(":")
        term = ":".join(term_parts)  # term may contain colons
        enqueue(chat_id, term, mode)
        # Nav message becomes the queue confirmation — dismiss mode selector
        delete_message(chat_id, msg_id)
        _pop_nav(chat_id)

    # ── actions ───────────────────────────────
    elif data == "do:cancel":
        with RUNNING_LOCK:
            job = RUNNING_JOB
        if job:
            CANCEL_EVENT.set()
            answer_callback(callback_id, text="\u23f9 Cancelling...")
        else:
            answer_callback(callback_id, text="\u2139\ufe0f Nothing running")

    elif data == "do:clean":
        out_path = Path(OUT_DIR)
        files = [f for f in out_path.iterdir()
                 if f.is_file() and f.suffix == ".txt"
                 and (f.name.startswith("ULP_") or f.name.startswith("COMBO_LP_"))]
        total  = sum(f.stat().st_size for f in files)
        deleted = 0
        for f in files:
            try:
                f.unlink(); deleted += 1
            except Exception:
                pass
        # Refresh the results screen in place
        text, kb = _screen_results()
        edit_message(chat_id, msg_id,
            f"\U0001f9f9  Cleaned {deleted} file(s) — freed {_fmt_bytes(total)}\n\n" + text,
            reply_markup=kb)

# ════════════════════════════════════════════════════════════
# MESSAGE HANDLER  (text input from user)
# ════════════════════════════════════════════════════════════
def handle_message(chat_id, text, msg_id):
    text = text.strip()
    state = get_state(chat_id)

    # ── always handle /start and /help ──
    if text in ("/start", "/help"):
        set_state(chat_id, ST_IDLE)
        _show_main(chat_id)
        return

    # ── legacy slash commands (still work) ──
    if text.startswith("/s "):
        t = text[3:].strip()
        if t:
            _show_mode_select(chat_id, t)
        return
    if text.startswith("/c "):
        t = text[3:].strip()
        if t:
            enqueue(chat_id, t, "combo")
        return
    if text == "/cancel":
        with RUNNING_LOCK:
            job = RUNNING_JOB
        if job:
            CANCEL_EVENT.set()
        return
    if text == "/queue":
        _show_screen(chat_id, _screen_queue)
        return
    if text == "/status":
        _show_screen(chat_id, _screen_status)
        return
    if text == "/ram":
        _show_screen(chat_id, _screen_ram)
        return
    if text == "/clean":
        # run clean and refresh
        out_path = Path(OUT_DIR)
        files    = [f for f in out_path.iterdir()
                    if f.is_file() and f.suffix == ".txt"
                    and (f.name.startswith("ULP_") or f.name.startswith("COMBO_LP_"))]
        total    = sum(f.stat().st_size for f in files)
        deleted  = 0
        for f in files:
            try:
                f.unlink(); deleted += 1
            except Exception:
                pass
        send_message(chat_id,
            f"\U0001f9f9  Cleaned {deleted} file(s) — freed {_fmt_bytes(total)}")
        return

    # ── state: waiting for search term ──
    if state == ST_AWAIT_TERM:
        # Delete the user’s typed message to keep the chat clean
        delete_message(chat_id, msg_id)
        # Show mode selector, editing the nav message in-place
        nav = _NAV_MSG.get(chat_id)
        _show_mode_select(chat_id, text, edit_msg_id=nav)
        return

    # ── fallback ──
    _show_main(chat_id)

# ════════════════════════════════════════════════════════════
# MAIN POLLING LOOP
# ════════════════════════════════════════════════════════════
def main():
    global LAST_UPDATE_ID

    threading.Thread(target=queue_worker, daemon=True).start()
    print(f"Falcon Bot v{__version__} started. (source={SOURCE_DIR}, out={OUT_DIR})")

    backoff = 2.0
    while True:
        try:
            r = api_get("getUpdates", params={
                "offset":          LAST_UPDATE_ID + 1,
                "timeout":         30,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            }).json()
            backoff = 2.0
        except Exception as e:
            print(f"getUpdates error: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        if r.get("ok"):
            for upd in r["result"]:
                with UPDATE_LOCK:
                    LAST_UPDATE_ID = upd["update_id"]

                # ── callback_query (button press) ──
                cq = upd.get("callback_query")
                if cq:
                    chat_id     = cq["message"]["chat"]["id"]
                    msg_id      = cq["message"]["message_id"]
                    callback_id = cq["id"]
                    data        = cq.get("data", "")
                    if chat_id in ALLOWED_CHAT_IDS:
                        handle_callback(chat_id, msg_id, callback_id, data)
                    else:
                        answer_callback(callback_id, text="\u26d4 Unauthorized")
                    continue

                # ── regular message ──
                msg     = upd.get("message", {})
                chat_id = msg.get("chat", {}).get("id")
                text    = msg.get("text", "")
                mid     = msg.get("message_id")
                if chat_id in ALLOWED_CHAT_IDS and text:
                    handle_message(chat_id, text, mid)

        time.sleep(0.3)

if __name__ == "__main__":
    main()
