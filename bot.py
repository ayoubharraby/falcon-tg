#!/usr/bin/env python3
"""
Falcon Telegram Bot v4.0.1

Changes in v4.0.1 (cancel fix):
  - _kill_running_proc() now kills the ENTIRE process group (parent +
    ProcessPoolExecutor workers spawned by falcon_parse.py), not just the
    single falcon_parse.py PID. Previously, orphaned pool workers kept the
    stdout pipe open, so `for line in proc.stdout` never got EOF and
    RUNNING_JOB was never cleared -> Queue screen showed the job as
    "running" forever after Cancel.
  - subprocess.Popen(...) now passes start_new_session=True so the child
    (and everything it forks) is a session/process-group leader that can
    be killed atomically with os.killpg().

Changes in v4.0.0:
  - QUEUE NAVIGATION: each queued job has a unique ID. Queue screen shows
    per-job [⬆] [⬇] [🗑] buttons to reorder or cancel individual pending
    jobs. Running job keeps its instant-kill [⏹ Cancel].
  - UNIQUE REMOVED: Phase 1 progress no longer shows "Unique" counter
    (was always 0 during scan — dedup only happens in Phase 2).
  - MULTI-TERM SEARCH: enter comma-separated terms (e.g. netflix.com, hulu.com)
    to enqueue all as a single multi-OR job passed to falcon_parse --terms.
  - SEARCH PRESETS: built-in named preset groups. 🗂 Presets button on main
    menu. /addpreset <name> <t1,t2,...> and /delpreset <name> commands.
  - LITE MODE: ⚡ Lite button on mode select. Stops after first 1000 hits,
    randomly picks up to 10, sends each result as a separate message.
    Format controlled by LITE_FORMAT env var: 'ulp' (full line, default)
    or 'combo' (user:pass only).
  - DORK SEARCH: extended search syntax parsed from query:
      domain:netflix.com  user:@gmail.com  pass:123  ext:.fr  -word
    All constraints ANDed. Passed to falcon_parse as --dork-* flags.
  - HELP SCREEN: ❓ Help button on main menu. Full navigation guide +
    dork syntax reference. Also /help command.
  - BACK from mode-select returns to search prompt, not main menu.

Flow:
  /start       → Main Menu
  🔍 Search    → type term (plain or dork syntax) → mode select
  🗂 Presets   → pick preset → mode select
  📋 Queue     → per-job ⬆⬇🗑, cancel running
  🖥 Status    → disk / archives summary
  💾 RAM       → memory stats
  📦 Archives  → paginated file list → tap → download
  ❓ Help      → navigation + dork syntax guide

Config: copy env.example → .env
"""
__version__ = "4.0.1"

import os, re, time, uuid, queue, traceback, subprocess, threading, collections, json, shutil, random, signal
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
LITE_FORMAT      = os.environ.get("LITE_FORMAT", "ulp").lower()   # 'ulp' or 'combo'

API          = f"https://api.telegram.org/bot{TOKEN}"
TG_MAX_BYTES = int(os.environ.get("TG_MAX_BYTES", str(45 * 1024 * 1024)))

# ── Progress bar calibration ─────────────────────────────────
FULL_RUN_SECONDS  = 731.0
PHASE2_SECONDS    = 120.0

# ── Adaptive edit intervals ──────────────────────────────────
EDIT_INTERVAL_FAST = 0.4
EDIT_INTERVAL_NORM = 1.0
EDIT_FAST_WINDOW   = 30.0

# ── Archives pagination ──────────────────────────────────────
ARCHIVES_PAGE_SIZE = 8

# ── Upload retry ─────────────────────────────────────────────
UPLOAD_MAX_RETRIES   = 3
UPLOAD_RETRY_BACKOFF = 2.0

# ── Search history ───────────────────────────────────────────
_SEARCH_HISTORY = {}   # chat_id -> deque(maxlen=5)
_HISTORY_LOCK   = threading.Lock()
HISTORY_MAXLEN  = 5

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

def _clear_history(chat_id):
    with _HISTORY_LOCK:
        _SEARCH_HISTORY.pop(chat_id, None)

# ════════════════════════════════════════════════════════════
# PRESETS  (in-memory, per process)
# ════════════════════════════════════════════════════════════
# Format: { name: [term, term, ...] }
PRESETS = {
    "🎬 Streaming":   ["netflix.com", "hulu.com", "disneyplus.com", "hbomax.com"],
    "🎮 Gaming":      ["steampowered.com", "epicgames.com", "ea.com", "ubisoft.com"],
    "💳 Banking":     ["paypal.com", "chase.com", "wellsfargo.com", "bankofamerica.com"],
    "📧 Mail":        ["@gmail.com", "@yahoo.com", "@hotmail.com", "@outlook.com"],
    "🛒 Shopping":    ["amazon.com", "ebay.com", "shopify.com"],
}
PRESETS_LOCK = threading.Lock()

def _preset_names():
    with PRESETS_LOCK:
        return list(PRESETS.keys())

def _preset_terms(name):
    with PRESETS_LOCK:
        return list(PRESETS.get(name, []))

def _add_preset(name, terms):
    with PRESETS_LOCK:
        PRESETS[name] = terms

def _del_preset(name):
    with PRESETS_LOCK:
        return PRESETS.pop(name, None)

