"""
serial_store.py — every serial range is kept in three places
=============================================================
  1. Serial registry      serial_registry/<UPC>.txt   (primary; same format as the old app)
  2. App database mirror  local.db → serial_allocations
  3. Serial journal       <backup folder>/serial_journal.csv   (append-only, on the share)

The next serial for a UPC is always 1 past the HIGHEST end found in ANY of them, so
losing or rolling back one record (an old backup restored, a folder deleted) can never
cause a serial to be handed out twice.

If the registry can't be reached, no serials are given out unless the person creating
the job confirms a start serial at or above the highest one the other records know.
Those ranges are marked "waiting" and written to the registry as soon as it is back;
if something else used an overlapping range in the meantime, it is flagged as a
conflict instead of being written.
"""
import os
import csv
import time
import sqlite3
import threading
from datetime import datetime

import serial_registry as R
from config import DB_PATH, SERIAL_REGISTRY_DIR

DEFAULT_START = R.SERIAL_START_DEFAULT
MAX_SERIAL = (1 << 38) - 1
SPECIAL_JOBS = ("HISTORY", "RESERVED")

_lock = threading.Lock()                     # one allocation at a time in this process
_health = {"t": 0.0, "val": None}
_journal = {"dir": "", "cache_mtime": None, "cache": {}}


class SerialsOffline(Exception):
    def __init__(self, msg, info):
        super().__init__(msg)
        self.info = info


def _db():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── setup ─────────────────────────────────────────────────────────────────────
def init_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS serial_allocations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        upc TEXT NOT NULL, job_number TEXT NOT NULL,
        start INTEGER NOT NULL, "end" INTEGER NOT NULL, qty INTEGER NOT NULL,
        note TEXT, created_by TEXT, created_at TEXT NOT NULL,
        registry_state INTEGER NOT NULL DEFAULT 1,      -- 1 in registry · 0 waiting · -1 conflict
        UNIQUE(upc, job_number, start))""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_serial_upc ON serial_allocations(upc)")


def set_journal_dir(path):
    _journal["dir"] = (path or "").strip()


def journal_path():
    return os.path.join(_journal["dir"], "serial_journal.csv") if _journal["dir"] else ""


# ── health ────────────────────────────────────────────────────────────────────
def registry_ok(force=False):
    """(ok, message). Checks the registry folder exists and can be written."""
    if not force and _health["val"] is not None and time.time() - _health["t"] < 10:
        return _health["val"]
    ok, msg = True, ""
    try:
        if not os.path.isdir(SERIAL_REGISTRY_DIR):
            raise OSError(f"folder not found: {SERIAL_REGISTRY_DIR}")
        # One probe file per thread: threads in one process (waitress) share a pid, and a shared
        # name let one thread delete the file under another, which then reported the registry offline.
        probe = os.path.join(SERIAL_REGISTRY_DIR, f".write_check_{os.getpid()}_{threading.get_ident()}")
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
    except OSError as e:
        ok, msg = False, str(e)
    _health.update(t=time.time(), val=(ok, msg))
    return ok, msg


def health():
    ok, msg = registry_ok()
    jp = journal_path()
    conn = _db()
    pending = conn.execute("SELECT COUNT(*) FROM serial_allocations WHERE registry_state=0").fetchone()[0]
    conflicts = [dict(r) for r in conn.execute(
        'SELECT id, upc, job_number, start, "end", qty, created_at FROM serial_allocations WHERE registry_state=-1')]
    conn.close()
    return {"registry_ok": ok, "registry_message": msg, "registry_dir": SERIAL_REGISTRY_DIR,
            "journal_path": jp, "journal_ok": bool(jp) and os.path.isdir(_journal["dir"]),
            "pending": pending, "conflicts": conflicts}


# ── reading the three records ─────────────────────────────────────────────────
def _mirror_last_end(conn, upc):
    r = conn.execute('SELECT MAX("end") FROM serial_allocations WHERE upc=?', (upc,)).fetchone()[0]
    return r


