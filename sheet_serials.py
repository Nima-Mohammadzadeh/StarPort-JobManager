"""
sheet_serials.py — sequential serial numbers for customer data sheets (CDO, Venture Research)
=============================================================================================
Some customers order a data sheet that counts up in one running series instead of a
UPC-based range: a decimal serial and an EPC that is that serial as 8 hex digits
(DEC2HEX(serial, 8)). The series belongs to the sheet, not to a job, and no number may
ever be handed out twice, so it is recorded in three places like the UPC serials:

  1. App database        local.db → sheet_serials
  2. Sheet log           serial_registry/data_sheets/<sheet>.txt  (human-readable)
  3. Journal             <backup folder>/sheet_serial_journal.csv (append-only)

The next number is always 1 past the HIGHEST end found in any of them (and never below the
sheet's first number), so losing or rolling back one record cannot cause a repeat. A range is
never taken back: if a job fails after taking numbers they are marked VOID and skipped, which
leaves a gap in the series but can never create a duplicate.

A sheet with a `share` folder (Venture Research) is also checked against every data file in
that customer's folder on the share, so a sheet made by hand outside the app still pushes the
series past its numbers. What a file holds is cached by its size and time, so only new or
changed files are read; the highest number ever seen is kept even if the file is later removed.
A file that can't be read stops the job: it might hold numbers we would otherwise repeat.
"""
import os
import csv
import json
import threading
from datetime import datetime

import db as DB
import serial_registry as R
import serial_store as SS
from config import SERIAL_REGISTRY_DIR, Z_DRIVE_BASE

# sheet → first number and largest number it can ever reach.
#   cdo_seq: 3768662565…3768677564 was the sample sheet CDO sent (already used), so the
#   series continues at 3768677565. 8 hex digits stop at FFFFFFFF.
#   venture_seq: every Venture Research sheet on the share (all label sizes) is one series:
#   10001-10020 proof, 10021-60020 job 1943978, 60021-160020 1943979, 160021-360020 1943977,
#   360021-380020 1998128. So the series continues at 380021. Serials are 10 decimal digits.
#   `share` is the customer folder whose files are checked too (see share_last_end).
SEQ_SHEETS = {
    "cdo_seq": {"first": 3_768_677_565, "max": 0xFFFFFFFF,
                "max_note": "the largest number an 8-digit hex EPC holds"},
    "venture_seq": {"first": 380_021, "max": 9_999_999_999, "max_note": "the largest 10-digit serial",
                    "share": "Venture Research"},
}

_lock = threading.Lock()