# ════════════════════════════════════════════════════════════
# DORK PARSER
# ════════════════════════════════════════════════════════════
# Supported operators:
#   domain:value   → --dork-domain
#   site:value     → alias for domain:
#   user:value     → --dork-user
#   pass:value     → --dork-pass
#   ext:value      → --dork-ext
#   -word          → --dork-not (first occurrence)
#   bare words     → collected as the base search terms
#
# Example:
#   netflix.com domain:netflix.com user:@gmail ext:.com -free
#   → terms=["netflix.com"], dork_domain="netflix.com",
#     dork_user="@gmail", dork_ext=".com", dork_not="free"

_DORK_KEY_RE = re.compile(
    r'(?:domain|site):([\S]+)'
    r'|user:([\S]+)'
    r'|pass:([\S]+)'
    r'|ext:([\S]+)'
    r'|-(\S+)'
)

def parse_dork(raw_input):
    """
    Parse a dork-style query string.
    Returns dict with keys:
      terms        : list[str]  base search terms (for ripgrep)
      dork_domain  : str or ''
      dork_user    : str or ''
      dork_pass    : str or ''
      dork_ext     : str or ''
      dork_not     : str or ''
      display      : str        human-readable summary of active dorks
    """
    result = {
        "terms":       [],
        "dork_domain": "",
        "dork_user":   "",
        "dork_pass":   "",
        "dork_ext":    "",
        "dork_not":    "",
    }
    remaining = raw_input.strip()
    for m in _DORK_KEY_RE.finditer(remaining):
        if m.group(1):   result["dork_domain"] = m.group(1)
        elif m.group(2): result["dork_user"]   = m.group(2)
        elif m.group(3): result["dork_pass"]   = m.group(3)
        elif m.group(4): result["dork_ext"]    = m.group(4)
        elif m.group(5): result["dork_not"]    = m.group(5)
    # Strip dork tokens to get bare terms
    clean = _DORK_KEY_RE.sub("", remaining).strip()
    # Support comma-separated multi-terms in the bare part
    if "," in clean:
        result["terms"] = [t.strip() for t in clean.split(",") if t.strip()]
    elif clean:
        result["terms"] = [clean]
    # If no bare terms but domain is set, use domain as base term
    if not result["terms"] and result["dork_domain"]:
        result["terms"] = [result["dork_domain"]]
    # Build display summary
    parts = []
    if result["dork_domain"]: parts.append(f"domain:{result['dork_domain']}")
    if result["dork_user"]:   parts.append(f"user:{result['dork_user']}")
    if result["dork_pass"]:   parts.append(f"pass:{result['dork_pass']}")
    if result["dork_ext"]:    parts.append(f"ext:{result['dork_ext']}")
    if result["dork_not"]:    parts.append(f"-{result['dork_not']}")
    result["display"] = "  ".join(parts)
    return result

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
# STATE MACHINE
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
_NAV_MSG    = {}   # chat_id -> message_id
_NAV_LOCK   = threading.Lock()
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
# JOB QUEUE  (rewritten for v4.0.0 — UUID-keyed, orderable)
# ════════════════════════════════════════════════════════════
# Each entry: {id, chat_id, term, terms_list, mode, dork}
JOB_QUEUE    = queue.Queue()
QUEUE_LIST   = []          # ordered list of job dicts (mutable)
QUEUE_LOCK   = threading.Lock()
CANCEL_EVENT = threading.Event()
RUNNING_JOB  = None
RUNNING_LOCK = threading.Lock()
UPDATE_LOCK  = threading.Lock()

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
    """
    Kill the running falcon_parse.py process AND its entire process
    group (this covers ProcessPoolExecutor workers it spawns for
    pure-Python search / combo extraction). Killing only the single
    PID left orphaned pool workers alive, which kept proc.stdout's
    write end open -> `for line in proc.stdout` never got EOF ->
    RUNNING_JOB was never cleared -> Queue showed "running" forever.
    """
    with _RUNNING_PROC_LOCK:
        proc = _RUNNING_PROC
    if not proc or proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception:
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
LITE_LINE_RE  = re.compile(r'^LITE_LINE (.+)$')
LITE_DONE_RE  = re.compile(r'^LITE_DONE hits=(\d+) sampled=(\d+) elapsed=([\d.]+)$')
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
            f"  Total  : {_fmt_bytes(total * 1024)}",
            f"  Used   : {_fmt_bytes(used  * 1024)} ({pct:.1f}%)",
            f"  Free   : {_fmt_bytes(avail * 1024)}",
        ]
        if swap_t:
            lines.append(f"  Swap   : {_fmt_bytes((swap_t-swap_f)*1024)} / {_fmt_bytes(swap_t*1024)}")
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
# PROGRESS BAR  (monotonic)
# ════════════════════════════════════════════════════════════
def _progress_bar(elapsed_s, width=14, done=False, phase=1,
                  phase2_elapsed=0.0, max_frac=0.0):
    if done:
        frac = 1.0
    elif phase == 2:
        frac = min(phase2_elapsed / PHASE2_SECONDS, 0.99)
    else:
        frac = min(elapsed_s / FULL_RUN_SECONDS, 0.99)
    frac = max(frac, max_frac)
    frac = min(frac, 1.0 if done else 0.99)
    filled = int(frac * width)
    bar    = "█" * filled + "░" * (width - filled)
    pct    = int(frac * 100)
    return f"  [{bar}]  {pct}%", frac

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
    [("🔍 Search",    "nav:search"),   ("📋 Queue",    "nav:queue")],
    [("🗂 Presets",   "nav:presets"),  ("❓ Help",     "nav:help")],
    [("🖥️ Status",   "nav:status"),   ("💾 RAM",      "nav:ram")],
    [("📦 Archives",  "nav:archives:0")],
)

