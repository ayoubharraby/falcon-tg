#!/usr/bin/env python3
"""
Falcon Telegram Bot v3.3.0

Changes in v3.3.0:
  - INSTANT CANCEL: proc.kill() fires immediately from a dedicated watcher
    thread — no longer waits for the next stdout line.
  - FAST STARTUP: ProcessPoolExecutor pre-warm removed from falcon_parse
    startup path; bot sends "Starting…" ACK in <100 ms.
  - INLINE SEARCH (dynamic): bot_commands and inline_query support;
    typing a term shows a live preview card before confirming.
  - RICHER GUI: search prompt shows recent history chips; job card has
    a pulsing phase indicator; archives show file-type badges.
  - CANCEL FEEDBACK: dedicated cancel-watcher thread updates the job
    message to \"⏹ Cancelling…\" within 0.5 s, not after the next stdout.
  - HEARTBEAT: idle poll every 25 s keeps connection fresh.

Flow:
  /start  → Main Menu (inline buttons)
  → 🔍 Search   → inline query (type in search box) OR plain text
  → 📊 Status   → [Refresh] [Back]
  → 🖥️ RAM      → [Refresh] [Back]
  → 📥 Archives → paginated file list → tap file → sends it
  → 📋 Queue    → [Cancel Job] [Refresh] [Back]

Config: copy env.example → .env
"""
__version__ = "3.3.0"

import os, re, time, queue, traceback, subprocess, threading, collections, json, shutil
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
# TG_MAX_BYTES: maximum bytes per Telegram document part.
# Override via TG_MAX_BYTES env-var (bytes). Default: 45 MB.
TG_MAX_BYTES = int(os.environ.get("TG_MAX_BYTES", str(45 * 1024 * 1024)))

# ── Progress bar calibration ──────────────────────────────────
FULL_RUN_SECONDS  = 731.0
PHASE2_SECONDS    = 120.0

# ── Adaptive edit intervals ───────────────────────────────────
EDIT_INTERVAL_FAST = 0.4
EDIT_INTERVAL_NORM = 1.0
EDIT_FAST_WINDOW   = 30.0

# ── Archives pagination ───────────────────────────────────────
ARCHIVES_PAGE_SIZE = 8

# ── Recent-search history (per chat, in-memory) ───────────────
_SEARCH_HISTORY   = {}   # chat_id -> deque(maxlen=5)
_HISTORY_LOCK     = threading.Lock()
HISTORY_MAXLEN    = 5

def _add_history(chat_id, term):
    with _HISTORY_LOCK:
        if chat_id not in _SEARCH_HISTORY:
            _SEARCH_HISTORY[chat_id] = collections.deque(maxlen=HISTORY_MAXLEN)
        dq = _SEARCH_HISTORY[chat_id]
        if term in dq:
            dq.remove(term)
        dq.appendleft(term)

def _get_history(chat_id):
    with _HISTORY_LOCK:
        return list(_SEARCH_HISTORY.get(chat_id, []))

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
ST_IDLE       = "idle"
ST_AWAIT_TERM = "await_term"

_STATES     = {}
_STATE_LOCK = threading.Lock()

def get_state(chat_id):
    with _STATE_LOCK:
        return _STATES.get(chat_id, ST_IDLE)

def set_state(chat_id, state):
    with _STATE_LOCK:
        _STATES[chat_id] = state

# ════════════════════════════════════════════════════════════
# NAV MESSAGE TRACKER
# ════════════════════════════════════════════════════════════
_NAV_MSG  = {}  # chat_id -> message_id
_NAV_LOCK = threading.Lock()

_LAST_SCREEN = {}
_SCREEN_LOCK = threading.Lock()

def _set_last_screen(chat_id, name):
    with _SCREEN_LOCK:
        _LAST_SCREEN[chat_id] = name

def _get_last_screen(chat_id):
    with _SCREEN_LOCK:
        return _LAST_SCREEN.get(chat_id, "status")

def _store_nav(chat_id, msg_id):
    with _NAV_LOCK:
        _NAV_MSG[chat_id] = msg_id

def _pop_nav(chat_id):
    with _NAV_LOCK:
        return _NAV_MSG.pop(chat_id, None)

def _delete_nav(chat_id):
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

# ── Live process handle for instant kill ──────────────────────
_RUNNING_PROC      = None
_RUNNING_PROC_LOCK = threading.Lock()

def _set_running_proc(proc):
    with _RUNNING_PROC_LOCK:
        global _RUNNING_PROC
        _RUNNING_PROC = proc

def _clear_running_proc():
    with _RUNNING_PROC_LOCK:
        global _RUNNING_PROC
        _RUNNING_PROC = None

def _kill_running_proc():
    """Kill the subprocess immediately. Called from the cancel-watcher thread."""
    with _RUNNING_PROC_LOCK:
        proc = _RUNNING_PROC
    if proc and proc.poll() is None:
        try:
            proc.kill()
        except Exception:
            pass