class SheetSerialError(Exception):
    pass


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS sheet_serials (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sheet TEXT NOT NULL, job_number TEXT NOT NULL,
        start INTEGER NOT NULL, "end" INTEGER NOT NULL, qty INTEGER NOT NULL,
        note TEXT, created_by TEXT, created_at TEXT NOT NULL,
        UNIQUE(sheet, start))""")


# ── the three records ────────────────────────────────────────────────────────
def _log_path(sheet):
    return os.path.join(SERIAL_REGISTRY_DIR, "data_sheets", f"{sheet}.txt")


def _journal_file():
    d = (SS._journal.get("dir") or "").strip()
    return os.path.join(d, "sheet_serial_journal.csv") if d else ""


def _max_end(path_rows):
    ends = [e for e in path_rows if e is not None]
    return max(ends) if ends else None


def _db_last_end(conn, sheet):
    return conn.execute('SELECT MAX("end") FROM sheet_serials WHERE sheet=?', (sheet,)).fetchone()[0]


def _log_last_end(sheet):
    p = _log_path(sheet)
    if not os.path.isfile(p):
        return None
    best = None
    with open(p, "r", encoding="utf-8") as fh:
        for line in fh:
            parts = [x.strip() for x in line.split("|")]
            if line.startswith("#") or len(parts) < 5:
                continue
            try:
                best = max(best or 0, int(parts[3]))
            except ValueError:
                pass
    return best


def _journal_last_end(sheet):
    p = _journal_file()
    if not p or not os.path.isfile(p):
        return None
    best = None
    try:
        with open(p, "r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                try:
                    if row["sheet"] == sheet:
                        best = max(best or 0, int(row["end"]))
                except (KeyError, ValueError, TypeError):
                    pass
    except OSError:
        return None
    return best


def last_end(sheet, conn=None):
    own = conn is None
    conn = conn or DB.open()
    try:
        return _max_end([_db_last_end(conn, sheet), _log_last_end(sheet), _journal_last_end(sheet),
                         share_last_end(sheet)["max"]])
    finally:
        if own:
            conn.close()


def next_start(sheet):
    cfg = SEQ_SHEETS[sheet]
    last = last_end(sheet)
    return cfg["first"] if last is None else max(cfg["first"], last + 1)


def _append_log(sheet, job_number, start, end, qty, note, ts):
    p = _log_path(sheet)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    new = not os.path.isfile(p)
    with open(p, "a", encoding="utf-8") as fh:
        if new:
            fh.write(f"# Data sheet serials: {sheet}\n# Initialized: {ts}\n"
                     "# Format: TIMESTAMP | JOB_NUMBER | START | END | QTY | NOTE\n#\n")
        fh.write(f"{ts} | {job_number} | {start} | {end} | {qty} | {note}\n")


def _append_journal(sheet, job_number, start, end, qty, note, who, ts):
    p = _journal_file()
    if not p:
        return False
    try:
        new = not os.path.isfile(p)
        with open(p, "a", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["timestamp", "sheet", "job_number", "start", "end", "qty", "note", "by"])
            w.writerow([ts, sheet, job_number, start, end, qty, note, who or ""])
        return True
    except OSError:
        return False


# ── public API ───────────────────────────────────────────────────────────────
def reserve(sheet, job_number, qty, who=None, note="base"):
    """Take the next `qty` numbers of the series. Returns {start, end, qty, sheet}.
    Safe across threads, processes and machines (same locks as the UPC registry)."""
    cfg = SEQ_SHEETS.get(sheet)
    if not cfg:
        raise SheetSerialError(f"Unknown sequential data sheet: {sheet}")
    qty = int(qty)
    if qty <= 0:
        raise SheetSerialError("Quantity must be positive")
    with _lock:
        lock_path = R._acquire_file_lock(f"sheet_{sheet}")
        try:
            share = share_last_end(sheet)          # before the database lock: the first read of the share is slow
            conn = DB.open()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if share["unreadable"]:
                    raise SheetSerialError(
                        f"These files in the {cfg['share']} folder can't be read, so it isn't safe to hand out "
                        f"numbers (they may already hold some): {'; '.join(share['unreadable'][:5])}. "
                        "Close them in Excel, or fix or move them, and try again.")
                start = cfg["first"]
                log_last = _log_last_end(sheet)
                last = _max_end([_db_last_end(conn, sheet), log_last, _journal_last_end(sheet), share["max"]])
                if last is not None:
                    start = max(start, last + 1)
                end = start + qty - 1
                if end > cfg["max"]:
                    raise SheetSerialError(
                        f"That would run past {cfg['max']:,}, {cfg.get('max_note', 'the largest number this sheet holds')} "
                        f"(next free is {start:,}).")
                ts = _now()
                if share["max"] is not None and share["max"] > (log_last or 0):
                    # Write what the share holds into the sheet log too, so losing the scan cache can't lower it.
                    _append_log(sheet, "SHARE", share["min"] or share["max"], share["max"], 0,
                                f"highest found in the {cfg['share']} folder", ts)
                _append_log(sheet, job_number, start, end, qty, note, ts)
                conn.execute('INSERT INTO sheet_serials (sheet, job_number, start, "end", qty, note, created_by, created_at)'
                             " VALUES (?,?,?,?,?,?,?,?)", (sheet, job_number, start, end, qty, note, who, ts))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
            _append_journal(sheet, job_number, start, end, qty, note, who, ts)
            return {"sheet": sheet, "start": start, "end": end, "qty": qty, "job_number": job_number}
        finally:
            R._release_file_lock(lock_path)


def void(alloc, reason):
    """The job failed after taking numbers. They stay used (never handed out again) but are
    marked VOID in the notes so the gap in the series is explained."""
    sheet, start = alloc["sheet"], alloc["start"]
    note = f"VOID: {reason}"[:200].replace("|", "/")
    try:
        with DB.connect() as conn:
            conn.execute("UPDATE sheet_serials SET note=? WHERE sheet=? AND start=?", (note, sheet, start))
            conn.commit()
    except Exception:
        pass
    ts = _now()
    try:
        _append_log(sheet, alloc["job_number"], start, alloc["end"], 0, note, ts)
    except OSError:
        pass
    _append_journal(sheet, alloc["job_number"], start, alloc["end"], 0, note, None, ts)


def history(sheet):
    conn = DB.open()
    try:
        return [dict(r) for r in conn.execute(
            'SELECT job_number, start, "end", qty, note, created_by, created_at FROM sheet_serials '
            "WHERE sheet=? ORDER BY start", (sheet,))]
    finally:
        conn.close()


# ── sheets already on the share ──────────────────────────────────────────────
_scan_lock = threading.Lock()
DATA_FILE_EXT = (".xlsx", ".xlsm", ".csv")


def _scan_cache_path(sheet):
    return os.path.join(SERIAL_REGISTRY_DIR, "data_sheets", f"{sheet}_share_scan.json")


def _load_scan_cache(sheet):
    try:
        with open(_scan_cache_path(sheet), "r", encoding="utf-8") as fh:
            c = json.load(fh)
        if isinstance(c, dict) and isinstance(c.get("files"), dict):
            return c
    except (OSError, ValueError):
        pass
    return {"files": {}, "high_water": None, "low": None}


def _save_scan_cache(sheet, cache):
    p = _scan_cache_path(sheet)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
        os.replace(tmp, p)
    except OSError:
        pass                      # only a cache: the next check reads the files again


def _serials_in_file(path, row_serials):
    """(lowest, highest, count) of the sheet's serials in one data file."""
    lo = hi = None
    n = 0
    if path.lower().endswith(".csv"):
        fh = open(path, "r", encoding="utf-8-sig", errors="replace", newline="")
        rows, close = csv.reader(fh), fh.close
    else:
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        rows = (row for ws in wb.worksheets for row in ws.iter_rows(values_only=True))
        close = wb.close
    try:
        for row in rows:
            for v in row_serials(row):
                lo = v if lo is None else min(lo, v)
                hi = v if hi is None else max(hi, v)
                n += 1
    finally:
        close()
    return lo, hi, n