KB_BACK         = _kb([("🔙 Back", "nav:main")])
KB_REFRESH_BACK = _kb(
    [("🔄 Refresh", "refresh:self"), ("🔙 Back", "nav:main")],
)

def _kb_mode(term_raw):
    # term_raw stored in callback_data — encode as base64 to avoid colon issues
    import base64
    enc = base64.urlsafe_b64encode(term_raw.encode()).decode()
    return _kb(
        [("📄 ULP  (full hits)",        f"run:ulp:{enc}")],
        [("🔑 COMBO (user:pass only)",   f"run:combo:{enc}")],
        [("⚡ Lite  (sample 10)",        f"run:lite:{enc}")],
        [("🔙 Back",                      "nav:search")],
    )

def _kb_queue_screen(running_job, pending):
    """
    Build queue keyboard:
      - If running job: [⏹ Cancel Running]
      - For each pending job: [⬆][⬇][🗑 <label>]
      - [🔄 Refresh] [🔙 Back]
    """
    rows = []
    if running_job:
        rows.append([("⏹ Cancel Running", "do:cancel")])
    for i, job in enumerate(pending):
        jid = job["id"]
        lbl = "ULP" if job["mode"] == "ulp" else ("COMBO" if job["mode"] == "combo" else "LITE")
        name = job["term"][:20]
        up_cb   = f"qup:{jid}"   if i > 0                  else "noop"
        down_cb = f"qdown:{jid}" if i < len(pending) - 1  else "noop"
        up_btn   = "⬆️" if i > 0               else "·"
        down_btn = "⬇️" if i < len(pending)-1  else "·"
        rows.append([
            (up_btn,   up_cb),
            (down_btn, down_cb),
            (f"🗑 [{lbl}] {name}", f"qcancel:{jid}"),
        ])
    rows.append([("🔄 Refresh", "refresh:self"), ("🔙 Back", "nav:main")])
    return {"inline_keyboard": [
        [{"text": t, "callback_data": d} for t, d in row]
        for row in rows
    ]}

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
    history = _get_history(chat_id)
    rows = []
    for term in history:
        rows.append([(f"🕒 {term}", f"hs:{term}")])
    if history:
        rows.append([("🗑 Clear History", "do:clear_history")])
    rows.append([("🔙 Back", "nav:main")])
    return {"inline_keyboard": [
        [{"text": t, "callback_data": d} for t, d in row]
        for row in rows
    ]}

def _kb_presets():
    names = _preset_names()
    rows  = []
    for name in names:
        rows.append([(name, f"preset:{name}")])
    rows.append([("🔙 Back", "nav:main")])
    return {"inline_keyboard": [
        [{"text": t, "callback_data": d} for t, d in row]
        for row in rows
    ]}

# ════════════════════════════════════════════════════════════
# HELP TEXT
# ════════════════════════════════════════════════════════════
HELP_TEXT = """
❓  Falcon Bot — Help  v4.0.1
━━━━━━━━━━━━━━━━━━━━━
📍 NAVIGATION
  🔍 Search    — enter a term, get mode selector
  🗂 Presets   — saved term groups, one tap to enqueue all
  📋 Queue     — view / reorder / cancel pending jobs
  🖥 Status    — disk, archives summary
  💾 RAM       — server memory stats
  📦 Archives  — download result files

🔍 SEARCH MODES
  📄 ULP    full matched + deduped lines → .txt file
  🔑 COMBO  clean user:pass pairs only   → .txt file
  ⚡ Lite   stops at 1000 hits, samples 10, sends each
            result as individual message (no file)

🎯 DORK SYNTAX
Mix plain terms with operators (space-separated):

  <term>           basic search  e.g. netflix.com
  t1, t2, t3       multi-term OR search (comma-separated)
  domain:<val>     line must contain this host/domain
  site:<val>       alias for domain:
  user:<val>       username part must contain value
  pass:<val>       password part must contain value
  ext:<val>        email extension  e.g.  ext:.fr
  -<word>          exclude lines containing word

📌 DORK EXAMPLES
  netflix.com ext:.fr
  → Netflix accounts with French email addresses

  amazon.com user:@gmail.com -free
  → Amazon logins with Gmail, excluding spam

  site:paypal.com pass:123456
  → PayPal lines where password contains 123456

  @yahoo.com, @hotmail.com ext:.co.uk
  → Yahoo or Hotmail with .co.uk extension

📋 QUEUE CONTROLS
  ⬆️ / ⬇️  move a pending job up or down
  🗑        cancel a specific pending job
  ⏹        cancel the currently running job

🗂 PRESETS
  Tap a preset to enqueue all its terms at once.
  /addpreset <name> <t1,t2,...>  — add preset
  /delpreset <name>              — remove preset

⚙️ COMMANDS
  /start  /help   main menu / this help
  /s <term>       search (ULP mode)
  /c <term>       search (COMBO mode)
  /l <term>       search (Lite mode)
  /queue          show queue
  /status         server status
  /ram            RAM usage
  /archives       file list
  /clean          delete all archive files
  /clearhistory   clear search history
  /addpreset <name> <terms>
  /delpreset <name>
""".strip()