def _journal_last_end(upc):
    jp = journal_path()
    if not jp or not os.path.isfile(jp):
        return None
    try:
        mt = os.path.getmtime(jp)
        if mt != _journal["cache_mtime"]:
            data = {}
            with open(jp, "r", encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    try:
                        u, e = row["upc"], int(row["end"])
                        data[u] = max(data.get(u, e), e)
                    except (KeyError, ValueError, TypeError):
                        pass
            _journal.update(cache_mtime=mt, cache=data)
        return _journal["cache"].get(upc)
    except OSError:
        return None


def _jobs_last_end(conn, upc):
    """Fourth safety net: the highest serial any job in the database used for this UPC,
    worked out from its start serial and quantity. Protects against a registry folder
    that was never copied or was lost."""
    from generators import production_qty
    best = None
    for r in conn.execute("SELECT start_serial, quantity, overage_pct FROM jobs WHERE upc=? AND start_serial IS NOT NULL", (upc,)):
        if r[0] and r[1]:
            end = int(r[0]) + production_qty(int(r[1]), float(r[2] or 0)) - 1
            best = end if best is None else max(best, end)
    return best


def known_last_end(upc, conn=None):
    """Highest end in any record that can be read right now (None if none)."""
    own = conn is None
    conn = conn or _db()
    try:
        ends = [_mirror_last_end(conn, upc), _journal_last_end(upc), _jobs_last_end(conn, upc)]
        if registry_ok()[0]:
            ends.append(R._read_last_end(upc))
        ends = [e for e in ends if e is not None]
        return max(ends) if ends else None
    finally:
        if own:
            conn.close()


def next_start(upc):
    last = known_last_end(upc)
    return DEFAULT_START if last is None else last + 1


# ── writing ───────────────────────────────────────────────────────────────────
def _journal_append(rec, status):
    jp = journal_path()
    if not jp:
        return False
    try:
        new = not os.path.isfile(jp)
        with open(jp, "a", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["timestamp", "upc", "job_number", "start", "end", "qty", "note", "by", "status"])
            w.writerow([rec["timestamp"], rec["upc"], rec["job_number"], rec["start"], rec["end"],
                        rec["qty"], rec.get("note", ""), rec.get("by") or "", status])
        return True
    except OSError:
        return False


def _repair_registry(conn, upc):
    """Write mirror lines (already confirmed) that the registry file lacks — e.g. after
    the registry folder was restored from an older backup."""
    have = {(r["job_number"], r["start"]) for r in R.read_records(upc)}
    missing = [dict(r) for r in conn.execute(
        'SELECT job_number, start, "end", qty, note, created_at FROM serial_allocations '
        'WHERE upc=? AND registry_state=1 ORDER BY start', (upc,))
        if (r["job_number"], r["start"]) not in have]
    R.append_records(upc, [{"timestamp": m["created_at"], "job_number": m["job_number"], "start": m["start"],
                            "end": m["end"], "qty": m["qty"],
                            "note": f"{m['note'] or ''}; restored from app database".strip("; ")}
                           for m in missing])
    return len(missing)


def reserve(upc, job_number, qty, note="base", min_start=None, who=None, offline_start=None):
    """Give out `qty` serials for `upc`. Returns {start, end, qty, skipped, offline}.
    Raises ValueError (bad request), SerialsOffline (registry unreachable and no
    confirmed start), RuntimeError (I/O)."""
    upc = upc.strip()
    if qty <= 0:
        raise ValueError("Quantity must be positive")
    with _lock:
        ok, msg = registry_ok(force=True)
        conn = _db()
        try:
            known = [e for e in (_mirror_last_end(conn, upc), _journal_last_end(upc), _jobs_last_end(conn, upc))
                     if e is not None]
            known_end = max(known) if known else None
            if ok:
                _repair_registry(conn, upc)
                natural = R.peek_next_start(upc)
                floor = max(natural, (known_end + 1) if known_end is not None else 0)
                want = max(min_start or 0, floor)
                reason = "start set by user" if min_start and min_start >= floor else \
                         "started after the last serial in the app database"
                a = R.allocate_serials(upc, job_number, qty, note,
                                       min_start=want if want > natural else None, skip_reason=reason)
                start, end, state, note_out = a["start"], a["end"], 1, note
                skipped = start - floor if min_start else a.get("skipped", 0)
            else:
                need = (known_end + 1) if known_end is not None else DEFAULT_START
                if offline_start is None:
                    raise SerialsOffline(f"The serial registry can't be reached ({msg}).",
                                         {"next_known": need, "registry_dir": SERIAL_REGISTRY_DIR,
                                          "journal": bool(journal_path() and os.path.isdir(_journal["dir"]))})
                if offline_start < need or (min_start and offline_start < min_start):
                    raise ValueError(f"Start serial {offline_start:,} is below {max(need, min_start or 0):,}, "
                                     "the next serial the app knows is free for this UPC.")
                start, end, state = offline_start, offline_start + qty - 1, 0
                note_out = f"{note}; created while the serial registry was offline"
                skipped = start - need
            if end > MAX_SERIAL:
                raise ValueError("That range runs past the largest serial an EPC can hold.")
            ts = _now()
            conn.execute('INSERT INTO serial_allocations (upc, job_number, start, "end", qty, note, created_by, '
                         'created_at, registry_state) VALUES (?,?,?,?,?,?,?,?,?)',
                         (upc, job_number, start, end, qty, note_out, who, ts, state))
            conn.commit()
            journaled = _journal_append({"timestamp": ts, "upc": upc, "job_number": job_number, "start": start,
                                         "end": end, "qty": qty, "note": note_out, "by": who},
                                        "registry" if state == 1 else "waiting for registry")
            return {"start": start, "end": end, "qty": qty, "skipped": skipped,
                    "offline": state == 0, "journaled": journaled}
        finally:
            conn.close()


def record_history(upc, last_used, note, who=None):
    """Record that serials up to `last_used` were used outside this app (forward-only)."""
    with _lock:
        if not registry_ok(force=True)[0]:
            raise SerialsOffline("The serial registry can't be reached.", {})
        conn = _db()
        try:
            known = known_last_end(upc, conn)
            floor = (known if known is not None else DEFAULT_START - 1)
            if last_used <= floor:
                return {"changed": False, "next_start": floor + 1}
            _repair_registry(conn, upc)
            ts = _now()
            R.append_records(upc, [{"timestamp": ts, "job_number": "HISTORY", "start": floor + 1,
                                    "end": last_used, "qty": 0, "note": note}])
            conn.execute('INSERT OR IGNORE INTO serial_allocations (upc, job_number, start, "end", qty, note, '
                         'created_by, created_at, registry_state) VALUES (?,?,?,?,?,?,?,?,1)',
                         (upc, "HISTORY", floor + 1, last_used, 0, note, who, ts))
            conn.commit()
            _journal_append({"timestamp": ts, "upc": upc, "job_number": "HISTORY", "start": floor + 1,
                             "end": last_used, "qty": 0, "note": note, "by": who}, "registry")
            return {"changed": True, "next_start": last_used + 1}
        finally:
            conn.close()


def registry_gaps():
    """UPCs whose jobs (in the database) used serials beyond the end of their registry
    file — usually a serial_registry folder that wasn't copied. Returns a list."""
    if not registry_ok()[0]:
        return []
    from generators import production_qty
    conn = _db()
    try:
        ends = {}
        for upc, st, q, ov in conn.execute("SELECT upc, start_serial, quantity, overage_pct FROM jobs "
                                           "WHERE upc IS NOT NULL AND upc != '' AND start_serial IS NOT NULL"):
            if st and q:
                e = int(st) + production_qty(int(q), float(ov or 0)) - 1
                ends[upc] = max(ends.get(upc, e), e)
    finally:
        conn.close()
    gaps = []
    for upc, e in ends.items():
        reg = R._read_last_end(upc)
        if reg is None or reg < e:
            gaps.append({"upc": upc, "jobs_end": e, "registry_end": reg})
    return gaps


# ── keeping the records in step ───────────────────────────────────────────────
def backfill_from_registry():
    """Copy registry lines the database doesn't have yet (first start, or ranges made
    by seed_serials / import_old). Returns number of lines added."""
    if not registry_ok(force=True)[0]:
        return 0
    conn = _db()
    added = 0
    try:
        for f in os.listdir(SERIAL_REGISTRY_DIR):
            if not f.endswith(".txt"):
                continue
            upc = f[:-4]
            for r in R.read_records(upc):
                cur = conn.execute('INSERT OR IGNORE INTO serial_allocations (upc, job_number, start, "end", qty, '
                                   'note, created_at, registry_state) VALUES (?,?,?,?,?,?,?,1)',
                                   (upc, r["job_number"], r["start"], r["end"], r["qty"], r["note"], r["timestamp"]))
                added += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return added


def repair_all():
    """Write confirmed database ranges back into registry files that lost them."""
    if not registry_ok(force=True)[0]:
        return 0
    conn = _db()
    try:
        upcs = [r[0] for r in conn.execute("SELECT DISTINCT upc FROM serial_allocations WHERE registry_state=1")]
        return sum(_repair_registry(conn, u) for u in upcs)
    finally:
        conn.close()


def sync_pending():
    """Write ranges created while the registry was offline. Returns {synced, conflicts}."""
    out = {"synced": 0, "conflicts": []}
    if not registry_ok(force=True)[0]:
        return out
    with _lock:
        conn = _db()
        try:
            rows = [dict(r) for r in conn.execute(
                'SELECT id, upc, job_number, start, "end", qty, note, created_at FROM serial_allocations '
                'WHERE registry_state=0 ORDER BY id')]
            for row in rows:
                clash = [r for r in R.read_records(row["upc"])
                         if (r["job_number"], r["start"]) != (row["job_number"], row["start"])
                         and r["start"] <= row["end"] and row["start"] <= r["end"]]
                if clash:
                    conn.execute("UPDATE serial_allocations SET registry_state=-1 WHERE id=?", (row["id"],))
                    out["conflicts"].append({**row, "clashes_with": [c["job_number"] for c in clash]})
                else:
                    R.append_records(row["upc"], [{"timestamp": row["created_at"], "job_number": row["job_number"],
                                                   "start": row["start"], "end": row["end"], "qty": row["qty"],
                                                   "note": row["note"]}])
                    conn.execute("UPDATE serial_allocations SET registry_state=1 WHERE id=?", (row["id"],))
                    out["synced"] += 1
            conn.commit()
        finally:
            conn.close()
    return out


def resolve_conflict(row_id, who, note):
    """Someone checked a conflict. The range stays recorded (it is written to the registry
    with the reviewer's note), so its serials still can never be given out again."""
    with _lock:
        if not registry_ok(force=True)[0]:
            raise SerialsOffline("The serial registry can't be reached.", {})
        conn = _db()
        try:
            row = conn.execute('SELECT * FROM serial_allocations WHERE id=? AND registry_state=-1', (row_id,)).fetchone()
            if not row:
                raise ValueError("That conflict is already resolved")
            text = f"{row['note'] or ''}; conflict reviewed by {who}: {note}".strip("; ")
            R.append_records(row["upc"], [{"timestamp": row["created_at"], "job_number": row["job_number"],
                                           "start": row["start"], "end": row["end"], "qty": row["qty"], "note": text}])
            conn.execute("UPDATE serial_allocations SET registry_state=1, note=? WHERE id=?", (text, row_id))
            conn.commit()
            return dict(row)
        finally:
            conn.close()


# ── looking things up ─────────────────────────────────────────────────────────
def history(upc):
    """Every range for a UPC from the registry and the database, merged."""
    reg_ok = registry_ok()[0]
    reg = {(r["job_number"], r["start"]): r for r in (R.read_records(upc) if reg_ok else [])}
    conn = _db()
    mir = {(r["job_number"], r["start"]): dict(r) for r in conn.execute(
        'SELECT * FROM serial_allocations WHERE upc=?', (upc,))}
    conn.close()
    rows = []
    for k in set(reg) | set(mir):
        r, m = reg.get(k), mir.get(k)
        base = m or r
        rows.append({"job_number": k[0], "start": k[1], "end": base["end"], "qty": base["qty"],
                     "note": (r or m).get("note") or "", "when": (m or {}).get("created_at") or r["timestamp"],
                     "by": (m or {}).get("created_by"),
                     "in_registry": r is not None if reg_ok else None,
                     "state": {1: "recorded", 0: "waiting for registry", -1: "CONFLICT"}.get(
                         (m or {}).get("registry_state", 1), "recorded")})
    rows.sort(key=lambda x: x["start"])
    return {"upc": upc, "rows": rows, "next_start": next_start(upc), "registry_ok": reg_ok,
            "used": sum(x["qty"] for x in rows), "jobs": sum(1 for x in rows if x["job_number"] not in SPECIAL_JOBS)}


def check_range(upc, start, end):
    """Is [start, end] inside ranges recorded for this UPC? Returns {covered, jobs}."""
    h = history(upc)["rows"]
    hits = [r for r in h if r["start"] <= end and start <= r["end"]]
    covered, cur = False, start
    for r in sorted(hits, key=lambda r: r["start"]):
        if r["start"] <= cur:
            cur = max(cur, r["end"] + 1)
        if cur > end:
            covered = True
            break
    return {"covered": covered, "jobs": sorted({r["job_number"] for r in hits})}
