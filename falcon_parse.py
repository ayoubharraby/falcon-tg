#!/usr/bin/env python3
"""
FALCON PARSE v3 — fast CLI credential extraction

Usage:
  python3 falcon_parse.py --term "netflix.com" --source /data/textset --out /data/archives
  python3 falcon_parse.py --term "@gmail.com" --source /path/to/file.txt --out ./results --mode combo

Outputs (in --out dir):
  ULP_{term}.txt       cleaned matched lines, deduped
  COMBO_LP_{term}.txt  clean user:pass pairs, deduped

Progress lines emitted to stdout (parsed by bot.py):
  PROGRESS phase=1 hits=<n> ulp=<n> elapsed=<s>
  PROGRESS phase=2 combos=<n> elapsed=<s>
  DONE hits=<n> ulp=<n> combos=<n> elapsed=<s>
"""
import argparse, os, re, sys, time, shutil, subprocess, tempfile
import concurrent.futures, multiprocessing
from pathlib import Path

# ---------- cleanup / junk rejection ----------

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
    r'\s*(?:[\u2503\u2502\u21d2\u16b2\u2551\xa6]\s*.*|' + '|'.join(_PROMO_MARKERS) + r').*$',
    re.IGNORECASE | re.UNICODE,
)
_MOJIBAKE_RE   = re.compile(r'[\u200b\u2063\ufeff]|\xc3[\x80-\xbf]?\S{0,3}', re.UNICODE)
_TAB_RE        = re.compile(r'\t{2,}')
_RE_BRACKET_PREFIX = re.compile(r'^.*?\]\s*:?')
_RE_JUNK_PREFIX    = re.compile(r'^[\|\+\-\s>]+')
_RE_PORT_ONLY      = re.compile(r'^\d{2,5}$')
_PERCENT_RE    = re.compile(r'%40|%3[Ff]|%3[Dd]')
_PERCENT_MAP   = {"%40": "@", "%3F": "?", "%3f": "?", "%3D": "=", "%3d": "="}
_URL_SCHEMES   = frozenset(("http","https","ftp","android","chrome","javascript","file","sftp","void(0)"))
_NAKED_URL_RE  = re.compile(r'^(?:https?://)?[\w\-]+\.[\w\-]+(?:\.[\w\-]+)*(?:/[^\s]*)?$')
_PATH_TOKEN_RE = re.compile(r'^/?(?:login|register|auth|chatgpt|ai|list|api|pastel|chatshare)$', re.IGNORECASE)

def _url_decode(s):
    if "%" not in s:
        return s
    return _PERCENT_RE.sub(lambda m: _PERCENT_MAP[m.group()], s)

def clean_raw_line(raw):
    raw = raw.strip()
    if not raw:
        return None
    raw = _MOJIBAKE_RE.sub('', raw)
    raw = _PROMO_RE.sub('', raw).strip()
    raw = _TAB_RE.split(raw)[0].strip()
    if not raw:
        return None
    stripped_colons = raw.replace(':', '').replace('/', '').strip()
    if not stripped_colons:
        return None
    return raw

def slice_line(raw):
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
            pw   = ":".join(parts[email_idx + 1:])
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
            pw   = ":".join(parts[start + 1:])
            user = parts[start] if pw else None
        if user is None or not pw:
            return None
    user = user.strip(); pw = pw.strip()
    if not user or not pw:
        return None
    if user.lower() in ("null","none","n/a","-") or pw.lower() in ("null","none","n/a","-"):
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

def search_with_rg(rg_exe, term, source, cpu_count):
    cmd = [rg_exe, "--no-heading", "--no-line-number", "--no-filename",
           "--smart-case", "-a", "-F", "-j", str(max(1, cpu_count)), term, source]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             bufsize=1 << 20, text=True, errors="ignore")
    for line in proc.stdout:
        line = line.rstrip("\n\r")
        if line:
            yield line
    proc.wait()

def collect_files(source):
    p = Path(source)
    if p.is_file():
        return [str(p)]
    if p.is_dir():
        return None
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

# ---------- dedup via sort -u (disk-based, handles any size) ----------

def sort_dedup_file(src_path, dst_path):
    """
    Deduplicate src_path using GNU sort -u and write result to dst_path.
    This is disk-based — RAM usage is O(sort_buffer) not O(file_size).
    """
    with open(dst_path, "wb") as out:
        subprocess.run(
            ["sort", "-u", "-o", "/dev/stdout", src_path],
            stdout=out, check=True
        )

# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description="FALCON PARSE v3")
    ap.add_argument("--term",   required=True)
    ap.add_argument("--source", required=True)
    ap.add_argument("--out",    required=True)
    ap.add_argument("--mode",   choices=["both","ulp","combo"], default="both")
    args = ap.parse_args()

    cpu_count = os.cpu_count() or 4
    os.makedirs(args.out, exist_ok=True)
    safe = re.sub(r"[^\w\-\.]", "_", args.term)
    ulp_path   = os.path.join(args.out, f"ULP_{safe}.txt")
    combo_path = os.path.join(args.out, f"COMBO_LP_{safe}.txt")

    # always overwrite — never append to stale results
    for p in (ulp_path, combo_path):
        if os.path.exists(p):
            os.remove(p)

    print(f"[INFO] Source : {args.source}")
    print(f"[INFO] Term   : {args.term}")
    print(f"[INFO] Mode   : {args.mode}")
    print(f"[INFO] CPUs   : {cpu_count}")
    sys.stdout.flush()

    rg_exe = rg_binary()
    files  = collect_files(args.source)

    if rg_exe:
        print(f"[OK]  ripgrep: {rg_exe}")
        line_source = search_with_rg(rg_exe, args.term, args.source, cpu_count)
    else:
        print("[WARN] ripgrep not found — pure-Python fallback (slower).")
        if files is None:
            files = [str(f) for f in Path(args.source).rglob("*") if f.is_file()]
        line_source = search_pure_python(args.term, files, cpu_count)

    sys.stdout.flush()

    # ── Phase 1: stream hits into a temp file (no RAM accumulation) ──────────
    write_ulp   = args.mode in ("both", "ulp")
    write_combo = args.mode in ("both", "combo")

    tmp_dir      = tempfile.mkdtemp(dir=args.out)
    raw_tmp_path = os.path.join(tmp_dir, "raw_hits.txt")

    hits        = 0
    t0          = time.time()
    last_report = t0

    try:
        with open(raw_tmp_path, "w", encoding="utf-8", errors="ignore", buffering=1 << 20) as raw_tmp:
            for raw in line_source:
                hits += 1
                cleaned = clean_raw_line(raw)
                if cleaned:
                    raw_tmp.write(cleaned + "\n")
                now = time.time()
                if now - last_report >= 1.0:
                    print(f"PROGRESS phase=1 hits={hits} ulp=0 elapsed={now-t0:.1f}")
                    sys.stdout.flush()
                    last_report = now

        print(f"PROGRESS phase=1 hits={hits} ulp=0 elapsed={time.time()-t0:.1f}")
        print(f"[OK]  Phase 1: {hits} raw hits in {time.time()-t0:.1f}s — deduplicating...")
        sys.stdout.flush()

        # ── dedup ULP via sort -u (disk-based, any file size) ────────────────
        ulp_written = 0
        if write_ulp and hits > 0:
            sort_dedup_file(raw_tmp_path, ulp_path)
            ulp_written = int(subprocess.check_output(["wc", "-l", ulp_path]).split()[0])
            print(f"[OK]  ULP dedup done: {ulp_written} unique lines")
            sys.stdout.flush()
        print(f"PROGRESS phase=1 hits={hits} ulp={ulp_written} elapsed={time.time()-t0:.1f}")
        sys.stdout.flush()

        # ── Phase 2: slice ULP lines into combos ──────────────────────────────
        combos = 0
        if write_combo and hits > 0:
            src_for_combo = ulp_path if write_ulp else raw_tmp_path
            combo_tmp_path = os.path.join(tmp_dir, "combo_raw.txt")

            print(f"[INFO] Phase 2: extracting combos from {ulp_written or hits} lines...")
            sys.stdout.flush()
            t1 = time.time()
            last_report2 = t1

            with open(src_for_combo, "r", encoding="utf-8", errors="ignore") as src, \
                 open(combo_tmp_path, "w", encoding="utf-8", errors="ignore", buffering=1 << 20) as ctmp:

                chunk = []
                chunk_size = max(5000, max(1, (ulp_written or hits) // cpu_count))

                with concurrent.futures.ProcessPoolExecutor(max_workers=cpu_count) as pool:
                    futures = []
                    for line in src:
                        chunk.append(line.rstrip("\n"))
                        if len(chunk) >= chunk_size:
                            futures.append(pool.submit(_slice_batch, chunk))
                            chunk = []
                        # drain completed futures to avoid memory buildup
                        done = [f for f in futures if f.done()]
                        for f in done:
                            for lp in f.result():
                                ctmp.write(lp + "\n")
                                combos += 1
                            futures.remove(f)
                        now = time.time()
                        if now - last_report2 >= 2.0:
                            print(f"PROGRESS phase=2 combos={combos} elapsed={now-t1:.1f}")
                            sys.stdout.flush()
                            last_report2 = now
                    # flush remaining chunk
                    if chunk:
                        futures.append(pool.submit(_slice_batch, chunk))
                    for f in concurrent.futures.as_completed(futures):
                        for lp in f.result():
                            ctmp.write(lp + "\n")
                            combos += 1

            # dedup combos via sort -u
            print(f"[OK]  Phase 2 raw: {combos} combos — deduplicating...")
            sys.stdout.flush()
            sort_dedup_file(combo_tmp_path, combo_path)
            combos = int(subprocess.check_output(["wc", "-l", combo_path]).split()[0])
            print(f"PROGRESS phase=2 combos={combos} elapsed={time.time()-t1:.1f}")
            print(f"[OK]  Phase 2 done: {combos} unique combos in {time.time()-t1:.1f}s")
            sys.stdout.flush()

    finally:
        # clean up temp dir
        shutil.rmtree(tmp_dir, ignore_errors=True)

    total = time.time() - t0
    ulp_size   = os.path.getsize(ulp_path)   if os.path.exists(ulp_path)   else 0
    combo_size = os.path.getsize(combo_path) if os.path.exists(combo_path) else 0
    print(f"DONE hits={hits} ulp={ulp_written if write_ulp else 0} combos={combos} elapsed={total:.1f} ulp_bytes={ulp_size} combo_bytes={combo_size}")
    sys.stdout.flush()

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