# ════════════════════════════════════════════════════════════
# SCREEN BUILDERS
# ════════════════════════════════════════════════════════════
def _screen_main():
    with RUNNING_LOCK:
        job = RUNNING_JOB
    with QUEUE_LOCK:
        q = len(QUEUE_LIST)
    if job:
        lbl = "ULP" if job["mode"] == "ulp" else ("COMBO" if job["mode"] == "combo" else "LITE")
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
        lbl = "ULP" if job["mode"] == "ulp" else ("COMBO" if job["mode"] == "combo" else "LITE")
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
        lbl = "ULP" if job["mode"] == "ulp" else ("COMBO" if job["mode"] == "combo" else "LITE")
        lines.append(f"🟢  Running : [{lbl}] {job['term']}")
    else:
        lines.append("⚪  Idle — no job running")
    if pending:
        lines.append(f"\n📌  Pending ({len(pending)}):")
        for i, j in enumerate(pending, 1):
            lbl = "ULP" if j["mode"] == "ulp" else ("COMBO" if j["mode"] == "combo" else "LITE")
            lines.append(f"   {i}.  [{lbl}]  {j['term']}")
    else:
        lines.append("\n📌  Queue is empty")
    return "\n".join(lines), _kb_queue_screen(job, pending)

def _screen_archives(page=0):
    files = _list_archive_files()
    total_pages = max(1, (len(files) + ARCHIVES_PAGE_SIZE - 1) // ARCHIVES_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    if not files:
        return ("📦  Archives\n―――――――――――――\n  No result files saved yet.", KB_BACK)
    total_size = sum(f.stat().st_size for f in files)
    start = page * ARCHIVES_PAGE_SIZE
    page_files = files[start:start + ARCHIVES_PAGE_SIZE]
    lines = ["📦  Archives", f"  {len(files)} files  ·  {_fmt_bytes(total_size)} total", ""]
    for i, f in enumerate(page_files, start + 1):
        badge = "🔑" if f.name.startswith("COMBO") else "📄"
        lines.append(f"  {i}.  {badge} {f.name}")
        lines.append(f"       {_fmt_bytes(f.stat().st_size)}  ·  "
                     f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(f.stat().st_mtime))}")
        lines.append("")
    lines.append(f"📃 Page {page+1} of {total_pages}  ·  Tap a file to download it")
    return "\n".join(lines), _kb_archives(files, page)

def _screen_presets():
    names = _preset_names()
    if not names:
        text = "🗂  Presets\n―――――――――――――\nNo presets defined.\nUse /addpreset <name> <t1,t2,...>"
        return text, KB_BACK
    lines = ["🗂  Presets", "―――――――――――――", "Tap a preset to enqueue all its terms:", ""]
    for name in names:
        terms = _preset_terms(name)
        lines.append(f"  {name}")
        lines.append(f"    {', '.join(terms)}")
        lines.append("")
    return "\n".join(lines), _kb_presets()

