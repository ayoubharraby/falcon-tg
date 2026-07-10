#!/usr/bin/env python3
"""
FALCON PARSE v2 — fast CLI credential extraction (no GUI)

Usage:
  python3 falcon_parse.py --term "netflix.com" --source /data/textset --out /data/archives
  python3 falcon_parse.py --term "@gmail.com" --source /path/to/file.txt --out ./results --mode combo

Outputs (in --out dir):
  ULP_{term}.txt       cleaned matched lines, deduped
  COMBO_LP_{term}.txt  clean user:pass pairs, deduped

Progress:
  Emits machine-readable lines to stdout of the form:
    PROGRESS phase=1 hits=<n> ulp=<n> elapsed=<s>
    PROGRESS phase=2 combos=<n> elapsed=<s>
    DONE hits=<n> ulp=<n> combos=<n> elapsed=<s>
  A wrapper (e.g. a Telegram bot) can tail stdout and parse these lines
  to render a live progress UI.
"""
import argparse, os, re, sys, time, shutil, subprocess
import concurrent.futures, multiprocessing
from pathlib import Path

# ---------- cleanup / junk rejection ----------

# Promo / reseller / ad tails appended after real credential data.
# Anything matching these markers -> the tail is stripped (or line dropped
# entirely if nothing usable remains before the marker).
_PROMO_MARKERS = [
    r'you can buy dm',
    r'free ulp\s*/?\s*logs?',
    r'free\s+cloud',
    r'best\s+free\s+cloud',
    r'monkeybasecloud\w*',
    r'txt_aliens\w*',
    r'azulcloud\w*',
    r't\.me/\S+',
    r'@\w+cloud\w*',
    r'chromeprofile\d*\(?[\d.]*\)?',
    r'new\s*link!*',
]
_PROMO_RE = re.compile(
    r'\s*(?:[┃│⇒ᚲ║¦]\s*.*|' + '|'.join(_PROMO_MARKERS) + r').*$',
    re.IGNORECASE | re.UNICODE,
)

# Mojibake / stray unicode separator junk commonly appended at line ends
_MOJIBAKE_RE = re.compile(r'[\u200b\u2063\ufeff]|â[\x80-\xbf]?\S{0,3}', re.UNICODE)

_TAB_RE            = re.compile(r'\t{2,}')
_RE_BRACKET_PREFIX = re.compile(r'^.*?\]\s*:?')
_RE_JUNK_PREFIX    = re.compile(r'^[\|\+\-\s>]+')
_RE_PORT_ONLY      = re.compile(r'^\d{2,5}$')
_PERCENT_RE        = re.compile(r'%40|%3[Ff]|%3[Dd]')
_PERCENT_MAP       = {"%40": "@", "%3F": "?", "%3f": "?", "%3D": "=", "%3d": "="}
_URL_SCHEMES       = frozenset((
    "http", "https", "ftp", "android", "chrome",
    "javascript", "file", "sftp", "void(0)"
))
_NAKED_URL_RE  = re.compile(r'^(?:https?://)?[\w\-]+\.[\w\-]+(?:\.[\w\-]+)*(?:/[^\s]*)?$')
_PATH_TOKEN_RE = re.compile(
    r'^/?(?:login|register|auth|chatgpt|ai|list|api|pastel|chatshare)$',
    re.IGNORECASE,
)

def _url_decode(s: str) -> str:
    if "%" not in s:
        return s
    return _PERCENT_RE.sub(lambda m: _PERCENT_MAP[m.group()], s)

def clean_raw_line(raw: str):
    """Strip promo/junk tails and mojibake from a raw hit line.
    Returns cleaned line, or None if nothing useful remains."""
    raw = raw.strip()
    if not raw:
        return None
    raw = _MOJIBAKE_RE.sub('', raw)
    raw = _PROMO_RE.sub('', raw).strip()
    raw = _TAB_RE.split(raw)[0].strip()
    if not raw:
        return None
    # reject lines that are just separators / empty fields e.g. "host::"
    stripped_colons = raw.replace(':', '').replace('/', '').strip()
    if not stripped_colons:
        return None
    return raw

