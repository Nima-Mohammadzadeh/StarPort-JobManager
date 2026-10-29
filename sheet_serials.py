"""
sheet_serials.py — sequential serial numbers for customer data sheets (CDO)
===========================================================================
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
"""
import os
import csv
import sqlite3
import threading
from datetime import datetime

import serial_registry as R
import serial_store as SS
from config import DB_PATH, SERIAL_REGISTRY_DIR

# sheet → first number and largest number it can ever reach.
#   cdo_seq: 3768662565…3768677564 was the sample sheet CDO sent (already used), so the
#   series continues at 3768677565. 8 hex digits stop at FFFFFFFF.
SEQ_SHEETS = {
    "cdo_seq": {"first": 3_768_677_565, "max": 0xFFFFFFFF},
}

_lock = threading.Lock()


class SheetSerialError(Exception):
    pass


def _db():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


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
    conn = conn or _db()
    try:
        return _max_end([_db_last_end(conn, sheet), _log_last_end(sheet), _journal_last_end(sheet)])
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
            conn = _db()
            try:
                conn.execute("BEGIN IMMEDIATE")
                start = cfg["first"]
                last = _max_end([_db_last_end(conn, sheet), _log_last_end(sheet), _journal_last_end(sheet)])
                if last is not None:
                    start = max(start, last + 1)
                end = start + qty - 1
                if end > cfg["max"]:
                    raise SheetSerialError(
                        f"That would run past {cfg['max']:,}, the largest number an 8-digit hex EPC holds "
                        f"(next free is {start:,}).")
                ts = _now()
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
        conn = _db()
        conn.execute("UPDATE sheet_serials SET note=? WHERE sheet=? AND start=?", (note, sheet, start))
        conn.commit(); conn.close()
    except Exception:
        pass
    ts = _now()
    try:
        _append_log(sheet, alloc["job_number"], start, alloc["end"], 0, note, ts)
    except OSError:
        pass
    _append_journal(sheet, alloc["job_number"], start, alloc["end"], 0, note, None, ts)


def history(sheet):
    conn = _db()
    try:
        return [dict(r) for r in conn.execute(
            'SELECT job_number, start, "end", qty, note, created_by, created_at FROM sheet_serials '
            "WHERE sheet=? ORDER BY start", (sheet,))]
    finally:
        conn.close()