def _screen_help():
    return HELP_TEXT, _kb([("🔙 Back", "nav:main")])

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
    hint = "\n\nRecent (tap to reuse):" if history else ""
    text = (
        f"🔍  Search\n"
        f"―――――――――――――\n"
        f"Type a term and send it.\n"
        f"Supports dork syntax — tap ❓ Help for full reference.\n"
        f"Examples:\n"
        f"  netflix.com\n"
        f"  amazon.com user:@gmail.com -free\n"
        f"  site:paypal.com ext:.fr\n"
        f"  netflix.com, hulu.com, disney.com{hint}"
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

def _show_mode_select(chat_id, raw_term, edit_msg_id=None):
    _add_history(chat_id, raw_term)
    dork = parse_dork(raw_term)
    terms_display = ", ".join(dork["terms"]) if dork["terms"] else raw_term
    dork_line = f"\n  🎯 Filters: {dork['display']}" if dork["display"] else ""
    text = (
        f"🔍  Query: {raw_term}\n"
        f"―――――――――――――\n"
        f"  Terms : {terms_display}{dork_line}\n\n"
        f"Select search mode:"
    )
    _set_last_screen(chat_id, "mode")
    kb = _kb_mode(raw_term)
    if edit_msg_id:
        edit_message(chat_id, edit_msg_id, text, reply_markup=kb)
        _store_nav(chat_id, edit_msg_id)
    else:
        _delete_nav(chat_id)
        mid = send_message(chat_id, text, reply_markup=kb)
        _store_nav(chat_id, mid)
    set_state(chat_id, ST_IDLE)

# ════════════════════════════════════════════════════════════
# INLINE QUERY HANDLER
# ════════════════════════════════════════════════════════════
def handle_inline_query(inline_query_id, from_user_id, query):
    if from_user_id not in ALLOWED_CHAT_IDS:
        answer_inline(inline_query_id, [])
        return
    query = query.strip()
    if not query:
        history = _get_history(from_user_id)
        results = []
        for i, term in enumerate(history):
            for mode, badge in (("ulp", "📄 ULP"), ("combo", "🔑 COMBO"), ("lite", "⚡ Lite")):
                results.append({
                    "type": "article",
                    "id": f"hist_{i}_{mode}",
                    "title": f"{badge} — {term}",
                    "description": "Recent search · tap to enqueue",
                    "input_message_content": {
                        "message_text": f"/{'l' if mode=='lite' else ('c' if mode=='combo' else 's')} {term}"
                    },
                })
        answer_inline(inline_query_id, results[:10])
        return
    results = [
        {"type": "article", "id": "ulp",   "title": f"📄 ULP — {query}",
         "description": "Full matched lines, deduped",
         "input_message_content": {"message_text": f"/s {query}"}},
        {"type": "article", "id": "combo", "title": f"🔑 COMBO — {query}",
         "description": "Clean user:pass pairs only",
         "input_message_content": {"message_text": f"/c {query}"}},
        {"type": "article", "id": "lite",  "title": f"⚡ Lite — {query}",
         "description": "Sample 10 from first 1000 hits",
         "input_message_content": {"message_text": f"/l {query}"}},
    ]
    answer_inline(inline_query_id, results)

# ════════════════════════════════════════════════════════════
# FILE DELIVERY
# ════════════════════════════════════════════════════════════
_SPLIT_CHUNK = 64 * 1024

def _send_document_with_retry(chat_id, part_path, caption, part_num, total_parts, msg_id):
    backoff = UPLOAD_RETRY_BACKOFF
    for attempt in range(1, UPLOAD_MAX_RETRIES + 1):
        edit_message(chat_id, msg_id,
            f"⬆️  Uploading part {part_num}/{total_parts}"
            f" ({_fmt_bytes(os.path.getsize(part_path))})"
            + (f"  — attempt {attempt}/{UPLOAD_MAX_RETRIES}" if attempt > 1 else "") + "...")
        ok, err = send_document(chat_id, str(part_path), caption=caption)
        if ok:
            return True, ""
        if attempt < UPLOAD_MAX_RETRIES:
            time.sleep(backoff)
            backoff *= 2
    return False, err

def _split_and_send(chat_id, file_path, caption, msg_id):
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
    failed_parts = []
    try:
        for i, part in enumerate(part_paths, 1):
            ok, err = _send_document_with_retry(
                chat_id, part,
                caption=f"{caption} — part {i}/{total_parts}",
                part_num=i, total_parts=total_parts,
                msg_id=msg_id)
            if not ok:
                failed_parts.append((i, err))
    finally:
        for p in part_paths:
            try:
                p.unlink()
            except Exception:
                pass
    if failed_parts:
        fail_lines = "\n".join(f"  ✗ Part {n}/{total_parts}: {e}" for n, e in failed_parts)
        sent = total_parts - len(failed_parts)
        edit_message(chat_id, msg_id,
            f"⚠️  Delivered {sent}/{total_parts} parts.\nFailed parts:\n{fail_lines}")
        return False
    return True

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
        f"⬆️  Preparing: {f.name}\n  Size: {_fmt_bytes(f.stat().st_size)}")
    if status_id is None:
        return
    deliver_file(chat_id, str(f), "Archive", f.stem, status_id)
    edit_message(chat_id, status_id,
        f"✅  Sent: {f.name}",
        reply_markup=_kb([("📦 Back to Archives", "nav:archives:0"), ("🏠 Home", "nav:main")]))

# ════════════════════════════════════════════════════════════
# BUILD FALCON COMMAND  (shared by all modes)
# ════════════════════════════════════════════════════════════
def _build_falcon_cmd(job):
    dork   = job.get("dork", {})
    terms  = job.get("terms_list") or [job["term"]]
    mode   = job["mode"]
    cmd = [PYTHON_BIN, FALCON_SCRIPT,
           "--source", SOURCE_DIR,
           "--out",    OUT_DIR,
           "--mode",   mode]
    if len(terms) == 1:
        cmd += ["--term", terms[0]]
    else:
        cmd += ["--terms", ",".join(terms)]
    if mode == "lite":
        cmd += ["--limit", "1000"]
    if dork.get("dork_domain"): cmd += ["--dork-domain", dork["dork_domain"]]
    if dork.get("dork_user"):   cmd += ["--dork-user",   dork["dork_user"]]
    if dork.get("dork_pass"):   cmd += ["--dork-pass",   dork["dork_pass"]]
    if dork.get("dork_ext"):    cmd += ["--dork-ext",    dork["dork_ext"]]
    if dork.get("dork_not"):    cmd += ["--dork-not",    dork["dork_not"]]
    return cmd

# ════════════════════════════════════════════════════════════
# CANCEL WATCHER
# ════════════════════════════════════════════════════════════
def _cancel_watcher(proc, chat_id, msg_id):
    while proc.poll() is None:
        if CANCEL_EVENT.is_set():
            try:
                _kill_running_proc()
            except Exception:
                pass
            edit_message(chat_id, msg_id,
                "⏹  Cancelling…\n―――――――――――――\nWaiting for process to stop.",
                reply_markup=None)
            return
        time.sleep(0.25)