def slice_line(raw: str):
    raw = clean_raw_line(raw)
    if not raw:
        return None
    if raw.lower().startswith("http") and " " in raw:
        raw = raw.split(" ", 1)[1].strip()
    raw = _url_decode(raw)
    raw = _RE_BRACKET_PREFIX.sub("", raw, count=1)
    raw = _RE_JUNK_PREFIX.sub("", raw, count=1)
    if not raw:
        return None
    if "?q=" in raw:
        raw = raw.split("?q=", 1)[1]
    raw = re.sub(r'\s*:\s*', ':', raw)
    if ":" not in raw and "|" in raw:
        raw = raw.replace("|", ":")
    parts = [p.strip() for p in raw.split(":") if p.strip()]
    if len(parts) < 2:
        return None
    while parts:
        first = parts[0]; fl = first.lower()
        if (fl in _URL_SCHEMES
                or (bool(_NAKED_URL_RE.match(first)) and "@" not in first)
                or bool(_PATH_TOKEN_RE.match(first))
                or first.startswith("//")
                or bool(_RE_PORT_ONLY.match(first))):
            parts.pop(0)
        else:
            break
    if len(parts) < 2:
        return None
    if len(parts) == 2:
        user, pw = parts[0], parts[1]
    else:
        email_idx = next((i for i, p in enumerate(parts[:-1]) if "@" in p and not p.startswith("//")), -1)
        if email_idx != -1:
            pw = ":".join(parts[email_idx + 1:])
            user = parts[email_idx] if pw else None
        else:
            start = 0
            for i, p in enumerate(parts[:-1]):
                pl = p.lower()
                if (pl in _URL_SCHEMES or p.startswith("//")
                        or bool(_RE_PORT_ONLY.match(p))
                        or ("." in p and " " not in p)):
                    continue
                start = i
                break
            pw = ":".join(parts[start + 1:])
            user = parts[start] if pw else None
        if user is None or not pw:
            return None
    user = user.strip(); pw = pw.strip()
    if not user or not pw:
        return None
    # reject degenerate combos
    if user.lower() in ("null", "none", "n/a", "-") or pw.lower() in ("null", "none", "n/a", "-"):
        return None
    return f"{user}:{pw}"

def _slice_batch(lines):
    out = []
    for ln in lines:
        r = slice_line(ln)
        if r:
            out.append(r)
    return out

# ---------- Phase 1: search ----------

def rg_binary():
    return shutil.which("rg") or ""

def search_with_rg_dir(rg_exe, term, source, cpu_count):
    """Let ripgrep walk the directory itself (faster than pre-listing files
    with Path.rglob in Python for very large trees)."""
    cmd = [rg_exe, "--no-heading", "--no-line-number", "--no-filename",
           "--smart-case", "-a", "-F", "-j", str(max(1, cpu_count)), term, source]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             bufsize=1 << 20, text=True, errors="ignore")
    for line in proc.stdout:
        line = line.rstrip("\n\r")
        if line:
            yield line
    proc.wait()

def collect_files(source: str):
    p = Path(source)
    if p.is_file():
        return [str(p)]
    if p.is_dir():
        return None  # let rg walk it directly — faster for huge trees
    print(f"[ERR] Source not found: {source}")
    sys.exit(1)

def _grep_worker(args):
    path, term_lower = args
    hits = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if term_lower in line.lower():
                    hits.append(line.rstrip("\n\r"))
    except Exception:
        pass
    return hits

def search_pure_python(term, files, cpu_count):
    term_lower = term.lower()
    with concurrent.futures.ProcessPoolExecutor(max_workers=cpu_count) as pool:
        for hits in pool.map(_grep_worker, [(f, term_lower) for f in files]):
            for h in hits:
                yield h

# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description="FALCON PARSE v2 — fast credential extraction")
    ap.add_argument("--term", required=True, help="Search term (e.g. netflix.com)")
    ap.add_argument("--source", required=True, help="File or directory to search")
    ap.add_argument("--out", required=True, help="Output directory")
    ap.add_argument("--mode", choices=["both", "ulp", "combo"], default="both",
                     help="Which output(s) to generate")
    args = ap.parse_args()

    cpu_count = os.cpu_count() or 4
    os.makedirs(args.out, exist_ok=True)
    safe_term = re.sub(r"[^\w\-\.]", "_", args.term)
    ulp_path = os.path.join(args.out, f"ULP_{safe_term}.txt")
    combo_path = os.path.join(args.out, f"COMBO_LP_{safe_term}.txt")

    print(f"[INFO] Source     : {args.source}")
    print(f"[INFO] Term       : {args.term}")
    print(f"[INFO] Mode       : {args.mode}")
    print(f"[INFO] CPUs       : {cpu_count}")
    print(f"[INFO] ULP out    : {ulp_path}")
    print(f"[INFO] COMBO out  : {combo_path}")
    sys.stdout.flush()

    rg_exe = rg_binary()
    files = collect_files(args.source)

    if rg_exe:
        print(f"[OK]   Using ripgrep: {rg_exe}")
        if files is None:
            line_source = search_with_rg_dir(rg_exe, args.term, args.source, cpu_count)
        else:
            cmd = [rg_exe, "--no-heading", "--no-line-number", "--no-filename",
                   "--smart-case", "-a", "-F", "-j", str(min(4, cpu_count)), args.term] + files
            def _gen():
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                         bufsize=1 << 20, text=True, errors="ignore")
                for line in proc.stdout:
                    line = line.rstrip("\n\r")
                    if line:
                        yield line
                proc.wait()
            line_source = _gen()
    else:
        print("[WARN] ripgrep not found — falling back to slower pure-Python scan.")
        print("[WARN] For much faster runs: sudo apt install ripgrep")
        if files is None:
            files = [str(f) for f in Path(args.source).rglob("*") if f.is_file()]
        line_source = search_pure_python(args.term, files, cpu_count)

    seen_ulp = set()
    raw_lines = []
    hits = 0
    ulp_written = 0
    t0 = time.time()
    last_report = t0
    write_ulp = args.mode in ("both", "ulp")

    ulp_file = open(ulp_path, "ab", buffering=1 << 20) if write_ulp else None
    try:
        for raw in line_source:
            hits += 1
            cleaned = clean_raw_line(raw)
            if cleaned:
                raw_lines.append(cleaned)
                if write_ulp and cleaned not in seen_ulp:
                    seen_ulp.add(cleaned)
                    ulp_file.write((cleaned + "\n").encode("utf-8"))
                    ulp_written += 1
            now = time.time()
            if now - last_report >= 1.0:
                print(f"PROGRESS phase=1 hits={hits} ulp={ulp_written} elapsed={now-t0:.1f}")
                sys.stdout.flush()
                last_report = now
    finally:
        if ulp_file:
            ulp_file.close()

    print(f"PROGRESS phase=1 hits={hits} ulp={ulp_written} elapsed={time.time()-t0:.1f}")
    print(f"[OK]   Phase 1 done: {hits} raw hits, {ulp_written} unique cleaned ULP in {time.time()-t0:.1f}s")
    sys.stdout.flush()

    combos = 0
    write_combo = args.mode in ("both", "combo")
    if write_combo and raw_lines:
        print(f"[INFO] Phase 2: slicing {len(raw_lines)} lines across {cpu_count} cores...")
        sys.stdout.flush()
        t1 = time.time()
        chunk_size = max(5000, max(1, len(raw_lines) // cpu_count))
        chunks = [raw_lines[i:i+chunk_size] for i in range(0, len(raw_lines), chunk_size)]
        seen_combo = set()
        with open(combo_path, "ab", buffering=1 << 20) as combo_file:
            with concurrent.futures.ProcessPoolExecutor(max_workers=cpu_count) as pool:
                for batch in pool.map(_slice_batch, chunks):
                    for lp in batch:
                        if lp in seen_combo:
                            continue
                        seen_combo.add(lp)
                        combo_file.write((lp + "\n").encode("utf-8"))
                        combos += 1
                        if combos % 500 == 0:
                            print(f"PROGRESS phase=2 combos={combos} elapsed={time.time()-t1:.1f}")
                            sys.stdout.flush()
        print(f"PROGRESS phase=2 combos={combos} elapsed={time.time()-t1:.1f}")
        print(f"[OK]   Phase 2 done: {combos} unique COMBO LP written in {time.time()-t1:.1f}s")
    elif not raw_lines:
        print("[WARN] Phase 2 skipped — no hits found.")
    sys.stdout.flush()

    total = time.time() - t0
    print(f"DONE hits={hits} ulp={ulp_written} combos={combos} elapsed={total:.1f}")
    print(f"[DONE] ULP file   -> {ulp_path}")
    print(f"[DONE] COMBO file -> {combo_path}")
    sys.stdout.flush()

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