PROGRESS_RE = re.compile(
    r'PROGRESS phase=(\d+)\s+(?:hits=(\d+)\s+ulp=(\d+)|combos=(\d+))\s+elapsed=([\d.]+)'
)
DONE_RE = re.compile(
    r'DONE hits=(\d+) ulp=(\d+) combos=(\d+) elapsed=([\d.]+)'
    r'(?:\s+ulp_bytes=(\d+))?(?:\s+combo_bytes=(\d+))?'
)
LAST_UPDATE_ID = 0

# ════════════════════════════════════════════════════════════
# DISK / RAM HELPERS
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
        total  = info.get("MemTotal", 0)
        avail  = info.get("MemAvailable", 0)
        used   = total - avail
        pct    = (used / total * 100) if total else 0
        swap_t = info.get("SwapTotal", 0)
        swap_f = info.get("SwapFree",  0)
        lines  = [
            f"  Total     : {_fmt_bytes(total * 1024)}",
            f"  Used      : {_fmt_bytes(used  * 1024)} ({pct:.1f}%)",
            f"  Free      : {_fmt_bytes(avail * 1024)}",
        ]
        if swap_t:
            lines.append(f"  Swap      : {_fmt_bytes((swap_t-swap_f)*1024)} / {_fmt_bytes(swap_t*1024)}")
        return lines
    except Exception as e:
        return [f"  ⚠️ {e}"]