# ════════════════════════════════════════════════════════════
# FALCON WORKER  — normal modes (ulp / combo)
# ════════════════════════════════════════════════════════════
def run_falcon(chat_id, job):
    global RUNNING_JOB
    term  = job["term"]
    mode  = job["mode"]
    dork  = job.get("dork", {})
    terms = job.get("terms_list") or [term]

    safe_name = re.sub(r"[^\w\-\.]", "_", "_".join(terms)[:60])
    label = "ULP" if mode == "ulp" else "COMBO"
    out_file = os.path.join(OUT_DIR,
        f"ULP_{safe_name}.txt" if mode == "ulp" else f"COMBO_LP_{safe_name}.txt")

    with RUNNING_LOCK:
        RUNNING_JOB = job
    CANCEL_EVENT.clear()

    display_term = ", ".join(terms) if len(terms) > 1 else term
    dork_line = f"\n  🎯 {dork.get('display','')}" if dork.get("display") else ""

    msg_id = send_message(chat_id,
        f"🔎  [{label}]  {display_term}{dork_line}\n"
        f"―――――――――――――\n⏳  Queuing process…",
        reply_markup=_kb([("⏹ Cancel", "do:cancel")]))
    if msg_id is None:
        with RUNNING_LOCK:
            RUNNING_JOB = None
        return

    cmd  = _build_falcon_cmd(job)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            bufsize=1, text=True, errors="ignore",
                            start_new_session=True)
    _set_running_proc(proc)

    watcher = threading.Thread(target=_cancel_watcher, args=(proc, chat_id, msg_id), daemon=True)
    watcher.start()

    last_text = ""; last_edit = 0.0; cancelled = False; done_stats = None
    job_start = time.time(); phase2_start = None; started_shown = False
    _p1_max_frac = 0.0; _p2_max_frac = 0.0

    try:
        for line in proc.stdout:
            if CANCEL_EVENT.is_set():
                cancelled = True
                break
            line = line.strip()
            now  = time.time()
            m    = PROGRESS_RE.search(line)
            d    = DONE_RE.search(line)
            wall_elapsed  = now - job_start
            edit_interval = EDIT_INTERVAL_FAST if wall_elapsed < EDIT_FAST_WINDOW else EDIT_INTERVAL_NORM

            if not started_shown:
                edit_message(chat_id, msg_id,
                    f"🔎  [{label}]  {display_term}{dork_line}\n"
                    f"―――――――――――――\n🟡  Starting…",
                    reply_markup=_kb([("⏹ Cancel", "do:cancel")]))
                started_shown = True

            if m:
                phase            = int(m.group(1))
                reported_elapsed = float(m.group(5))
                if phase == 1:
                    hits = int(m.group(2) or 0)
                    bar, _p1_max_frac = _progress_bar(
                        elapsed_s=reported_elapsed, done=False, phase=1, max_frac=_p1_max_frac)
                    text = (
                        f"🔎  [{label}]  {display_term}{dork_line}\n"
                        f"―――――――――――――\n"
                        f"🔵  Phase 1 — Scanning\n"
                        f"{bar}\n"
                        f"  Hits    : {hits:,}\n"
                        f"  Elapsed : {reported_elapsed:.1f}s"
                    )
                else:
                    if phase2_start is None:
                        phase2_start = now
                    combos     = int(m.group(4) or 0)
                    p2_elapsed = now - phase2_start
                    bar, _p2_max_frac = _progress_bar(
                        elapsed_s=reported_elapsed, done=False, phase=2,
                        phase2_elapsed=p2_elapsed, max_frac=_p2_max_frac)
                    text = (
                        f"🔎  [{label}]  {display_term}{dork_line}\n"
                        f"―――――――――――――\n"
                        f"🟢  Phase 2 — Extracting\n"
                        f"{bar}\n"
                        f"  Combos  : {combos:,}\n"
                        f"  Elapsed : {reported_elapsed:.1f}s"
                    )
                if now - last_edit >= edit_interval and text != last_text:
                    edit_message(chat_id, msg_id, text,
                                 reply_markup=_kb([("⏹ Cancel", "do:cancel")]))
                    last_edit = now; last_text = text

            elif d:
                hits        = int(d.group(1))
                ulp         = int(d.group(2))
                combos      = int(d.group(3))
                elapsed_s   = float(d.group(4))
                ulp_bytes   = int(d.group(5) or 0)
                combo_bytes = int(d.group(6) or 0)
                done_stats  = (hits, ulp, combos, elapsed_s, ulp_bytes, combo_bytes)
                bar, _ = _progress_bar(elapsed_s, done=True)
                edit_message(chat_id, msg_id,
                    f"📊  [{label}]  {display_term}\n"
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
    finally:
        with RUNNING_LOCK:
            RUNNING_JOB = None
        _clear_running_proc()

    watcher.join(timeout=2)

    if CANCEL_EVENT.is_set() or cancelled:
        edit_message(chat_id, msg_id,
            f"⛔  Cancelled: {display_term}\n―――――――――――――",
            reply_markup=_kb([("🏠 Home", "nav:main")]))
        return

    if proc.returncode != 0:
        edit_message(chat_id, msg_id, f"❌  Falcon exited {proc.returncode}",
                     reply_markup=_kb([("🏠 Home", "nav:main")]))
        return

    if not os.path.exists(out_file) or os.path.getsize(out_file) == 0:
        edit_message(chat_id, msg_id, f"⚠️  No results for: {display_term}",
                     reply_markup=_kb([("🏠 Home", "nav:main")]))
        return

    deliver_file(chat_id, out_file, label, display_term, msg_id)

    if done_stats:
        hits, ulp, combos, elapsed_s, ulp_bytes, combo_bytes = done_stats
        fsize       = os.path.getsize(out_file) if os.path.exists(out_file) else 0
        total_parts = max(1, (fsize + TG_MAX_BYTES - 1) // TG_MAX_BYTES)
        lines = [
            f"✅  Done — {display_term}", "―――――――――――――",
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
                          ("🏠 Home",         "nav:main")]))