def _row_reader(sheet):
    import generators as G
    return {"venture_seq": G.venture_serials_in_row}[sheet]


def share_last_end(sheet):
    """The highest serial of this sheet in the customer's share folder (every .xlsx/.xlsm/.csv under it).
    Returns {"max", "min", "files", "unreadable": [names]}. max is None when the sheet has no share
    folder or nothing was found. The highest number ever seen is kept, even after a file is removed."""
    cfg = SEQ_SHEETS.get(sheet) or {}
    out = {"max": None, "min": None, "files": 0, "unreadable": []}
    if not cfg.get("share"):
        return out
    row_serials = _row_reader(sheet)
    root = os.path.join(Z_DRIVE_BASE, cfg["share"])
    with _scan_lock:
        cache = _load_scan_cache(sheet)
        seen = {}
        if not os.path.isdir(root):              # folder not there (yet): keep what we knew
            out.update(max=cache.get("high_water"), min=cache.get("low"))
            return out
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if name.startswith("~$") or not name.lower().endswith(DATA_FILE_EXT):
                    continue
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, root)
                try:
                    st = os.stat(path)
                    sig = [st.st_size, st.st_mtime_ns]
                    hit = cache["files"].get(rel)
                    if hit and hit.get("sig") == sig:
                        seen[rel] = hit
                        continue
                    lo, hi, n = _serials_in_file(path, row_serials)
                except Exception as e:          # any file we can't read might hold numbers: stop
                    out["unreadable"].append(f"{rel} ({type(e).__name__})")
                    continue
                seen[rel] = {"sig": sig, "min": lo, "max": hi, "count": n}
        high = _max_end([f.get("max") for f in seen.values()] + [cache.get("high_water")])
        lows = [f["min"] for f in seen.values() if f.get("min") is not None]
        if cache.get("low") is not None:
            lows.append(cache["low"])
        low = min(lows) if lows else None
        new = {"files": seen, "high_water": high, "low": low}
        if new != {k: cache.get(k) for k in new}:
            _save_scan_cache(sheet, {**new, "scanned_at": _now()})
    out.update(max=high, min=low, files=sum(1 for f in seen.values() if f.get("count")))
    return out