def _fmt_bytes(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"

def safe_term(t):
    return re.sub(r"[^\w\-\.]", "_", t)

def _list_archive_files():
    """Return list of result .txt files sorted newest-first."""
    out_path = Path(OUT_DIR)
    if not out_path.exists():
        return []
    return sorted(
        [f for f in out_path.iterdir()
         if f.is_file() and f.suffix == ".txt"
         and (f.name.startswith("ULP_") or f.name.startswith("COMBO_LP_"))],
        key=lambda f: f.stat().st_mtime, reverse=True
    )

# ════════════════════════════════════════════════════════════
# PROGRESS BAR  (hybrid: hits-driven phase 1, time-driven phase 2)
# ════════════════════════════════════════════════════════════
def _progress_bar(elapsed_s, width=14, done=False, phase=1,
                  hits=0, expected_hits=0, phase2_elapsed=0.0):
    if done:
        frac = 1.0
    elif phase == 2:
        frac = min(phase2_elapsed / PHASE2_SECONDS, 0.99)
    else:
        time_frac = min(elapsed_s / FULL_RUN_SECONDS, 0.99)
        if expected_hits > 0 and hits > 0:
            hit_frac = min(hits / expected_hits, 0.99)
            frac = 0.70 * hit_frac + 0.30 * time_frac
        else:
            frac = time_frac
        frac = min(frac, 0.99)

    filled = int(frac * width)
    bar    = "█" * filled + "░" * (width - filled)
    pct    = int(frac * 100)
    return f"  [{bar}]  {pct}%"

# ════════════════════════════════════════════════════════════
# TELEGRAM API WRAPPERS
# ════════════════════════════════════════════════════════════
def api_post(method, data=None, files=None, timeout=120):
    return _SESSION.post(f"{API}/{method}", data=data, files=files, timeout=timeout)

def api_get(method, params=None, timeout=40):
    return _SESSION.get(f"{API}/{method}", params=params, timeout=timeout)

def answer_callback(callback_id, text=None):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    try:
        api_post("answerCallbackQuery", payload)
    except Exception:
        pass

def answer_inline(inline_query_id, results, cache_time=5):
    try:
        api_post("answerInlineQuery", {
            "inline_query_id": inline_query_id,
            "results": json.dumps(results),
            "cache_time": cache_time,
            "is_personal": True,
        })
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
    return {"inline_keyboard": [
        [{"text": t, "callback_data": d} for t, d in row]
        for row in rows
    ]}

KB_MAIN = _kb(
    [("🔍 Search",   "nav:search"),  ("📋 Queue",   "nav:queue")],
    [("🖥️ Status",  "nav:status"), ("💾 RAM",     "nav:ram")],
    [("📦 Archives", "nav:archives:0")],
)

KB_BACK         = _kb([("🔙 Back", "nav:main")])
KB_CLOSE        = _kb([("❌ Close",  "nav:close")])
KB_REFRESH_BACK = _kb(
    [("🔄 Refresh", "refresh:self"), ("🔙 Back", "nav:main")],
)

def _kb_queue(has_job):
    rows = []
    if has_job:
        rows.append([("⏹ Cancel Job", "do:cancel")])
    rows.append([("🔄 Refresh", "refresh:self"), ("🔙 Back", "nav:main")])
    return {"inline_keyboard": [
        [{"text": t, "callback_data": d} for t, d in row]
        for row in rows
    ]}

KB_MODE = lambda term: _kb(
    [("📄 ULP  (full hits)",      f"run:ulp:{term}")],
    [("🔑 COMBO (user:pass only)", f"run:combo:{term}")],
    [("🔙 Back",                    "nav:main")],
)

def _kb_archives(files, page):
    total_pages = max(1, (len(files) + ARCHIVES_PAGE_SIZE - 1) // ARCHIVES_PAGE_SIZE)
    page        = max(0, min(page, total_pages - 1))
    start       = page * ARCHIVES_PAGE_SIZE
    page_files  = files[start:start + ARCHIVES_PAGE_SIZE]

    rows = []
    for i, f in enumerate(page_files):
        idx   = start + i
        badge = "🔑" if f.name.startswith("COMBO") else "📄"
        label = f"{badge} {f.name}  ({_fmt_bytes(f.stat().st_size)})"
        rows.append([(label, f"pull:{idx}")])

    nav_row = []
    if page > 0:
        nav_row.append(("◀ Prev", f"nav:archives:{page-1}"))
    nav_row.append((f"📃 {page+1}/{total_pages}", "noop"))
    if page < total_pages - 1:
        nav_row.append(("▶ Next", f"nav:archives:{page+1}"))
    rows.append(nav_row)
    rows.append([("🧹 Clean All", "do:clean"), ("🔙 Back", "nav:main")])

    return {"inline_keyboard": [
        [{"text": t, "callback_data": d} for t, d in row]
        for row in rows
    ]}

def _kb_search_prompt(chat_id):
    """
    Dynamic search prompt keyboard.
    Top rows: recent-history chips (up to 5).
    Bottom row: Back.
    """
    history = _get_history(chat_id)
    rows = []
    for term in history:
        label = f"🕒 {term}"
        rows.append([(label, f"hs:{term}")])
    rows.append([("🔙 Back", "nav:main")])
    return {"inline_keyboard": [
        [{"text": t, "callback_data": d} for t, d in row]
        for row in rows
    ]}

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
        status_line = f"🟢 Running: [{lbl}] {job['term']}"
    else:
        status_line = "⚪ Idle"
    pending = f" · {q} queued" if q else ""
    text = (
        f"🦅 Falcon Bot  v{__version__}\n"
        f"―――――――――――――\n"
        f"{status_line}{pending}\n\n"
        f"Choose an action:"
    )
    return text, KB_MAIN

def _screen_status():
    out_path = Path(OUT_DIR)
    lines = ["🖥️  Server Status", ""]
    if out_path.exists():
        files = _list_archive_files()
        total = sum(f.stat().st_size for f in files)
        lines.append(f"📂  Archives  : {len(files)} files  ({_fmt_bytes(total)})")
        if files:
            lines.append(f"   Latest   : {files[0].name}")
    else:
        lines.append(f"⚠️  {OUT_DIR} not found")
    df = _get_disk_info()
    if df:
        lines += ["", f"💾  Disk (/)  : {df}"]
    with RUNNING_LOCK:
        job = RUNNING_JOB
    lines.append("")
    if job:
        lbl = "ULP" if job["mode"] == "ulp" else "COMBO"
        lines.append(f"🟢  Running   : [{lbl}] {job['term']}")
    else:
        lines.append("⚪  Bot is idle")
    lines.append(f"\n🕒  {time.strftime('%H:%M:%S')}")
    return "\n".join(lines), KB_REFRESH_BACK

def _screen_ram():
    lines = ["💾  RAM Usage", ""] + _get_ram_lines()
    lines.append(f"\n🕒  {time.strftime('%H:%M:%S')}")
    return "\n".join(lines), KB_REFRESH_BACK

def _screen_queue():
    with RUNNING_LOCK:
        job = RUNNING_JOB
    with QUEUE_LOCK:
        pending = list(QUEUE_LIST)
    lines = ["📋  Job Queue", ""]
    if job:
        lbl = "ULP" if job["mode"] == "ulp" else "COMBO"
        lines.append(f"🟢  Running : [{lbl}] {job['term']}")
    else:
        lines.append("⚪  Idle — no job running")
    if pending:
        lines.append(f"\n📌  Pending ({len(pending)}):")
        for i, (_, t, m) in enumerate(pending, 1):
            lbl = "ULP" if m == "ulp" else "COMBO"
            lines.append(f"   {i}.  [{lbl}]  {t}")
    else:
        lines.append("\n📌  Queue is empty")
    return "\n".join(lines), _kb_queue(job is not None)

def _screen_archives(page=0):
    files = _list_archive_files()
    total_pages = max(1, (len(files) + ARCHIVES_PAGE_SIZE - 1) // ARCHIVES_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    if not files:
        text = (
            "📦  Archives\n"
            "―――――――――――――\n"
            "  No result files saved yet."
        )
        return text, KB_BACK

    total_size = sum(f.stat().st_size for f in files)
    start = page * ARCHIVES_PAGE_SIZE
    page_files = files[start:start + ARCHIVES_PAGE_SIZE]

    lines = [
        "📦  Archives",
        f"  {len(files)} files  ·  {_fmt_bytes(total_size)} total",
        "",
    ]
    for i, f in enumerate(page_files, start + 1):
        badge = "🔑" if f.name.startswith("COMBO") else "📄"
        lines.append(f"  {i}.  {badge} {f.name}")
        lines.append(f"       {_fmt_bytes(f.stat().st_size)}  ·  "
                     f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(f.stat().st_mtime))}")
        lines.append("")

    lines.append(f"📃 Page {page+1} of {total_pages}  ·  Tap a file to download it")

    return "\n".join(lines), _kb_archives(files, page)

# ════════════════════════════════════════════════════════════
# NAV ACTIONS
# ════════════════════════════════════════════════════════════
def _show_main(chat_id, edit_msg_id=None):
    text, kb = _screen_main()
    _set_last_screen(chat_id, "main")
    if edit_msg_id:
        edit_message(chat_id, edit_msg_id, text, reply_markup=kb)
        _store_nav(chat_id, edit_msg_id)
    else:
        _delete_nav(chat_id)
        mid = send_message(chat_id, text, reply_markup=kb)
        _store_nav(chat_id, mid)

def _show_screen(chat_id, screen_fn, screen_name, edit_msg_id=None, **kwargs):
    text, kb = screen_fn(**kwargs)
    _set_last_screen(chat_id, screen_name)
    if edit_msg_id:
        edit_message(chat_id, edit_msg_id, text, reply_markup=kb)
        _store_nav(chat_id, edit_msg_id)
    else:
        _delete_nav(chat_id)
        mid = send_message(chat_id, text, reply_markup=kb)
        _store_nav(chat_id, mid)

def _show_search_prompt(chat_id, edit_msg_id=None):
    history = _get_history(chat_id)
    hint = ""
    if history:
        hint = "\n\nRecent (tap to reuse):"
    text = (
        f"🔍  Search\n"
        f"―――――――――――――\n"
        f"Type your search term and send it:\n"
        f"(e.g.  netflix.com  or  @gmail.com){hint}"
    )
    _set_last_screen(chat_id, "search")
    kb = _kb_search_prompt(chat_id)
    if edit_msg_id:
        edit_message(chat_id, edit_msg_id, text, reply_markup=kb)
        _store_nav(chat_id, edit_msg_id)
    else:
        _delete_nav(chat_id)
        mid = send_message(chat_id, text, reply_markup=kb)
        _store_nav(chat_id, mid)
    set_state(chat_id, ST_AWAIT_TERM)

def _show_mode_select(chat_id, term, edit_msg_id=None):
    _add_history(chat_id, term)
    text = (
        f"🔍  Term: {term}\n"
        f"―――――――――――――\n"
        f"Select search mode:"
    )
    _set_last_screen(chat_id, "mode")
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
# INLINE QUERY HANDLER  (dynamic search suggestions)
# ════════════════════════════════════════════════════════════
def handle_inline_query(inline_query_id, from_user_id, query):
    """
    When the user types @BotName <term> in any chat, or types in the
    inline search box, return live cards showing ULP / COMBO options.
    Only responds to allowed users.
    """
    if from_user_id not in ALLOWED_CHAT_IDS:
        answer_inline(inline_query_id, [])
        return

    query = query.strip()
    if not query:
        # Show recent history as suggestions
        history = _get_history(from_user_id)
        results = []
        for i, term in enumerate(history):
            for mode, badge in (("ulp", "📄 ULP"), ("combo", "🔑 COMBO")):
                results.append({
                    "type": "article",
                    "id": f"hist_{i}_{mode}",
                    "title": f"{badge} — {term}",
                    "description": "Recent search · tap to enqueue",
                    "input_message_content": {
                        "message_text": f"/{'c' if mode=='combo' else 's'} {term}"
                    },
                })
        answer_inline(inline_query_id, results[:10])
        return

    results = [
        {
            "type": "article",
            "id": "ulp",
            "title": f"📄 ULP — {query}",
            "description": "Full matched lines, deduped",
            "input_message_content": {
                "message_text": f"/s {query}"
            },
        },
        {
            "type": "article",
            "id": "combo",
            "title": f"🔑 COMBO — {query}",
            "description": "Clean user:pass pairs only",
            "input_message_content": {
                "message_text": f"/c {query}"
            },
        },
    ]
    answer_inline(inline_query_id, results)

# ════════════════════════════════════════════════════════════
# FILE DELIVERY
# ════════════════════════════════════════════════════════════
_SPLIT_CHUNK = 64 * 1024  # 64 KB read buffer for splitting

def _split_and_send(chat_id, file_path, caption, msg_id):
    """
    Split *file_path* into sequential parts of at most TG_MAX_BYTES each
    and upload every part as a Telegram document.

    The old implementation used shutil.copyfileobj(src, dst, length=TG_MAX_BYTES)
    inside a loop over parts.  That is wrong: copyfileobj's `length` parameter is
    only the read-buffer size — it keeps reading until EOF regardless, so the
    first part consumed the whole file and all later parts were empty.

    The fix reads at most `remaining` bytes per part using a small chunk loop,
    stopping each part file as soon as TG_MAX_BYTES have been written.
    """
    file_size   = os.path.getsize(file_path)
    stem        = Path(file_path).stem
    ext         = Path(file_path).suffix
    tmp_dir     = Path(file_path).parent
    total_parts = (file_size + TG_MAX_BYTES - 1) // TG_MAX_BYTES
    part_paths  = []

    edit_message(chat_id, msg_id,
        f"✂️  File is {_fmt_bytes(file_size)} — splitting into {total_parts} parts...")
    try:
        with open(file_path, "rb") as src:
            for i in range(total_parts):
                pname = tmp_dir / f"{stem}.part{i+1}of{total_parts}{ext}"
                remaining = TG_MAX_BYTES
                with open(pname, "wb") as dst:
                    while remaining > 0:
                        chunk = src.read(min(_SPLIT_CHUNK, remaining))
                        if not chunk:
                            break
                        dst.write(chunk)
                        remaining -= len(chunk)
                part_paths.append(pname)
    except Exception as e:
        edit_message(chat_id, msg_id, f"❌  Failed to split: {e}")
        return False

    all_ok = True
    for i, part in enumerate(part_paths, 1):
        edit_message(chat_id, msg_id,
            f"⬆️  Uploading part {i}/{total_parts} ({_fmt_bytes(os.path.getsize(part))})...")
        ok, err = send_document(chat_id, str(part),
                                caption=f"{caption} — part {i}/{total_parts}")
        if not ok:
            edit_message(chat_id, msg_id,
                f"❌  Upload failed on part {i}/{total_parts}: {err}")
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
        edit_message(chat_id, msg_id, f"⬆️  Uploading ({_fmt_bytes(fsize)})...")
        ok, err = send_document(chat_id, file_path, caption=caption)
        if not ok:
            edit_message(chat_id, msg_id, f"❌  Upload failed: {err}\n{file_path}")
    else:
        ok = _split_and_send(chat_id, file_path, caption, msg_id)
        if not ok:
            edit_message(chat_id, msg_id,
                f"❌  Partial failure. File on server:\n{file_path}")

def _pull_archive_file(chat_id, file_index, callback_msg_id):
    files = _list_archive_files()
    if file_index >= len(files):
        edit_message(chat_id, callback_msg_id,
            "⚠️  File not found (list may have changed). Tap 🔄 Refresh.")
        return
    f = files[file_index]
    status_id = send_message(chat_id,
        f"⬆️  Preparing: {f.name}\n"
        f"  Size: {_fmt_bytes(f.stat().st_size)}")
    if status_id is None:
        return
    deliver_file(chat_id, str(f), "Archive", f.stem, status_id)
    edit_message(chat_id, status_id,
        f"✅  Sent: {f.name}",
        reply_markup=_kb([("📦 Back to Archives", "nav:archives:0"), ("🏠 Home", "nav:main")]))

# ════════════════════════════════════════════════════════════
# FALCON WORKER
# ════════════════════════════════════════════════════════════
def _cancel_watcher(proc, chat_id, msg_id):
    """
    Dedicated thread: polls CANCEL_EVENT every 0.25 s.
    When set, kills the subprocess immediately and updates
    the job message — no longer waiting for the next stdout line.
    """
    while proc.poll() is None:
        if CANCEL_EVENT.is_set():
            try:
                proc.kill()
            except Exception:
                pass
            # Update the message immediately so the user sees feedback
            edit_message(chat_id, msg_id,
                "⏹  Cancelling…\n"
                "―――――――――――――\n"
                "Waiting for process to stop.",
                reply_markup=None)
            return
        time.sleep(0.25)

def run_falcon(chat_id, term, mode):
    global RUNNING_JOB

    st       = safe_term(term)
    label    = "ULP" if mode == "ulp" else "COMBO"
    out_file = os.path.join(OUT_DIR,
        f"ULP_{st}.txt" if mode == "ulp" else f"COMBO_LP_{st}.txt")

    with RUNNING_LOCK:
        RUNNING_JOB = {"chat_id": chat_id, "term": term, "mode": mode}
    CANCEL_EVENT.clear()

    # ── ACK the user immediately (<100 ms) ───────────────────
    msg_id = send_message(chat_id,
        f"🔎  [{label}]  {term}\n"
        f"―――――――――――――\n"
        f"⏳  Queuing process…",
        reply_markup=_kb([("⏹ Cancel", "do:cancel")]))
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

    _set_running_proc(proc)

    # ── Start cancel-watcher thread ───────────────────────────
    watcher = threading.Thread(
        target=_cancel_watcher,
        args=(proc, chat_id, msg_id),
        daemon=True
    )
    watcher.start()

    last_text      = ""
    last_edit      = 0.0
    cancelled      = False
    done_stats     = None
    job_start      = time.time()
    phase2_start   = None
    last_hits      = 0
    expected_hits  = 0
    _hit_samples   = []
    _HIT_WINDOW    = 30.0
    _HIT_ETA_MULT  = 1.1
    started_shown  = False    # track whether we showed "Starting…" update

    try:
        for line in proc.stdout:
            if CANCEL_EVENT.is_set():
                cancelled = True
                break

            line = line.strip()
            now  = time.time()
            m    = PROGRESS_RE.search(line)
            d    = DONE_RE.search(line)

            wall_elapsed = now - job_start
            edit_interval = (EDIT_INTERVAL_FAST
                             if wall_elapsed < EDIT_FAST_WINDOW
                             else EDIT_INTERVAL_NORM)

            # Show "Starting…" on first stdout line (proves process is alive)
            if not started_shown:
                edit_message(chat_id, msg_id,
                    f"🔎  [{label}]  {term}\n"
                    f"―――――――――――――\n"
                    f"🟡  Starting…",
                    reply_markup=_kb([("⏹ Cancel", "do:cancel")]))
                started_shown = True

            if m:
                phase            = int(m.group(1))
                reported_elapsed = float(m.group(5))

                if phase == 1:
                    hits = int(m.group(2) or 0)
                    ulp  = int(m.group(3) or 0)

                    _hit_samples.append((now, hits))
                    _hit_samples = [(t, h) for t, h in _hit_samples
                                    if now - t <= _HIT_WINDOW]
                    if len(_hit_samples) >= 2 and reported_elapsed > 5:
                        t0, h0 = _hit_samples[0]
                        rate   = (hits - h0) / max(now - t0, 1.0)
                        if rate > 0:
                            remaining = FULL_RUN_SECONDS - reported_elapsed
                            expected_hits = int((hits + rate * remaining) * _HIT_ETA_MULT)
                    last_hits = hits

                    bar = _progress_bar(
                        elapsed_s=reported_elapsed,
                        done=False, phase=1,
                        hits=last_hits,
                        expected_hits=expected_hits,
                    )
                    phase_icon = "🔵"
                    text = (
                        f"🔎  [{label}]  {term}\n"
                        f"―――――――――――――\n"
                        f"{phase_icon}  Phase 1 — Scanning\n"
                        f"{bar}\n"
                        f"  Hits    : {hits:,}\n"
                        f"  Unique  : {ulp:,}\n"
                        f"  Elapsed : {reported_elapsed:.1f}s"
                    )

                else:  # phase 2
                    if phase2_start is None:
                        phase2_start = now
                    combos = int(m.group(4) or 0)
                    p2_elapsed = now - phase2_start

                    bar = _progress_bar(
                        elapsed_s=reported_elapsed,
                        done=False, phase=2,
                        phase2_elapsed=p2_elapsed,
                    )
                    phase_icon = "🟢"
                    text = (
                        f"🔎  [{label}]  {term}\n"
                        f"―――――――――――――\n"
                        f"{phase_icon}  Phase 2 — Extracting\n"
                        f"{bar}\n"
                        f"  Combos  : {combos:,}\n"
                        f"  Elapsed : {reported_elapsed:.1f}s"
                    )

                if now - last_edit >= edit_interval and text != last_text:
                    edit_message(chat_id, msg_id, text,
                                 reply_markup=_kb([("⏹ Cancel", "do:cancel")]))
                    last_edit = now
                    last_text = text

            elif d:
                hits        = int(d.group(1))
                ulp         = int(d.group(2))
                combos      = int(d.group(3))
                elapsed_s   = float(d.group(4))
                ulp_bytes   = int(d.group(5) or 0)
                combo_bytes = int(d.group(6) or 0)
                done_stats  = (hits, ulp, combos, elapsed_s, ulp_bytes, combo_bytes)

                bar = _progress_bar(elapsed_s, done=True)
                edit_message(chat_id, msg_id,
                    f"📊  [{label}]  {term}\n"
                    f"―――――――――――――\n"
                    f"{bar}\n"
                    f"  Hits    : {hits:,}\n"
                    f"  ULP     : {ulp:,}\n"
                    f"  Combos  : {combos:,}\n"
                    f"  Time    : {elapsed_s:.1f}s\n"
                    f"⬆️  Preparing upload...")
        proc.wait()
    except Exception as e:
        edit_message(chat_id, msg_id, f"❌  Error: {e}")
        with RUNNING_LOCK:
            RUNNING_JOB = None
        _clear_running_proc()
        return
    finally:
        with RUNNING_LOCK:
            RUNNING_JOB = None
        _clear_running_proc()

    watcher.join(timeout=2)

    if CANCEL_EVENT.is_set() or cancelled:
        edit_message(chat_id, msg_id,
            f"⛔  Cancelled: {term}\n"
            f"―――――――――――――",
            reply_markup=_kb([("🏠 Home", "nav:main")]))
        return

    if proc.returncode != 0:
        edit_message(chat_id, msg_id,
            f"❌  Falcon exited {proc.returncode}",
            reply_markup=_kb([("🏠 Home", "nav:main")]))
        return

    if not os.path.exists(out_file) or os.path.getsize(out_file) == 0:
        edit_message(chat_id, msg_id,
            f"⚠️  No results for: {term}",
            reply_markup=_kb([("🏠 Home", "nav:main")]))
        return

    deliver_file(chat_id, out_file, label, term, msg_id)

    if done_stats:
        hits, ulp, combos, elapsed_s, ulp_bytes, combo_bytes = done_stats
        fsize       = os.path.getsize(out_file) if os.path.exists(out_file) else 0
        total_parts = max(1, (fsize + TG_MAX_BYTES - 1) // TG_MAX_BYTES)
        lines = [
            f"✅  Done — {term}",
            f"―――――――――――――",
            f"  Hits    : {hits:,}",
            f"  ULP     : {ulp:,}",
            f"  Combos  : {combos:,}",
            f"  Time    : {elapsed_s:.1f}s",
        ]
        if mode == "ulp" and ulp_bytes:
            lines.append(f"  File    : {_fmt_bytes(ulp_bytes)}")
        elif mode == "combo" and combo_bytes:
            lines.append(f"  File    : {_fmt_bytes(combo_bytes)}")
        if total_parts > 1:
            lines.append(f"  Parts   : {total_parts} × 45 MB")
        send_message(chat_id, "\n".join(lines),
                     reply_markup=_kb(
                         [("🔍 Search Again", "nav:search"),
                          ("📦 Archives",    "nav:archives:0"),
                          ("🏠 Home",         "nav:main")]
                     ))

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
            f"📋  Queued at position {pos}\n"
            f"  [{'ULP' if mode=='ulp' else 'COMBO'}]  {term}\n"
            f"Starts when current job finishes.",
            reply_markup=_kb(
                [("📋 Queue", "nav:queue"), ("⏹ Cancel Job", "do:cancel")]
            ))

# ════════════════════════════════════════════════════════════
# CALLBACK QUERY HANDLER
# ════════════════════════════════════════════════════════════
def handle_callback(chat_id, msg_id, callback_id, data):
    answer_callback(callback_id)

    if data == "nav:main":
        _show_main(chat_id, edit_msg_id=msg_id)

    elif data == "nav:search":
        _show_search_prompt(chat_id, edit_msg_id=msg_id)

    elif data == "nav:status":
        _show_screen(chat_id, _screen_status, "status", edit_msg_id=msg_id)

    elif data == "nav:ram":
        _show_screen(chat_id, _screen_ram, "ram", edit_msg_id=msg_id)

    elif data == "nav:queue":
        _show_screen(chat_id, _screen_queue, "queue", edit_msg_id=msg_id)

    elif data.startswith("nav:archives:"):
        try:
            page = int(data.split(":")[2])
        except (IndexError, ValueError):
            page = 0
        _show_screen(chat_id, _screen_archives, f"archives:{page}",
                     edit_msg_id=msg_id, page=page)

    elif data == "nav:close":
        delete_message(chat_id, msg_id)
        _pop_nav(chat_id)

    elif data == "refresh:self":
        screen = _get_last_screen(chat_id)
        if screen == "status":
            _show_screen(chat_id, _screen_status, "status", edit_msg_id=msg_id)
        elif screen == "ram":
            _show_screen(chat_id, _screen_ram, "ram", edit_msg_id=msg_id)
        elif screen == "queue":
            _show_screen(chat_id, _screen_queue, "queue", edit_msg_id=msg_id)
        elif screen.startswith("archives:"):
            try:
                page = int(screen.split(":")[1])
            except (IndexError, ValueError):
                page = 0
            _show_screen(chat_id, _screen_archives, screen,
                         edit_msg_id=msg_id, page=page)
        else:
            _show_screen(chat_id, _screen_status, "status", edit_msg_id=msg_id)

    elif data == "noop":
        pass

    elif data.startswith("hs:"):
        # History chip tapped — go straight to mode select
        term = data[3:]
        _show_mode_select(chat_id, term, edit_msg_id=msg_id)

    elif data.startswith("run:"):
        _, mode, *term_parts = data.split(":")
        term = ":".join(term_parts)
        enqueue(chat_id, term, mode)
        delete_message(chat_id, msg_id)
        _pop_nav(chat_id)

    elif data.startswith("pull:"):
        try:
            idx = int(data.split(":")[1])
        except (IndexError, ValueError):
            return
        threading.Thread(
            target=_pull_archive_file,
            args=(chat_id, idx, msg_id),
            daemon=True
        ).start()

    elif data == "do:cancel":
        with RUNNING_LOCK:
            job = RUNNING_JOB
        if job:
            CANCEL_EVENT.set()
            _kill_running_proc()   # kill immediately — don't wait for next stdout
            answer_callback(callback_id, text="⏹ Cancelling…")
        else:
            answer_callback(callback_id, text="ℹ️ Nothing running")

    elif data == "do:clean":
        files   = _list_archive_files()
        total   = sum(f.stat().st_size for f in files)
        deleted = 0
        for f in files:
            try:
                f.unlink()
                deleted += 1
            except Exception:
                pass
        text, kb = _screen_archives(page=0)
        edit_message(chat_id, msg_id,
            f"🧹  Cleaned {deleted} file(s) — freed {_fmt_bytes(total)}\n\n" + text,
            reply_markup=kb)

# ════════════════════════════════════════════════════════════
# MESSAGE HANDLER
# ════════════════════════════════════════════════════════════
def handle_message(chat_id, text, msg_id):
    text  = text.strip()
    state = get_state(chat_id)

    if text in ("/start", "/help"):
        set_state(chat_id, ST_IDLE)
        _show_main(chat_id)
        return

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
            _kill_running_proc()
        return
    if text == "/queue":
        _show_screen(chat_id, _screen_queue, "queue")
        return
    if text == "/status":
        _show_screen(chat_id, _screen_status, "status")
        return
    if text == "/ram":
        _show_screen(chat_id, _screen_ram, "ram")
        return
    if text == "/archives":
        _show_screen(chat_id, _screen_archives, "archives:0", page=0)
        return
    if text == "/clean":
        files   = _list_archive_files()
        total   = sum(f.stat().st_size for f in files)
        deleted = 0
        for f in files:
            try:
                f.unlink()
                deleted += 1
            except Exception:
                pass
        send_message(chat_id,
            f"🧹  Cleaned {deleted} file(s) — freed {_fmt_bytes(total)}")
        return

    if state == ST_AWAIT_TERM:
        delete_message(chat_id, msg_id)
        nav = _NAV_MSG.get(chat_id)
        _show_mode_select(chat_id, text, edit_msg_id=nav)
        return

    _show_main(chat_id)

# ════════════════════════════════════════════════════════════
# MAIN POLLING LOOP
# ════════════════════════════════════════════════════════════
def main():
    global LAST_UPDATE_ID

    threading.Thread(target=queue_worker, daemon=True).start()
    print(f"Falcon Bot v{__version__} started. (source={SOURCE_DIR}, out={OUT_DIR})")
    print(f"Instant-cancel watcher enabled. Inline search enabled.")
    print(f"Edit interval: fast={EDIT_INTERVAL_FAST}s for first {EDIT_FAST_WINDOW}s, norm={EDIT_INTERVAL_NORM}s.")

    backoff = 2.0
    while True:
        try:
            r = api_get("getUpdates", params={
                "offset":          LAST_UPDATE_ID + 1,
                "timeout":         25,           # slightly shorter for tighter heartbeat
                "allowed_updates": json.dumps(["message", "callback_query", "inline_query"]),
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

                # ── inline_query (dynamic search bar) ────────
                iq = upd.get("inline_query")
                if iq:
                    handle_inline_query(
                        iq["id"],
                        iq["from"]["id"],
                        iq.get("query", ""),
                    )
                    continue

                # ── callback_query ────────────────────────────
                cq = upd.get("callback_query")
                if cq:
                    chat_id     = cq["message"]["chat"]["id"]
                    msg_id      = cq["message"]["message_id"]
                    callback_id = cq["id"]
                    data        = cq.get("data", "")
                    if chat_id in ALLOWED_CHAT_IDS:
                        handle_callback(chat_id, msg_id, callback_id, data)
                    else:
                        answer_callback(callback_id, text="⛔ Unauthorized")
                    continue

                # ── message ───────────────────────────────────
                msg     = upd.get("message", {})
                chat_id = msg.get("chat", {}).get("id")
                text    = msg.get("text", "")
                mid     = msg.get("message_id")
                if chat_id in ALLOWED_CHAT_IDS and text:
                    handle_message(chat_id, text, mid)

        time.sleep(0.3)

if __name__ == "__main__":
    main()