# ════════════════════════════════════════════════════════════
# LITE MODE RUNNER
# ════════════════════════════════════════════════════════════
def run_falcon_lite(chat_id, job):
    global RUNNING_JOB
    term  = job["term"]
    dork  = job.get("dork", {})
    terms = job.get("terms_list") or [term]
    display_term = ", ".join(terms) if len(terms) > 1 else term
    dork_line = f"\n  🎯 {dork.get('display','')}" if dork.get("display") else ""

    with RUNNING_LOCK:
        RUNNING_JOB = job
    CANCEL_EVENT.clear()

    msg_id = send_message(chat_id,
        f"⚡  [LITE]  {display_term}{dork_line}\n"
        f"―――――――――――――\n⏳  Scanning up to 1000 hits…",
        reply_markup=_kb([("⏹ Cancel", "do:cancel")]))
    if msg_id is None:
        with RUNNING_LOCK:
            RUNNING_JOB = None
        return

    cmd  = _build_falcon_cmd(job)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            bufsize=1, text=True, errors="ignore",
                            start_new_session=True)
    _set_running_proc(proc)
    watcher = threading.Thread(target=_cancel_watcher, args=(proc, chat_id, msg_id), daemon=True)
    watcher.start()

    lite_lines   = []
    hits_total   = 0
    sampled      = 0
    cancelled    = False

    try:
        for line in proc.stdout:
            if CANCEL_EVENT.is_set():
                cancelled = True
                break
            line = line.strip()
            ml = LITE_LINE_RE.match(line)
            md = LITE_DONE_RE.match(line)
            if ml:
                lite_lines.append(ml.group(1))
            elif md:
                hits_total = int(md.group(1))
                sampled    = int(md.group(2))
        proc.wait()
    except Exception as e:
        edit_message(chat_id, msg_id, f"❌  Error: {e}")
    finally:
        with RUNNING_LOCK:
            RUNNING_JOB = None
        _clear_running_proc()

    watcher.join(timeout=2)

    if CANCEL_EVENT.is_set() or cancelled:
        edit_message(chat_id, msg_id, f"⛔  Cancelled: {display_term}",
                     reply_markup=_kb([("🏠 Home", "nav:main")]))
        return

    if not lite_lines:
        edit_message(chat_id, msg_id, f"⚠️  No results for: {display_term}",
                     reply_markup=_kb([("🏠 Home", "nav:main")]))
        return

    # Update status message with summary
    edit_message(chat_id, msg_id,
        f"⚡  [LITE]  {display_term}\n"
        f"―――――――――――――\n"
        f"  Scanned : {hits_total:,} hits\n"
        f"  Sampled : {sampled}\n"
        f"  Sending results below…")

    # Send each result as individual message
    for result_line in lite_lines:
        # Format based on LITE_FORMAT setting
        if LITE_FORMAT == "combo":
            # Try to extract user:pass only
            # Simple extraction: take last two colon-parts
            parts = result_line.split(":")
            if len(parts) >= 2:
                formatted = ":".join(parts[-2:]) if len(parts) > 2 else result_line
            else:
                formatted = result_line
        else:
            formatted = result_line
        send_message(chat_id, formatted)
        time.sleep(0.05)  # small delay to avoid flood

    send_message(chat_id,
        f"✅  Lite done — {display_term}\n"
        f"  {len(lite_lines)} result(s) sent above.",
        reply_markup=_kb(
            [("🔍 Search Again", "nav:search"),
             ("🏠 Home",         "nav:main")]))

# ════════════════════════════════════════════════════════════
# QUEUE WORKER
# ════════════════════════════════════════════════════════════
def queue_worker():
    while True:
        job = JOB_QUEUE.get()
        with QUEUE_LOCK:
            QUEUE_LIST[:] = [j for j in QUEUE_LIST if j["id"] != job["id"]]
        try:
            if job["mode"] == "lite":
                run_falcon_lite(job["chat_id"], job)
            else:
                run_falcon(job["chat_id"], job)
        except Exception:
            print(f"[queue_worker] {traceback.format_exc()}")
        finally:
            JOB_QUEUE.task_done()

# ════════════════════════════════════════════════════════════
# ENQUEUE
# ════════════════════════════════════════════════════════════
def enqueue(chat_id, raw_term, mode):
    dork  = parse_dork(raw_term)
    terms = dork["terms"] or [raw_term]
    job = {
        "id":         str(uuid.uuid4()),
        "chat_id":    chat_id,
        "term":       raw_term,
        "terms_list": terms,
        "mode":       mode,
        "dork":       dork,
    }
    with QUEUE_LOCK:
        pos = len(QUEUE_LIST) + 1
        QUEUE_LIST.append(job)
    JOB_QUEUE.put(job)
    with RUNNING_LOCK:
        busy = RUNNING_JOB is not None
    if busy:
        lbl = "ULP" if mode == "ulp" else ("COMBO" if mode == "combo" else "LITE")
        send_message(chat_id,
            f"📋  Queued at position {pos}\n"
            f"  [{lbl}]  {raw_term}\n"
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

    elif data == "nav:help":
        _show_screen(chat_id, _screen_help, "help", edit_msg_id=msg_id)

    elif data == "nav:presets":
        _show_screen(chat_id, _screen_presets, "presets", edit_msg_id=msg_id)

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
        elif screen == "presets":
            _show_screen(chat_id, _screen_presets, "presets", edit_msg_id=msg_id)
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
        term = data[3:]
        _show_mode_select(chat_id, term, edit_msg_id=msg_id)

    elif data.startswith("preset:"):
        name  = data[7:]
        terms = _preset_terms(name)
        if terms:
            raw = ", ".join(terms)
            _show_mode_select(chat_id, raw, edit_msg_id=msg_id)
        else:
            answer_callback(callback_id, text="⚠️ Preset not found")

    elif data.startswith("run:"):
        import base64
        parts = data.split(":", 2)
        mode  = parts[1]
        try:
            raw_term = base64.urlsafe_b64decode(parts[2].encode()).decode()
        except Exception:
            raw_term = parts[2]
        enqueue(chat_id, raw_term, mode)
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
            _kill_running_proc()
            answer_callback(callback_id, text="⏹ Cancelling…")
        else:
            answer_callback(callback_id, text="ℹ️ Nothing running")

    # ── Queue reorder / per-job cancel ───────────────────────
    elif data.startswith("qup:"):
        jid = data[4:]
        with QUEUE_LOCK:
            idx = next((i for i, j in enumerate(QUEUE_LIST) if j["id"] == jid), -1)
            if idx > 0:
                QUEUE_LIST[idx], QUEUE_LIST[idx-1] = QUEUE_LIST[idx-1], QUEUE_LIST[idx]
        _show_screen(chat_id, _screen_queue, "queue", edit_msg_id=msg_id)

    elif data.startswith("qdown:"):
        jid = data[6:]
        with QUEUE_LOCK:
            idx = next((i for i, j in enumerate(QUEUE_LIST) if j["id"] == jid), -1)
            if 0 <= idx < len(QUEUE_LIST) - 1:
                QUEUE_LIST[idx], QUEUE_LIST[idx+1] = QUEUE_LIST[idx+1], QUEUE_LIST[idx]
        _show_screen(chat_id, _screen_queue, "queue", edit_msg_id=msg_id)

    elif data.startswith("qcancel:"):
        jid = data[8:]
        with QUEUE_LOCK:
            before = len(QUEUE_LIST)
            QUEUE_LIST[:] = [j for j in QUEUE_LIST if j["id"] != jid]
            removed = before - len(QUEUE_LIST)
        answer_callback(callback_id,
            text=("🗑 Job removed from queue" if removed else "ℹ️ Job not found"))
        _show_screen(chat_id, _screen_queue, "queue", edit_msg_id=msg_id)

    elif data == "do:clean":
        files   = _list_archive_files()
        total   = sum(f.stat().st_size for f in files)
        deleted = 0
        for f in files:
            try:
                f.unlink(); deleted += 1
            except Exception:
                pass
        text, kb = _screen_archives(page=0)
        edit_message(chat_id, msg_id,
            f"🧹  Cleaned {deleted} file(s) — freed {_fmt_bytes(total)}\n\n" + text,
            reply_markup=kb)

    elif data == "do:clear_history":
        _clear_history(chat_id)
        _show_search_prompt(chat_id, edit_msg_id=msg_id)

# ════════════════════════════════════════════════════════════
# MESSAGE HANDLER
# ════════════════════════════════════════════════════════════
def handle_message(chat_id, text, msg_id):
    text  = text.strip()
    state = get_state(chat_id)

    if text in ("/start",):
        set_state(chat_id, ST_IDLE)
        _show_main(chat_id)
        return

    if text in ("/help",):
        _show_screen(chat_id, _screen_help, "help")
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
    if text.startswith("/l "):
        t = text[3:].strip()
        if t:
            enqueue(chat_id, t, "lite")
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
                f.unlink(); deleted += 1
            except Exception:
                pass
        send_message(chat_id, f"🧹  Cleaned {deleted} file(s) — freed {_fmt_bytes(total)}")
        return
    if text == "/clearhistory":
        _clear_history(chat_id)
        send_message(chat_id, "🗑  Search history cleared.")
        return

    # /addpreset <name> <t1,t2,...>
    if text.startswith("/addpreset "):
        rest  = text[11:].strip()
        parts = rest.split(None, 1)
        if len(parts) == 2:
            pname  = parts[0].strip()
            pterms = [t.strip() for t in parts[1].split(",") if t.strip()]
            if pterms:
                _add_preset(pname, pterms)
                send_message(chat_id,
                    f"✅  Preset '{pname}' saved with {len(pterms)} term(s):\n"
                    f"  {', '.join(pterms)}")
            else:
                send_message(chat_id, "⚠️  No valid terms provided.")
        else:
            send_message(chat_id, "Usage: /addpreset <name> <t1,t2,...>")
        return

    # /delpreset <name>
    if text.startswith("/delpreset "):
        pname = text[11:].strip()
        removed = _del_preset(pname)
        if removed is not None:
            send_message(chat_id, f"🗑  Preset '{pname}' deleted.")
        else:
            send_message(chat_id, f"⚠️  Preset '{pname}' not found.")
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
    print(f"Lite format: {LITE_FORMAT}")
    print(f"Edit interval: fast={EDIT_INTERVAL_FAST}s / norm={EDIT_INTERVAL_NORM}s")

    backoff = 2.0
    while True:
        try:
            r = api_get("getUpdates", params={
                "offset":          LAST_UPDATE_ID + 1,
                "timeout":         25,
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

                iq = upd.get("inline_query")
                if iq:
                    handle_inline_query(iq["id"], iq["from"]["id"], iq.get("query", ""))
                    continue

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

                msg     = upd.get("message", {})
                chat_id = msg.get("chat", {}).get("id")
                text    = msg.get("text", "")
                mid     = msg.get("message_id")
                if chat_id in ALLOWED_CHAT_IDS and text:
                    handle_message(chat_id, text, mid)

        time.sleep(0.3)

if __name__ == "__main__":
    main()
