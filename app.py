"""
Job Manager — encoding team edition
===================================
One job = one Radius ticket. Creating a job:
  1. reserves a job number
  2. allocates a serial range for the UPC (serial_registry, file-locked)
  3. creates  <customer>\\<label size>\\<date> - <PO> - <ticket>\\{Data,Print,Proof}
  4. writes the encoding database file(s), checklist and QC sheet
  5. optionally copies a BarTender template into Print\\
  6. sets up the digital roll tracker

Everything else from the old app (inventory, audits, email, rewind, Zebra
roll tags, analytics, template rules) was removed on purpose.
"""
import os
import re
import json
import time
import shutil
import sqlite3
import zipfile
import tempfile
import threading
import subprocess
import io
import sys
import socket
import hashlib
import logging
import logging.handlers
import secrets
import traceback
from html import escape
from datetime import date, datetime, timedelta

from flask import Flask, jsonify, render_template, request, send_file, abort, Response, g

from config import (DB_PATH, Z_DRIVE_BASE, Z_IS_DEFAULT, SERIAL_REGISTRY_DIR, THUMBNAIL_DIR, CONFIG_INI,
                    HOST, PORT, SYSTEM, validate_and_prepare)
from serial_registry import (allocate_serials, peek_next_start, get_upc_history,
                             SERIAL_START_DEFAULT)
from generators import (validate_upc, production_qty, roll_plan, roll_plan_plain, db_file_split,
                        generate_db_files, generate_go2_files, generate_cdo_files, DATA_SHEETS, GO2_SHEET, CDO_SHEET,
                        sheet_customer, roll_plan_seq, sheet_size_error, generate_qc_sheet, qc_html, _safe_name, safe_folder_name,
                        go2_file_names, XLSX_AVAILABLE)
import serial_store as SS
import integrity
import floor_ops as F
import host_check as H
import sheet_serials as SHEETS_SEQ
import backup
from generators import decode_epc
from checklist_generator import generate_checklist_html
from lists import CUSTOMERS, LABEL_SIZES
import proofing                      # RFID proofing at /proofing (proofing/__init__.py)

APP_VERSION = "3.3"
DEFAULT_TEMPLATE_DIR = r"Z:\3 Encoding and Printing Files\Master Templates"
STATUSES    = ["Pending", "In Progress", "On Hold", "Complete", "Cancelled"]
# Why a job is on hold. Asked every time a job goes on hold (see api_job_edit).
HOLD_REASONS = ["Material", "Printer", "Encoding", "Data / file", "Customer", "Quality", "Scheduling", "Other"]
JUNK_PO     = {"0", "00", "000", "na", "n/a", "none", "-", "--", "tbd", "x"}
EDITABLE    = ["title", "job_ticket", "customer", "status", "due_date", "notes",
               "inlay_type", "part_number", "po_number", "label_size"]

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 250 * 1024 * 1024   # uploads into job folders


# ── Error log, request IDs, page build ────────────────────────────────────────
# Every request gets a short ID (X-Request-ID). A server error is written to logs/jobmanager.log with
# that ID, the page, the person and the full traceback, and the browser shows the ID ("ref a1b2c3d4"),
# so a report from the floor can be matched to its log entry. Slow requests are logged too.
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
SLOW_REQUEST_SECONDS = 3.0
log = logging.getLogger("jobmanager")


def setup_log(log_dir=None):
    """Rotating log file (5 x 2 MB). Called at startup; safe to call again."""
    log_dir = log_dir or LOG_DIR
    log.setLevel(logging.INFO)
    for h in list(log.handlers):
        log.removeHandler(h); h.close()
    try:
        os.makedirs(log_dir, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(os.path.join(log_dir, "jobmanager.log"), maxBytes=2_000_000,
                                                 backupCount=5, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(h)
    except OSError as e:
        print(f"  [LOG] can't write {log_dir}: {e}")


def _page_build():
    """Fingerprint of the pages and their scripts. Changes when an update changes what the browser
    runs, so pages left open from before the update can tell (see poll() in index.html)."""
    root = os.path.dirname(os.path.abspath(__file__))
    hsh = hashlib.sha1()
    for d in ("templates", os.path.join("proofing", "static")):
        for dirpath, _dirs, files in sorted(os.walk(os.path.join(root, d))):
            for f in sorted(files):
                try:
                    with open(os.path.join(dirpath, f), "rb") as fh:
                        hsh.update(f.encode() + fh.read())
                except OSError:
                    pass
    return hsh.hexdigest()[:10]


BUILD = _page_build()


@app.before_request
def _begin_request():
    g.rid = secrets.token_hex(4)
    g.t0 = time.perf_counter()
    # A page opened before an update sends the old build. Reading still works; changing data is refused
    # until it reloads, so old page code can't send old-shaped data to the new server.
    sent = request.headers.get("X-App-Build")
    if sent and sent != BUILD and request.method in ("POST", "PATCH", "PUT", "DELETE") and request.path.startswith("/api/"):
        return jsonify({"error": "Job Manager was updated since this page was opened. Reload the page (F5) "
                                 "and try again. Nothing was changed.", "reload": True}), 409


@app.after_request
def _end_request(resp):
    rid = getattr(g, "rid", "")
    resp.headers["X-Request-ID"] = rid
    took = time.perf_counter() - getattr(g, "t0", time.perf_counter())
    if took >= SLOW_REQUEST_SECONDS:
        log.warning("slow ref=%s %.1fs %s %s -> %s", rid, took, request.method, request.full_path.rstrip("?"),
                    resp.status_code)
    return resp


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def err(msg, code=400, **extra):
    return jsonify({"error": msg, **extra}), code


def actor():
    """Name the browser sends with every request (set once per PC in Settings)."""
    try:
        return (request.headers.get("X-Operator") or "").strip()[:40] or None
    except RuntimeError:
        return None


def log_activity(conn, kind, job_id, message, who=None):
    conn.execute("INSERT INTO activity (ts, job_id, kind, actor, message) VALUES (?,?,?,?,?)",
                 (now_str(), job_id, kind, who or actor(), message))


def job_label(job):
    return f"{job.get('customer') or ''} {job.get('job_ticket') or job.get('job_number')}".strip()


# ══════════════════════════════════════════════════════════════════════════════
# Database
# ══════════════════════════════════════════════════════════════════════════════
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create the tables this app uses. Tables left over from the old app
    (inventory, audits, rewind, rules) are left alone — nothing reads them."""
    conn = get_db(); cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    cur.execute("INSERT OR IGNORE INTO _meta VALUES ('created_at', datetime('now'))")
    cur.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    cur.execute("""CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_number TEXT NOT NULL UNIQUE, job_ticket TEXT,
        title TEXT NOT NULL, customer TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'Pending', priority TEXT NOT NULL DEFAULT 'Medium',
        due_date TEXT, notes TEXT, upc TEXT, quantity INTEGER, lpr INTEGER,
        qty_db INTEGER, start_serial INTEGER, overage_pct REAL NOT NULL DEFAULT 0,
        label_size TEXT, inlay_type TEXT, part_number TEXT, po_number TEXT,
        folder_path TEXT, archived_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS job_rolls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        roll_number INTEGER NOT NULL, quantity INTEGER NOT NULL,
        serial_start INTEGER, serial_end INTEGER, epc_start TEXT, epc_end TEXT,
        status TEXT NOT NULL DEFAULT 'pending', completed_at TEXT, tag_printed_at TEXT,
        notes TEXT, created_at TEXT NOT NULL, UNIQUE(job_id, roll_number))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS btw_templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, file_path TEXT NOT NULL,
        description TEXT, notes TEXT, thumbnail_path TEXT,
        active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")

    cur.execute("""CREATE TABLE IF NOT EXISTS job_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        roll_number INTEGER, author TEXT, body TEXT NOT NULL,
        important INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)""")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_notes_job ON job_notes(job_id)")
    cur.execute("""CREATE TABLE IF NOT EXISTS activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, job_id INTEGER,
        kind TEXT NOT NULL, actor TEXT, message TEXT NOT NULL)""")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_activity_job ON activity(job_id)")

    def add_col(table, col, ddl):
        cols = [r[1] for r in cur.execute(f"PRAGMA table_info({table})")]
        if col not in cols:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
            print(f"  [DB]   added {table}.{col}")
    add_col("jobs", "archived_at", "archived_at TEXT")
    add_col("jobs", "template_path", "template_path TEXT")
    add_col("jobs", "job_type", "job_type TEXT NOT NULL DEFAULT 'standard'")
    add_col("jobs", "priority_rank", "priority_rank INTEGER")
    add_col("jobs", "created_by", "created_by TEXT")
    add_col("jobs", "completed_at", "completed_at TEXT")
    add_col("jobs", "data_sheet", "data_sheet TEXT")      # GO2 data sheet kind on special jobs
    for col in ("hold_reason", "hold_note", "hold_since", "hold_by"):   # set while a job is On Hold
        add_col("jobs", col, f"{col} TEXT")
    # Jobs already on hold before reasons were asked: they have no reason, but use when they were put on hold.
    cur.execute("""UPDATE jobs SET hold_since = COALESCE(
                       (SELECT MAX(ts) FROM activity a WHERE a.job_id = jobs.id AND a.kind = 'status'
                          AND a.message LIKE '%set to On Hold%'), updated_at)
                   WHERE status = 'On Hold' AND hold_since IS NULL""")
    # Jobs already marked Complete before this column existed: use when they were set to Complete.
    cur.execute("""UPDATE jobs SET completed_at = COALESCE(
                       (SELECT MAX(ts) FROM activity a WHERE a.job_id = jobs.id AND a.kind = 'status'
                          AND a.message LIKE '%set to Complete'), updated_at)
                   WHERE status = 'Complete' AND completed_at IS NULL""")
    cur.execute("UPDATE jobs SET job_type='special' WHERE (upc IS NULL OR upc='') AND job_type='standard'")
    SS.init_schema(cur)
    SHEETS_SEQ.init_schema(cur)
    proofing.init_schema(cur)                              # job_proofs: which proof belongs to which job
    add_col("job_rolls", "completed_by", "completed_by TEXT")
    add_col("job_rolls", "started_at", "started_at TEXT")
    add_col("job_rolls", "started_by", "started_by TEXT")
    add_col("job_rolls", "paused_at", "paused_at TEXT")
    add_col("job_rolls", "paused_by", "paused_by TEXT")
    add_col("job_rolls", "printer", "printer TEXT")
    F.init_schema(cur)                                     # tag checks, reprints, BarTender print requests
    add_col("btw_templates", "thumbnail_path", "thumbnail_path TEXT")
    add_col("btw_templates", "file_size", "file_size INTEGER")
    add_col("btw_templates", "file_mtime", "file_mtime TEXT")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_jobs_ticket ON jobs(job_ticket)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_jobs_archived ON jobs(archived_at)")
    # Old per-roll notes become entries in the job's note log.
    moved = cur.execute("""INSERT INTO job_notes (job_id, roll_number, author, body, important, created_at)
        SELECT job_id, roll_number, completed_by, notes, 0, COALESCE(completed_at, created_at)
        FROM job_rolls WHERE notes IS NOT NULL AND TRIM(notes) != ''""").rowcount
    if moved:
        cur.execute("UPDATE job_rolls SET notes=NULL WHERE notes IS NOT NULL")
        print(f"  [DB]   moved {moved} roll notes into the job note log")
    conn.commit()
    # Give open jobs a digital roll tracker if they were created before it existed.
    todo = [dict(r) for r in cur.execute("""SELECT * FROM jobs j WHERE archived_at IS NULL
        AND upc IS NOT NULL AND upc != '' AND start_serial IS NOT NULL AND lpr > 0
        AND quantity > 0 AND NOT EXISTS (SELECT 1 FROM job_rolls r WHERE r.job_id = j.id)""")]
    for job in todo:
        ensure_rolls(conn, job)
    if todo:
        print(f"  [DB]   set up roll tracking for {len(todo)} open jobs")
    conn.close()
    print(f"  [DB]   ready → {DB_PATH}")


DEFAULT_PRINTERS = "Printer 1\nPrinter 2\nPrinter 3\nPrinter 4"


def clean_printers(raw) -> str:
    """One printer name per line (commas also split), trimmed, no repeats, at most 24 names."""
    seen, out = set(), []
    for part in re.split(r"[\n,;]+", str(raw or "")):
        name = " ".join(part.split())[:30]
        if name and name.lower() not in seen:
            seen.add(name.lower()); out.append(name)
    return "\n".join(out[:24])


def get_setting(key, default=""):
    conn = get_db()
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


# Number settings: (label, lowest, highest, whole numbers only, default). Saving checks these; reading
# falls back to the default, so a bad value already in the database can never stop the app.
NUMBER_SETTINGS = {
    "default_overage_pct":   ("Default overage %", 0, 100, False, 0),
    "backup_interval_hours": ("Backup every (hours)", 1, 168, True, 6),
    "backup_keep":           ("Backups to keep", 5, 1000, True, 60),
    "auto_archive_hours":    ("Archive completed jobs after (hours)", 0, 8760, True, 32),
}


def _setting_number(key, raw):
    """The number in raw if it is valid for this setting, else None."""
    _, lo, hi, whole, _ = NUMBER_SETTINGS[key]
    try:
        v = float(str(raw).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None
    if v != v or (whole and v != int(v)) or not lo <= v <= hi:
        return None
    return int(v) if whole else (int(v) if v == int(v) else v)


# BarTender Integration folders: next to "Customers Encoding Files" on the share by default.
_ENC_ROOT = os.path.dirname(Z_DRIVE_BASE.rstrip("\\/"))
BT_DEFAULT_TRIGGERS = os.path.join(_ENC_ROOT, "BarTender Triggers")
BT_DEFAULT_ACKS = os.path.join(_ENC_ROOT, "BarTender Acknowledgements")
SHIFT_DEFAULT = "06:00,14:00,22:00"


def clean_shift_starts(raw):
    """'6:00, 14:00,22:00' -> '06:00,14:00,22:00', or None if any part isn't a time."""
    out = []
    for part in re.split(r"[,;\s]+", str(raw or "").strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", part)
        if not m or int(m[1]) > 23 or int(m[2]) > 59:
            return None
        out.append(f"{int(m[1]):02d}:{m[2]}")
    return ",".join(sorted(set(out))) or None


def get_settings() -> dict:
    conn = get_db()
    s = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM app_settings")}
    conn.close()
    def num(k):
        v = _setting_number(k, s.get(k, NUMBER_SETTINGS[k][4]))
        return NUMBER_SETTINGS[k][4] if v is None else v
    return {
        "btw_template_dir":      s.get("btw_template_dir") or DEFAULT_TEMPLATE_DIR,
        "default_overage_pct":   str(num("default_overage_pct")),
        "backup_dir":            s.get("backup_dir", ""),
        "backup_interval_hours": num("backup_interval_hours"),
        "backup_auto":           s.get("backup_auto", "1") == "1",
        "backup_keep":           num("backup_keep"),
        "rewind_base_url":       s.get("rewind_base_url", ""),
        "printers":              s.get("printers", DEFAULT_PRINTERS),
        "auto_archive_hours":    num("auto_archive_hours"),
        "bt_enabled":            s.get("bt_enabled", "0") == "1",
        "bt_trigger_dir":        s.get("bt_trigger_dir", BT_DEFAULT_TRIGGERS),
        "bt_ack_dir":            s.get("bt_ack_dir", BT_DEFAULT_ACKS),          # blank = don't wait for replies
        "bt_printer_map":        s.get("bt_printer_map", ""),
        "shift_starts":          s.get("shift_starts") or SHIFT_DEFAULT,
    }


def next_job_number(cur):
    """Next JOB-####. Never goes below job_number_floor, which the importer sets
    so a fresh database keeps counting after the old app's numbers."""
    row = cur.execute("SELECT job_number FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
    try:
        last = int(row["job_number"].split("-")[1])
    except Exception:
        last = 0
    floor = 0
    for key in ("job_number_floor", "job_number_last"):   # last = highest ever issued
        fl = cur.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        if fl and str(fl["value"]).isdigit():
            floor = max(floor, int(fl["value"]))
    n = max(last, floor) + 1
    # Remember it, so deleting the newest job never frees its number for reuse.
    cur.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('job_number_last', ?)", (str(n),))
    return f"JOB-{n:04d}"


def clean_po(po):
    po = (po or "").strip()
    return None if po.lower() in JUNK_PO else (po or None)


def job_folder_parts(customer, label_size, po, ticket, job_number):
    """<customer>\\<label size>\\<MM.DD.YY> - <job ticket>   (job number if there is no ticket).
    A folder that already holds another job gets " - JOB-####" added (see create_job)."""
    name   = safe_folder_name(f"{datetime.now():%m.%d.%y} - {ticket or job_number}")
    parts  = [safe_folder_name(customer)]
    if label_size:
        parts.append(safe_folder_name(label_size))
    parts.append(name)
    return parts


# ══════════════════════════════════════════════════════════════════════════════
# Job creation
# ══════════════════════════════════════════════════════════════════════════════
def _int(v, field, required=False, minimum=1):
    if v in (None, ""):
        if required:
            raise ValueError(f"{field} is required")
        return None
    try:
        n = int(float(v))
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{field} must be a whole number")
    if n < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    return n


def _same_ticket(conn, ticket, exclude_id=None):
    """Open, finished or archived jobs holding this ticket (cancelled jobs don't count)."""
    t = (ticket or "").strip().lower()
    if not t:
        return []
    sql = ("SELECT id, job_number, status, customer FROM jobs "
           "WHERE LOWER(TRIM(job_ticket))=? AND status!='Cancelled'")
    args = [t]
    if exclude_id:
        sql += " AND id!=?"; args.append(exclude_id)
    return [dict(r) for r in conn.execute(sql + " ORDER BY id", args)]


def find_duplicate(conn, ticket, customer, title, upc, quantity):
    """Returns an error payload if this job would duplicate an existing one, else None.
    With a ticket: the ticket must be new. Without one: the same entry (customer, item,
    UPC, quantity) can't be repeated within 2 minutes, which catches a double submit."""
    same = _same_ticket(conn, ticket)
    if same:
        names = ", ".join(j["job_number"] for j in same)
        return {"error": f"Ticket {ticket} already has a job ({names}). Open that job instead of creating a second one. "
                         "To redo it, cancel that job first.",
                "duplicate_of": [j["job_number"] for j in same], "duplicate_ids": [j["id"] for j in same]}
    if not (ticket or "").strip():
        r = conn.execute("""SELECT id, job_number FROM jobs WHERE customer=? AND title=? AND COALESCE(upc,'')=?
            AND COALESCE(quantity,0)=? AND status!='Cancelled'
            AND created_at >= datetime('now','localtime','-2 minutes') ORDER BY id DESC LIMIT 1""",
            (customer, title, upc or "", quantity or 0)).fetchone()
        if r:
            return {"error": f"This looks like the job that was just created ({r['job_number']}): same customer, item, "
                             "UPC and quantity. Open it, or wait two minutes if you really need a second one.",
                    "duplicate_of": [r["job_number"]], "duplicate_ids": [r["id"]]}
    return None


@app.route("/api/ticket-check")
def api_ticket_check():
    conn = get_db()
    jobs = _same_ticket(conn, request.args.get("ticket", ""))
    conn.close()
    return jsonify({"exists": bool(jobs), "jobs": jobs})


def create_job(data: dict) -> tuple[dict, int]:
    """Returns (result, http_status). Used by single and batch creation.

    Job types
      standard  UPC + serials: encoding database, serial-tracked rolls, QC sheet.
      special   no UPC / not our normal process: folder, checklist, template, notes and
                (if quantity and labels per roll are given) rolls tracked by quantity.
                No UPC serials are given out. Optional data_sheet (see DATA_SHEETS in generators.py):
                "go2_rand" (customer GO2 only) random EPC sheet; "cdo_seq" (customer CDO only)
                sequential serial + 8-hex EPC sheet, numbers from sheet_serials.py."""
    g = lambda k: (str(data.get(k) or "")).strip() or None
    customer, title, ticket = g("customer"), g("title"), g("job_ticket")
    upc, label_size, inlay  = g("upc"), g("label_size"), g("inlay_type")
    part, po, notes, due    = g("part_number"), g("po_number"), g("notes"), g("due_date")
    created_by = (g("created_by") or actor() or "")[:40] or None
    job_type = (g("job_type") or ("standard" if upc else "special")).lower()
    if job_type not in ("standard", "special"):
        return {"error": "Job type must be standard or special"}, 400

    if not customer: return {"error": "Customer is required"}, 400
    customer = sheet_customer(customer) or customer     # go2 / Go2 / cdo / Cdo are one customer each (folders, grouping)
    if not title:    return {"error": "Item / title is required"}, 400
    if job_type == "standard" and not upc:
        return {"error": "A standard job needs a UPC. Choose Special for jobs without one."}, 400
    data_sheet = g("data_sheet")
    if data_sheet:
        cfg = DATA_SHEETS.get(data_sheet)
        if not cfg:
            return {"error": "Unknown data sheet."}, 400
        if job_type != "special" or customer != cfg["customer"]:
            return {"error": f"The {cfg['customer']} data sheet is only for special (no UPC) jobs for customer {cfg['customer']}."}, 400
    if job_type == "special":
        upc = None                          # special jobs never take serials
    try:
        quantity = _int(data.get("quantity"), "Quantity", required=(job_type == "standard" or bool(data_sheet)))
        lpr      = _int(data.get("lpr"), "Labels per roll")
        qty_db   = _int(data.get("qty_db"), "Qty per DB file") if (upc or data_sheet) else None
        start_override = _int(data.get("start_serial"), "Start serial") if upc else None
        offline_start  = _int(data.get("offline_start"), "Start serial") if (upc and data.get("offline_confirm")) else None
        overage  = float(data.get("overage_pct") or 0)
        if not 0 <= overage <= 100:
            raise ValueError("Overage must be between 0 and 100")
    except ValueError as e:
        return {"error": str(e)}, 400
    if upc:
        v = validate_upc(upc)
        if not v["valid"]:
            return {"error": v["error"], "corrected_upc": v.get("corrected_upc")}, 400

    total_qty = production_qty(quantity, overage) if quantity else None
    if data_sheet:
        too_big = sheet_size_error(total_qty, qty_db)
        if too_big:
            return {"error": too_big}, 400
    template_id = data.get("template_id")

    # Pre-flight: if the shared job folders can't be reached, nothing is reserved,
    # allocated or written. (A job without its folder and files helps no one.)
    if not os.path.isdir(Z_DRIVE_BASE):
        return {"error": f"The job folders aren't reachable from the host PC ({Z_DRIVE_BASE}). "
                         "Nothing was created. Check that the drive or share is connected."}, 503
    if upc:
        nxt = SS.next_start(upc)
        if start_override and start_override < nxt:
            return {"error": f"Start serial {start_override:,} would reuse serials already used for this "
                             f"UPC. The next free serial is {nxt:,}.", "next_start": nxt}, 400
        if (start_override or offline_start or nxt) + total_qty - 1 > SS.MAX_SERIAL:
            return {"error": "That range runs past the largest serial an EPC can hold."}, 400

    # 1 ── reserve the job number (short lock) ─────────────────────────────────
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # No duplicate jobs. Checked inside the same lock that reserves the job number, so
        # two people entering the same ticket at the same moment can't both get through.
        dup = find_duplicate(conn, ticket, customer, title, upc, quantity)
        if dup:
            conn.rollback(); conn.close()
            return dup, 409
        job_number = next_job_number(conn)
        ts = now_str()
        # The ticket, UPC and quantity go in with the reservation so a second request that
        # arrives while this one is still being built can see it and be refused.
        cur = conn.execute(
            "INSERT INTO jobs (job_number, job_ticket, title, customer, upc, quantity, status, created_at, updated_at,"
            " data_sheet) VALUES (?, ?, ?, ?, ?, ?, 'Pending', ?, ?, ?)",
            (job_number, ticket, title, customer, upc, quantity, ts, ts, data_sheet))
        job_id = cur.lastrowid
        conn.commit()
    except Exception as e:
        conn.rollback(); conn.close()
        return {"error": f"Could not reserve a job number: {e}"}, 500
    conn.close()

    # 2 ── folders, BEFORE any serials are taken ────────────────────────────────
    job_dir = os.path.join(Z_DRIVE_BASE, *job_folder_parts(customer, label_size, po, ticket, job_number))
    try:
        if os.path.isdir(job_dir) and os.listdir(job_dir):
            job_dir = f"{job_dir} - {job_number}"       # never mix two jobs in one folder
    except OSError:
        pass
    created_new = not os.path.exists(job_dir)
    warnings, folder_ok = [], False
    try:
        for sub in ("", "Data", "Print", "Proof"):
            os.makedirs(os.path.join(job_dir, sub), exist_ok=True)
        folder_ok = True
    except Exception as e:
        _discard_job(job_id, job_dir if created_new else None)
        return {"error": f"The job folder couldn't be created, so nothing was created: {e}"}, 500

    # 3 ── serials (standard jobs only), recorded in registry + database + journal ─
    alloc = None
    if upc:
        note = f"+{overage:g}%" if overage > 0 else "base"
        try:
            alloc = SS.reserve(upc, job_number, total_qty, note, min_start=start_override,
                               who=created_by, offline_start=offline_start)
        except SS.SerialsOffline as e:
            _discard_job(job_id, job_dir if created_new else None)
            return {"error": f"{e} Nothing was created.", "serial_offline": True, **e.info}, 423
        except ValueError as e:
            _discard_job(job_id, job_dir if created_new else None)
            return {"error": str(e)}, 400
        except Exception as e:
            _discard_job(job_id, job_dir if created_new else None)
            return {"error": f"Serial allocation failed, job not created: {e}"}, 500
        if alloc["offline"]:
            warnings.append("Created while the serial registry was offline. The range is saved in the app "
                            "database and will be written to the registry when it is back.")
        if not alloc["journaled"] and SS.journal_path():
            warnings.append("The serial journal (backup folder) couldn't be written. The range is still "
                            "saved in the registry and the app database.")
    seq_alloc = None
    if data_sheet == CDO_SHEET:
        try:
            seq_alloc = SHEETS_SEQ.reserve(CDO_SHEET, job_number, total_qty, who=created_by,
                                           note=f"+{overage:g}%" if overage > 0 else "base")
        except SHEETS_SEQ.SheetSerialError as e:
            _discard_job(job_id, job_dir if created_new else None)
            return {"error": f"{e} Nothing was created."}, 400
        except Exception as e:
            _discard_job(job_id, job_dir if created_new else None)
            return {"error": f"CDO serial allocation failed, job not created: {e}"}, 500
    start_serial = alloc["start"] if alloc else (seq_alloc["start"] if seq_alloc else None)

    job = {"job_number": job_number, "customer": customer, "title": title,
           "job_ticket": ticket, "po_number": po, "po_clean": clean_po(po),
           "part_number": part, "inlay_type": inlay, "label_size": label_size,
           "due_date": due, "upc": upc, "quantity": quantity, "lpr": lpr,
           "qty_db": qty_db, "start_serial": start_serial, "overage_pct": overage}
    rolls = (roll_plan(upc, start_serial, total_qty, lpr) if upc
             else roll_plan_seq(start_serial, total_qty, lpr) if seq_alloc
             else roll_plan_plain(total_qty, lpr))
    generated = {}

    # 4 ── files ────────────────────────────────────────────────────────────────
    if folder_ok:
        if upc:
            try:
                generated["db_files"] = generate_db_files(job_dir, upc, start_serial, total_qty, qty_db)
            except Exception as e:
                warnings.append(f"Encoding database not written: {e}")
        if data_sheet:
            label = DATA_SHEETS[data_sheet]["customer"]
            try:
                if seq_alloc:
                    generated["cdo"] = generate_cdo_files(job_dir, ticket or job_number, seq_alloc["start"],
                                                          total_qty, qty_db)
                else:
                    generated["go2"] = generate_go2_files(job_dir, ticket or job_number, total_qty, qty_db)
            except Exception as e:
                # Nothing is half-written (the generator removes its own files). Any sequential
                # numbers already taken stay used (marked VOID), so they can never repeat.
                if seq_alloc:
                    SHEETS_SEQ.void(seq_alloc, f"sheet not written for {job_number}")
                _discard_job(job_id, job_dir if created_new else None)
                return {"error": f"The {label} data sheet could not be written, "
                                 f"so nothing was created: {e}"}, 500
        r = generate_checklist_html(job_dir, job)
        if not r.get("success"): warnings.append(f"Checklist not written: {r.get('error')}")
        if rolls:
            r = generate_qc_sheet(job_dir, job, rolls)
            if not r.get("success"): warnings.append(f"QC sheet not written: {r.get('error')}")

    # 5 ── template ─────────────────────────────────────────────────────────────
    template_path = None
    if folder_ok and template_id:
        res = copy_template(int(template_id), job_dir, job_number)
        if res.get("error"): warnings.append(f"Template not copied: {res['error']}")
        template_path = res.get("dest_path")

    # 6 ── save everything ──────────────────────────────────────────────────────
    conn = get_db()
    try:
        conn.execute("""UPDATE jobs SET job_ticket=?, po_number=?, part_number=?, due_date=?,
            notes=?, upc=?, quantity=?, lpr=?, qty_db=?, start_serial=?, overage_pct=?,
            label_size=?, inlay_type=?, folder_path=?, template_path=?, job_type=?, created_by=?,
            updated_at=? WHERE id=?""",
            (ticket, po, part, due, notes, upc, quantity, lpr, qty_db, start_serial, overage,
             label_size, inlay, job_dir if folder_ok else None, template_path, job_type, created_by,
             now_str(), job_id))
        _insert_rolls(conn, job_id, rolls)
        log_activity(conn, "created", job_id,
                     f"Created {job_number} for {customer}"
                     + (f", ticket {ticket}" if ticket else "")
                     + (f" (special, {DATA_SHEETS[data_sheet]['customer']} data sheet)" if data_sheet
                        else " (special, no serials)" if job_type == "special" else "")
                     + (f" — {total_qty:,} labels in {len(rolls)} rolls" if rolls else ""), created_by)
        conn.commit()
        saved = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    except Exception as e:
        conn.rollback(); conn.close()
        return {"error": f"Files were created but the job record could not be saved: {e}. "
                         f"Serials {start_serial} onward are recorded under {job_number}."}, 500
    conn.close()

    return {"success": True, "job": saved, "job_number": job_number,
            "folder_path": job_dir if folder_ok else None, "serial_alloc": alloc or seq_alloc,
            "db_split": db_file_split(total_qty, qty_db) if upc else [],
            "rolls": len(rolls), "warnings": warnings}, 201


def _discard_job(job_id, folder=None):
    """Undo a job that failed before any serials were taken: drop the reserved row and
    any folder this attempt created."""
    try:
        c = get_db()
        row = c.execute("SELECT job_number FROM jobs WHERE id=?", (job_id,)).fetchone()
        c.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        # Give the number back if nothing was issued after it (no serials were taken under it).
        if row:
            n = int(row["job_number"].split("-")[1])
            c.execute("UPDATE app_settings SET value=? WHERE key='job_number_last' AND value=?", (str(n - 1), str(n)))
        c.commit(); c.close()
    except Exception:
        pass
    if folder:
        shutil.rmtree(folder, ignore_errors=True)


def _insert_rolls(conn, job_id, rolls):
    ts = now_str()
    for r in rolls:
        conn.execute("""INSERT OR IGNORE INTO job_rolls (job_id, roll_number, quantity,
            serial_start, serial_end, epc_start, epc_end, status, created_at)
            VALUES (?,?,?,?,?,?,?,'pending',?)""",
            (job_id, r["roll_number"], r["quantity"], r["serial_start"], r["serial_end"],
             r["epc_start"], r["epc_end"], ts))


def ensure_rolls(conn, job: dict) -> list:
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM job_rolls WHERE job_id=? ORDER BY roll_number", (job["id"],))]
    if rows:
        return rows
    total = production_qty(job.get("quantity") or 0, job.get("overage_pct") or 0)
    if job.get("upc"):
        plan = roll_plan(job["upc"], job.get("start_serial"), total, job.get("lpr") or 0)
    else:
        plan = roll_plan_plain(total, job.get("lpr") or 0)
    if not plan:
        return []
    _insert_rolls(conn, job["id"], plan); conn.commit()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM job_rolls WHERE job_id=? ORDER BY roll_number", (job["id"],))]


# ══════════════════════════════════════════════════════════════════════════════
# Pages + lookups
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/")
def index():
    return render_template("index.html", version=APP_VERSION, build=BUILD)


@app.route("/favicon.ico")
def favicon():
    return Response(status=204)


# ── Shares (Z:, serial registry, journal, Master Templates, backup folder) ─────
# A dropped or slow share can take tens of seconds per look. A background thread looks every
# SHARE_EVERY seconds; the pages every browser polls (/api/stamp, /api/meta, the Data panel) read
# the result, so they answer at once whatever the share is doing. A check stuck for longer than
# SS.SHARE_STUCK_SECONDS is reported as "not answering".
SHARE_EVERY = 15
_share = {"at": None, "checking_since": None, "z_ok": None, "templates_ok": None, "backup_ok": None,
          "registry_files": 0, "journal_info": {"exists": False}, "folders": [], "folders_at": 0.0}
_share_lock = threading.Lock()


def check_shares():
    with _share_lock:
        _share["checking_since"] = time.time()
        try:
            SS.check_shares()
            s = get_settings()
            z = os.path.isdir(Z_DRIVE_BASE)
            found = {"z_ok": z, "templates_ok": os.path.isdir(s["btw_template_dir"]),
                     "backup_ok": _dir_ok(backup_dir_effective(s))}
            try:
                found["registry_files"] = sum(1 for f in os.listdir(SERIAL_REGISTRY_DIR) if f.endswith(".txt"))
            except OSError:
                found["registry_files"] = 0
            jp = SS.journal_path()
            found["journal_info"] = _file_info(jp) if jp else {"exists": False}
            if z and time.time() - _share["folders_at"] > 300:          # customer folder names, for the New job list
                try:
                    found["folders"] = sorted(e.name for e in os.scandir(Z_DRIVE_BASE)
                                              if e.is_dir() and not e.name.startswith("."))
                    found["folders_at"] = time.time()
                except OSError:
                    pass
            _share.update(found, at=time.time())
        finally:
            _share["checking_since"] = None


def share_state() -> dict:
    """The last background look at the shares (the first call looks itself if none has run yet)."""
    if _share["at"] is None and not _share["checking_since"]:
        check_shares()
    st = dict(_share)
    since = st["checking_since"]
    st["stuck"] = bool(since and time.time() - since > SS.SHARE_STUCK_SECONDS)
    if st["stuck"]:
        st.update(z_ok=False, templates_ok=False, backup_ok=False)
    return st


def start_share_watch():
    def run():
        while True:
            try:
                check_shares()
            except Exception as e:
                print(f"  [SHARE] check failed: {e}")
            time.sleep(SHARE_EVERY)
    threading.Thread(target=run, daemon=True, name="share-watch").start()


def _customer_folders():
    return share_state()["folders"]


def _dedupe(values):
    seen = {}
    for v in values:
        v = (v or "").strip()
        if v and v.lower() not in seen:
            seen[v.lower()] = v
    return sorted(seen.values(), key=str.lower)


@app.route("/api/meta")
def api_meta():
    conn = get_db()
    hist_c = [r[0] for r in conn.execute("SELECT DISTINCT customer FROM jobs")]
    hist_s = [r[0] for r in conn.execute("SELECT DISTINCT label_size FROM jobs")]
    inlays = [r[0] for r in conn.execute(
        "SELECT inlay_type FROM jobs WHERE inlay_type IS NOT NULL GROUP BY inlay_type ORDER BY COUNT(*) DESC")]
    conn.close()
    return jsonify({
        "version": APP_VERSION, "system": SYSTEM,
        "db_path": DB_PATH, "registry_dir": SERIAL_REGISTRY_DIR, "z_drive_base": Z_DRIVE_BASE,
        "z_reachable": bool(share_state()["z_ok"]), "z_shared": not Z_IS_DEFAULT,
        "customers": _dedupe(CUSTOMERS + hist_c + _customer_folders()),
        "label_sizes": _dedupe(LABEL_SIZES + hist_s),
        "inlays": _dedupe(inlays), "statuses": STATUSES, "hold_reasons": HOLD_REASONS,
        "names": _recent_names(), "serials": {**SS.health(probe=False), "registry_gaps": _registry_gap_count["n"]},
        "settings": get_settings(), "backup": backup_health(),
        "data_sheets": [{"key": k, "customer": c["customer"], "label": c["label"], "detail": c["detail"],
                         "sequential": c["sequential"]} for k, c in DATA_SHEETS.items()],
        "cdo_next": SHEETS_SEQ.next_start(CDO_SHEET),
    })


def _recent_names():
    conn = get_db()
    rows = [r[0] for r in conn.execute("""SELECT actor FROM activity WHERE actor IS NOT NULL AND actor != ''
        AND actor != 'Import' GROUP BY actor ORDER BY MAX(ts) DESC LIMIT 40""")]
    conn.close()
    return rows


@app.route("/api/stamp")
def api_stamp():
    """Cheap change marker the browser polls to know when to refresh."""
    conn = get_db()
    r = conn.execute("""SELECT (SELECT COUNT(*) FROM jobs) c, (SELECT MAX(updated_at) FROM jobs) m,
                               (SELECT MAX(id) FROM activity) a,
                               (SELECT value FROM app_settings WHERE key='priority_version') p""").fetchone()
    conn.close()
    h = {**SS.health(probe=False), "registry_gaps": _registry_gap_count["n"]}
    return jsonify({"stamp": f"{r['c']}|{r['m']}|{r['a']}|{r['p']}", "build": BUILD,
                    "serials_ok": h["registry_ok"] and not h["conflicts"], "serials": h,
                    "backup": backup_health()})


# ══════════════════════════════════════════════════════════════════════════════
# Jobs API
# ══════════════════════════════════════════════════════════════════════════════
ROLL_SUMMARY = """(SELECT COUNT(*) FROM job_rolls r WHERE r.job_id=j.id) AS roll_total,
                  (SELECT COUNT(*) FROM job_rolls r WHERE r.job_id=j.id
                     AND r.status IN ('complete','verified')) AS roll_done,
                  (SELECT COUNT(*) FROM job_rolls r WHERE r.job_id=j.id AND r.status='running') AS roll_running,
                  (SELECT COUNT(*) FROM job_rolls r WHERE r.job_id=j.id AND r.status='paused') AS roll_paused"""


@app.route("/api/jobs")
def api_jobs():
    view = request.args.get("view", "active")
    q    = request.args.get("q", "").strip()
    sql  = f"SELECT j.*, {ROLL_SUMMARY}, {proofing.PROOF_SUMMARY_SQL} FROM jobs j WHERE "
    sql += "j.archived_at IS NULL" if view == "active" else "j.archived_at IS NOT NULL"
    params = []
    for word in q.split():
        sql += (" AND (j.job_number LIKE ? OR j.customer LIKE ? OR j.title LIKE ? OR j.job_ticket LIKE ?"
                " OR j.po_number LIKE ? OR j.upc LIKE ? OR j.part_number LIKE ? OR j.inlay_type LIKE ?)")
        params += [f"%{word}%"] * 8
    sql += " ORDER BY j.created_at DESC"
    if view != "active":
        sql += " LIMIT 500"
    conn = get_db()
    rows = [dict(r) for r in conn.execute(sql, params)]
    conn.close()
    return jsonify({"jobs": rows})


ARCHIVE_SORTS = {
    "ticket":   "CAST(j.job_ticket AS INTEGER) {d}, j.job_ticket {d}",
    "customer": "j.customer COLLATE NOCASE {d}",
    "item":     "j.title COLLATE NOCASE {d}",
    "size":     "j.label_size {d}",
    "qty":      "j.quantity {d}",
    "upc":      "j.upc {d}",
    "status":   "j.status {d}",
    "archived": "j.archived_at {d}",
    "created":  "j.created_at {d}",
}


@app.route("/api/archive")
def api_archive_list():
    """Archived jobs, one page at a time: search, filters, sorting and paging happen here so the
    page stays fast however many jobs pile up."""
    a = request.args
    try:
        size = max(10, min(200, int(a.get("size", 50))))
        page = max(1, int(a.get("page", 1)))
    except ValueError:
        return err("Bad page or size")
    where, params = ["j.archived_at IS NOT NULL"], []
    for word in a.get("q", "").split():
        where.append("(j.job_number LIKE ? OR j.customer LIKE ? OR j.title LIKE ? OR j.job_ticket LIKE ?"
                     " OR j.po_number LIKE ? OR j.upc LIKE ? OR j.part_number LIKE ? OR j.inlay_type LIKE ?)")
        params += [f"%{word}%"] * 8
    if a.get("customer"):
        where.append("j.customer = ? COLLATE NOCASE"); params.append(a["customer"])
    if a.get("status"):
        where.append("j.status = ?"); params.append(a["status"])
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", a.get("from", "")):
        where.append("j.archived_at >= ?"); params.append(a["from"] + " 00:00:00")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", a.get("to", "")):
        where.append("j.archived_at <= ?"); params.append(a["to"] + " 23:59:59")
    d = "ASC" if a.get("dir") == "asc" else "DESC"
    order = ARCHIVE_SORTS.get(a.get("sort"), ARCHIVE_SORTS["archived"]).format(d=d) + ", j.id DESC"
    w = " AND ".join(where)
    conn = get_db()
    total = conn.execute(f"SELECT COUNT(*) FROM jobs j WHERE {w}", params).fetchone()[0]
    pages = max(1, -(-total // size)); page = min(page, pages)
    rows = [dict(r) for r in conn.execute(
        f"SELECT j.*, {ROLL_SUMMARY} FROM jobs j WHERE {w} ORDER BY {order} LIMIT ? OFFSET ?",
        params + [size, (page - 1) * size])]
    customers = [r[0] for r in conn.execute(
        "SELECT DISTINCT customer FROM jobs WHERE archived_at IS NOT NULL AND customer IS NOT NULL "
        "ORDER BY customer COLLATE NOCASE")]
    statuses = [r[0] for r in conn.execute(
        "SELECT DISTINCT status FROM jobs WHERE archived_at IS NOT NULL ORDER BY status")]
    conn.close()
    return jsonify({"jobs": rows, "total": total, "page": page, "pages": pages, "size": size,
                    "customers": customers, "statuses": statuses})


@app.route("/api/jobs", methods=["POST"])
def api_create_job():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return err("Send the job as a JSON object")
    result, code = create_job(data)
    return jsonify(result), code


@app.route("/api/jobs/batch", methods=["POST"])
def api_batch_create():
    body = request.get_json(silent=True)
    rows = (body.get("jobs") if isinstance(body, dict) else None) or []
    if not isinstance(rows, list) or not rows:
        return err("No rows to create")
    if len(rows) > 100:
        return err("Create at most 100 jobs at a time")
    results = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            results.append({"row": i, "ok": False, "job_number": None, "template": None,
                            "error": "This row isn't a job", "duplicate_of": None, "warnings": []})
            continue
        picked = None
        if not row.get("template_id") and not row.get("no_template"):
            m = match_templates_fresh(row.get("upc", ""), row.get("customer", ""),
                                      row.get("label_size", ""), row.get("part_number", ""))
            if m["auto_id"] and m["confidence"] in ("upc", "history"):
                row["template_id"] = m["auto_id"]
                picked = next(c["file_name"] for c in m["candidates"] if c["id"] == m["auto_id"])
        try:
            res, code = create_job(row)
        except Exception as e:
            res, code = {"error": str(e)}, 500
        results.append({"row": i, "ok": code == 201, "job_number": res.get("job_number"),
                        "template": picked if code == 201 and (res.get("job") or {}).get("template_path") else None,
                        "error": res.get("error"), "duplicate_of": res.get("duplicate_of"),
                        "warnings": res.get("warnings", [])})
    ok = sum(r["ok"] for r in results)
    return jsonify({"results": results, "created": ok, "failed": len(results) - ok})


def _load_job(conn, job_id):
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def _list_files(folder):
    out = []
    if not folder or not os.path.isdir(folder):
        return out
    for sub in ("", "Data", "Print", "Proof"):
        d = os.path.join(folder, sub)
        try:
            for e in sorted(os.scandir(d), key=lambda e: e.name.lower()):
                if e.is_file() and not e.name.startswith(("~$", ".")):
                    out.append({"path": f"{sub}/{e.name}" if sub else e.name,
                                "folder": sub or "Job folder", "name": e.name,
                                "size_kb": round(e.stat().st_size / 1024, 1)})
        except OSError:
            pass
        if len(out) > 200:
            break
    return out


@app.route("/api/jobs/<int:job_id>")
def api_job_detail(job_id):
    conn = get_db()
    job = _load_job(conn, job_id)
    if not job:
        conn.close(); return err("Job not found", 404)
    rolls = ensure_rolls(conn, job)
    notes = _notes(conn, job_id)
    floor = _floor_detail(conn, job_id, rolls)
    conn.close()
    total = production_qty(job["quantity"] or 0, job["overage_pct"] or 0) if job["quantity"] else None
    return jsonify({"job": job, "rolls": rolls, "notes": notes, "files": _list_files(job["folder_path"]), **floor,
                    "total_qty": total,
                    "end_serial": (job["start_serial"] + total - 1) if job["start_serial"] and total else None,
                    "folder_exists": bool(job["folder_path"] and os.path.isdir(job["folder_path"]))})


def _hold_input(data) -> tuple:
    """(reason, note) for putting a job on hold, or ValueError saying what is missing."""
    reason = " ".join(str(data.get("hold_reason") or "").split())
    note = " ".join(str(data.get("hold_note") or "").split())[:300]
    match = next((r for r in HOLD_REASONS if r.lower() == reason.lower()), None)
    if not match:
        raise ValueError("Choose why the job is on hold.")
    if len(note) < 3:
        raise ValueError("Say what the job is waiting for.")
    return match, note


def _span(ts) -> str:
    """How long ago a 'YYYY-MM-DD HH:MM:SS' time was, in words: 'under a minute', '25 min', '3 h', '2 days'."""
    try:
        mins = max(0, int((datetime.now() - datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")).total_seconds() // 60))
    except (TypeError, ValueError):
        return ""
    if mins < 1:
        return "under a minute"
    return f"{mins} min" if mins < 60 else f"{mins // 60} h" if mins < 48 * 60 else f"{mins // 1440} days"


@app.route("/api/jobs/<int:job_id>", methods=["PATCH"])
def api_job_edit(job_id):
    """Edit a job. Putting it On Hold needs hold_reason (one of HOLD_REASONS) and hold_note; while
    it is on hold the two can be changed on their own. Leaving On Hold clears them (the activity
    log keeps what they were)."""
    data = request.get_json(silent=True) or {}
    if "status" in data and data["status"] not in STATUSES:
        return err("Unknown status")
    sets, vals = [], []
    for f in EDITABLE:
        if f in data:
            v = data[f].strip() if isinstance(data[f], str) else data[f]
            if f in ("title", "customer") and not v:
                return err(f"{f.title()} can't be empty")
            sets.append(f"{f}=?"); vals.append(v or None)
    hold_fields = "hold_reason" in data or "hold_note" in data
    if not sets and not hold_fields:
        return err("Nothing to update")
    conn = get_db()
    before = _load_job(conn, job_id)
    if not before:
        conn.close(); return err("Job not found", 404)
    who = (data.get("operator") or "").strip()[:40] or actor()
    new_status = data.get("status", before["status"])
    new_ticket = (data["job_ticket"] if "job_ticket" in data else before.get("job_ticket")) or ""
    changed = ("job_ticket" in data and new_ticket.strip().lower() != (before.get("job_ticket") or "").strip().lower()) \
              or (before["status"] == "Cancelled" and new_status != "Cancelled")
    if changed and new_status != "Cancelled":
        same = _same_ticket(conn, new_ticket, exclude_id=job_id)
        if same:
            conn.close()
            return err(f"Ticket {new_ticket.strip()} already belongs to {', '.join(j['job_number'] for j in same)}. "
                       "Two jobs can't share a ticket.", 409)

    was_held, held = before["status"] == "On Hold", new_status == "On Hold"
    hold = None
    if held and (not was_held or hold_fields):
        given = data if not was_held else {"hold_reason": data.get("hold_reason", before.get("hold_reason")),
                                           "hold_note": data.get("hold_note", before.get("hold_note"))}
        try:
            hold = _hold_input(given)
        except ValueError as e:
            conn.close(); return err(str(e))
    elif hold_fields and not sets:
        conn.close(); return err("This job isn't on hold.")

    ts = now_str()
    sets.append("updated_at=?"); vals.append(ts)
    if hold and not was_held:
        sets += ["hold_reason=?", "hold_note=?", "hold_since=?", "hold_by=?"]; vals += [*hold, ts, who]
    elif hold:
        sets += ["hold_reason=?", "hold_note=?"]; vals += list(hold)
    elif was_held and not held:
        sets += ["hold_reason=NULL", "hold_note=NULL", "hold_since=NULL", "hold_by=NULL"]
    conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", vals + [job_id])

    if "status" in data and data["status"] != before["status"]:
        msg = f"{job_label(before)} set to {data['status']}"
        if hold:
            msg += f": {hold[0]}, {hold[1]}"
        if was_held:
            span = _span(before.get("hold_since"))
            msg += f" (was on hold{' ' + span if span else ''}{' for ' + before['hold_reason'] if before.get('hold_reason') else ''})"
        log_activity(conn, "status", job_id, msg, who)
        conn.execute("UPDATE jobs SET completed_at=? WHERE id=?",
                     (ts if data["status"] == "Complete" else None, job_id))
    elif hold and was_held and hold != (before.get("hold_reason"), before.get("hold_note")):
        log_activity(conn, "hold", job_id, f"{job_label(before)}: on hold for {hold[0]}, {hold[1]}", who)
    changed = [f.replace("_", " ") for f in EDITABLE if f != "status" and f in data
               and (data[f] or None) != (before.get(f) or None)]
    if changed:
        log_activity(conn, "edited", job_id, f"{job_label(before)}: changed {', '.join(changed)}")
    conn.commit()
    job = _load_job(conn, job_id); conn.close()
    proofing.job_changed(job_id)                     # its proofs take the new values
    return jsonify({"success": True, "job": job})


@app.route("/api/jobs/<int:job_id>/archive", methods=["POST"])
def api_archive(job_id):
    undo = (request.get_json(silent=True) or {}).get("undo")
    ts = now_str()
    conn = get_db()
    job = _load_job(conn, job_id)
    cur = conn.execute("UPDATE jobs SET archived_at=?, updated_at=? WHERE id=?",
                       (None if undo else ts, ts, job_id))
    if undo:
        conn.execute("UPDATE jobs SET completed_at=? WHERE id=? AND status='Complete'", (ts, job_id))
    if not undo and job and job["priority_rank"] is not None:
        conn.execute("UPDATE jobs SET priority_rank=NULL WHERE id=?", (job_id,))
        _renumber_priority(conn); _bump_priority(conn)
    if job:
        log_activity(conn, "archived", job_id,
                     f"{job_label(job)} {'moved back to jobs' if undo else 'archived'}")
    conn.commit(); conn.close()
    return jsonify({"success": True}) if cur.rowcount else err("Job not found", 404)


@app.route("/api/jobs/<int:job_id>", methods=["DELETE"])
def api_job_delete(job_id):
    """Removes the record only. Folder and serial registry are never touched,
    so the serial range stays used and can't be handed out again."""
    conn = get_db()
    job = _load_job(conn, job_id)
    if not job:
        conn.close(); return err("Job not found", 404)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "shelf_transactions" in tables:
        conn.execute("UPDATE shelf_transactions SET job_id=NULL WHERE job_id=?", (job_id,))
    for t in ("rewind_scans", "btw_job_assignments", "job_rolls"):
        if t in tables:
            conn.execute(f"DELETE FROM {t} WHERE job_id=?", (job_id,))
    conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    log_activity(conn, "deleted", None, f"Deleted the record for {job['job_number']} ({job_label(job)})")
    conn.commit(); conn.close()
    return jsonify({"success": True, "job_number": job["job_number"]})


@app.route("/api/jobs/<int:job_id>/rolls/<int:roll_number>", methods=["POST"])
def api_roll_update(job_id, roll_number):
    data = request.get_json(silent=True) or {}
    return _set_rolls(job_id, [roll_number], data.get("status", "complete"),
                      (data.get("operator") or "").strip()[:40] or actor(),
                      (data.get("note") or "").strip()[:2000])


@app.route("/api/jobs/<int:job_id>/rolls/<int:roll_number>/printer", methods=["POST"])
def api_roll_printer(job_id, roll_number):
    """Put a roll on a printer (or take it off). Assigning a roll that hasn't started also starts it,
    so an encoder only has to say which printer they are loading it on."""
    data = request.get_json(silent=True) or {}
    printer = " ".join(str(data.get("printer") or "").split())[:30]
    who = (data.get("operator") or "").strip()[:40] or actor()
    conn = get_db()
    job = _load_job(conn, job_id)
    if not job:
        conn.close(); return err("Job not found", 404)
    ensure_rolls(conn, job)
    row = conn.execute("SELECT status, printer FROM job_rolls WHERE job_id=? AND roll_number=?",
                       (job_id, roll_number)).fetchone()
    if not row:
        conn.close(); return err("Roll not found", 404)
    if row["status"] in ("complete", "verified"):
        conn.close(); return err("This roll is done. Reopen it before changing its printer.", 400)
    ts = now_str()
    if printer:
        if row["status"] == "pending":
            conn.execute("""UPDATE job_rolls SET printer=?, status='running', started_at=?, started_by=?
                            WHERE job_id=? AND roll_number=?""", (printer, ts, who, job_id, roll_number))
            if job["status"] == "Pending":
                conn.execute("UPDATE jobs SET status='In Progress' WHERE id=?", (job_id,))
        else:
            conn.execute("UPDATE job_rolls SET printer=? WHERE job_id=? AND roll_number=?",
                         (printer, job_id, roll_number))
        msg = f"{job_label(job)}: roll {roll_number} on {printer}"
    else:
        conn.execute("UPDATE job_rolls SET printer=NULL WHERE job_id=? AND roll_number=?", (job_id, roll_number))
        msg = f"{job_label(job)}: roll {roll_number} taken off {row['printer'] or 'its printer'}"
    conn.execute("UPDATE jobs SET updated_at=? WHERE id=?", (ts, job_id))
    log_activity(conn, "roll_start", job_id, msg + _roll_progress(conn, job_id), who)
    conn.commit(); conn.close()
    return jsonify({"success": True, "printer": printer or None})


@app.route("/api/printers")
def api_printers():
    """The configured printers and what each one is running right now (for the printer board)."""
    names = [n for n in get_settings()["printers"].split("\n") if n.strip()]
    conn = get_db()
    busy = [dict(r) for r in conn.execute("""
        SELECT r.printer, r.job_id, r.roll_number, r.quantity, r.status, r.started_by, r.started_at,
               j.customer, j.job_ticket, j.job_number, j.qty_db, j.upc,
               (SELECT COALESCE(SUM(x.quantity), 0) FROM job_rolls x
                 WHERE x.job_id = r.job_id AND x.roll_number < r.roll_number) AS rec_before,
               (SELECT COALESCE(SUM(x.quantity), 0) FROM job_rolls x WHERE x.job_id = r.job_id) AS job_total
        FROM job_rolls r JOIN jobs j ON j.id = r.job_id
        WHERE r.printer IS NOT NULL AND r.printer != '' AND r.status IN ('running', 'paused')
          AND j.archived_at IS NULL
        ORDER BY r.started_at""")]
    conn.close()
    known = {n.lower() for n in names}
    extra = sorted({b["printer"] for b in busy if b["printer"].lower() not in known})
    return jsonify({"printers": names + extra, "busy": busy})


@app.route("/api/jobs/<int:job_id>/rolls/bulk", methods=["POST"])
def api_rolls_bulk(job_id):
    """Set any selection of rolls to pending / running / complete in one go."""
    data = request.get_json(silent=True) or {}
    nums = data.get("roll_numbers") or []
    try:
        nums = sorted({int(n) for n in nums})
    except (TypeError, ValueError):
        return err("Bad roll numbers")
    if not nums:
        return err("Select at least one roll")
    return _set_rolls(job_id, nums, data.get("status", "complete"),
                      (data.get("operator") or "").strip()[:40] or actor())


def _ranges(nums):
    """[1,2,3,5,7,8] -> '1–3, 5, 7–8'"""
    out, start, prev = [], None, None
    for n in nums + [None]:
        if start is None:
            start = prev = n
        elif n is not None and n == prev + 1:
            prev = n
        else:
            out.append(f"{start}–{prev}" if prev != start else str(start))
            start = prev = n
    return ", ".join(out)


def _set_rolls(job_id, nums, status, who, note=""):
    """Roll states: pending (not started) → running (in progress) ⇄ paused → complete → verified.
    Any state can be reset to pending. An optional note (e.g. why it was paused) is
    saved on the roll. A roll whose first and last tags have passed a scan (see record_check) is
    'verified' when it is done; reopening or resetting a done roll clears that."""
    if status not in ("pending", "running", "paused", "complete"):
        return err("Unknown roll status")
    conn = get_db()
    job = _load_job(conn, job_id)
    if not job:
        conn.close(); return err("Job not found", 404)
    ensure_rolls(conn, job)
    ts, changed, words = now_str(), [], []
    for n in nums:
        row = conn.execute("SELECT status FROM job_rolls WHERE job_id=? AND roll_number=?", (job_id, n)).fetchone()
        if not row or row["status"] == status or (status == "complete" and row["status"] == "verified"):
            continue
        before = row["status"]
        if before in ("complete", "verified") or status == "pending":
            conn.execute("""UPDATE job_rolls SET verified_at=NULL, verified_by=NULL
                            WHERE job_id=? AND roll_number=?""", (job_id, n))
            if status == "pending" or before == "verified":
                conn.execute("""UPDATE job_rolls SET first_ok_at=NULL, last_ok_at=NULL
                                WHERE job_id=? AND roll_number=?""", (job_id, n))
        if status == "complete":
            conn.execute("""UPDATE job_rolls SET status='complete', completed_at=?, completed_by=?,
                started_at=COALESCE(started_at, ?), started_by=COALESCE(started_by, ?), paused_at=NULL, paused_by=NULL
                WHERE job_id=? AND roll_number=?""", (ts, who, ts, who, job_id, n))
            words.append("done")
            if _maybe_verify(conn, job_id, n, ts):
                words[-1] = "done (first and last tags already checked: verified)"
        elif status == "running":
            # Start, resume after a pause, or reopen a finished roll.
            conn.execute("""UPDATE job_rolls SET status='running',
                started_at=CASE WHEN status='paused' THEN started_at ELSE ? END,
                started_by=CASE WHEN status='paused' THEN started_by ELSE ? END,
                paused_at=NULL, paused_by=NULL, completed_at=NULL, completed_by=NULL
                WHERE job_id=? AND roll_number=?""", (ts, who, job_id, n))
            words.append({"paused": "left in progress", "complete": "reopened"}.get(before, "left in progress"))
        elif status == "paused":
            conn.execute("""UPDATE job_rolls SET status='paused', paused_at=?, paused_by=?,
                started_at=COALESCE(started_at, ?), started_by=COALESCE(started_by, ?), completed_at=NULL, completed_by=NULL
                WHERE job_id=? AND roll_number=?""", (ts, who, ts, who, job_id, n))
            words.append("paused")
        else:
            conn.execute("""UPDATE job_rolls SET status='pending', started_at=NULL, started_by=NULL,
                paused_at=NULL, paused_by=NULL, completed_at=NULL, completed_by=NULL, printer=NULL
                WHERE job_id=? AND roll_number=?""", (job_id, n))
            words.append("reset to not started")
        changed.append(n)
    if changed:
        if status in ("running", "paused", "complete") and job["status"] == "Pending":
            conn.execute("UPDATE jobs SET status='In Progress' WHERE id=?", (job_id,))
        conn.execute("UPDATE jobs SET updated_at=? WHERE id=?", (ts, job_id))
        if note and len(changed) == 1:
            conn.execute("INSERT INTO job_notes (job_id, roll_number, author, body, important, created_at)"
                         " VALUES (?,?,?,?,0,?)", (job_id, changed[0], who, note, ts))
        word = words[0] if len(set(words)) == 1 else {"complete": "done", "running": "started",
                                                      "paused": "paused", "pending": "reset"}[status]
        label = "roll" if len(changed) == 1 else "rolls"
        kind = {"complete": "roll", "running": "roll_start", "paused": "roll_pause", "pending": "roll_undo"}[status]
        log_activity(conn, kind, job_id, f"{job_label(job)}: {label} {_ranges(changed)} {word}"
                     + (f" — {note[:80]}" if note else "") + _roll_progress(conn, job_id), who)
    nxt = _next_on_printer(conn, job_id, changed[0]) if status == "complete" and len(changed) == 1 else None
    conn.commit(); conn.close()
    return jsonify({"success": True, "changed": changed, "next": nxt})


def _maybe_verify(conn, job_id, n, ts=None, who=None) -> bool:
    """A done roll whose first and last tags have both passed becomes 'verified'."""
    cur = conn.execute("""UPDATE job_rolls SET status='verified', verified_at=?,
                              verified_by=COALESCE(?, (SELECT c.who FROM roll_checks c WHERE c.job_id=job_rolls.job_id
                                  AND c.roll_number=job_rolls.roll_number AND c.result='pass' ORDER BY c.id DESC LIMIT 1))
                          WHERE job_id=? AND roll_number=? AND status='complete'
                            AND first_ok_at IS NOT NULL AND last_ok_at IS NOT NULL""", (ts or now_str(), who, job_id, n))
    return cur.rowcount > 0


def _next_on_printer(conn, job_id, done_roll):
    """After a roll on a printer is done: the next roll of this job not started yet, to offer
    "Start roll N on <printer>". None if the roll had no printer, nothing is left, or that printer
    is already running something else."""
    r = conn.execute("SELECT printer FROM job_rolls WHERE job_id=? AND roll_number=?", (job_id, done_roll)).fetchone()
    if not r or not r["printer"]:
        return None
    busy = conn.execute("""SELECT 1 FROM job_rolls x JOIN jobs j ON j.id=x.job_id WHERE x.printer=? COLLATE NOCASE
                           AND x.status='running' AND j.archived_at IS NULL LIMIT 1""", (r["printer"],)).fetchone()
    if busy:
        return None
    nxt = conn.execute("""SELECT roll_number, quantity FROM job_rolls WHERE job_id=? AND status='pending'
                          ORDER BY (roll_number < ?), roll_number LIMIT 1""", (job_id, done_roll)).fetchone()
    if not nxt:
        return None
    return {"roll_number": nxt["roll_number"], "quantity": nxt["quantity"], "printer": r["printer"],
            "can_print": bool(get_settings()["bt_enabled"])}


def _roll_progress(conn, job_id):
    r = conn.execute("""SELECT COUNT(*) t, SUM(status IN ('complete','verified')) d
                        FROM job_rolls WHERE job_id=?""", (job_id,)).fetchone()
    return f" ({r['d'] or 0} of {r['t']})" if r["t"] else ""


@app.route("/api/jobs/<int:job_id>/rolls/done-through", methods=["POST"])
def api_rolls_done_through(job_id):
    """Catch-up: mark every unfinished roll up to and including roll N as done."""
    data = request.get_json(silent=True) or {}
    try:
        upto = int(data.get("roll_number"))
    except (TypeError, ValueError):
        return err("Enter a roll number")
    conn = get_db()
    job = _load_job(conn, job_id)
    if not job:
        conn.close(); return err("Job not found", 404)
    ensure_rolls(conn, job)
    who = (data.get("operator") or "").strip()[:40] or actor()
    cur = conn.execute("""UPDATE job_rolls SET status='complete', completed_at=?, completed_by=?
                          WHERE job_id=? AND roll_number<=? AND status='pending'""",
                       (now_str(), who, job_id, upto))
    if cur.rowcount:
        if job["status"] == "Pending":
            conn.execute("UPDATE jobs SET status='In Progress' WHERE id=?", (job_id,))
        conn.execute("UPDATE jobs SET updated_at=? WHERE id=?", (now_str(), job_id))
        log_activity(conn, "roll", job_id, f"{job_label(job)}: {cur.rowcount} rolls done through roll {upto}"
                     + _roll_progress(conn, job_id), who)
    conn.commit(); conn.close()
    return jsonify({"success": True, "marked": cur.rowcount})


# ── Notes ─────────────────────────────────────────────────────────────────────
def _notes(conn, job_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM job_notes WHERE job_id=? ORDER BY important DESC, id DESC", (job_id,))]


@app.route("/api/jobs/<int:job_id>/notes", methods=["POST"])
def api_note_add(job_id):
    data = request.get_json(silent=True) or {}
    body = (data.get("body") or "").strip()
    if not body:
        return err("Write a note first")
    roll = data.get("roll_number") or None
    important = 1 if data.get("important") else 0
    conn = get_db()
    job = _load_job(conn, job_id)
    if not job:
        conn.close(); return err("Job not found", 404)
    who = (data.get("author") or "").strip()[:40] or actor()
    conn.execute("INSERT INTO job_notes (job_id, roll_number, author, body, important, created_at)"
                 " VALUES (?,?,?,?,?,?)", (job_id, roll, who, body[:2000], important, now_str()))
    short = body if len(body) <= 90 else body[:87] + "…"
    log_activity(conn, "note_important" if important else "note", job_id,
                 f"{job_label(job)}{f' roll {roll}' if roll else ''}: {short}", who)
    conn.execute("UPDATE jobs SET updated_at=? WHERE id=?", (now_str(), job_id))
    conn.commit()
    notes = _notes(conn, job_id); conn.close()
    return jsonify({"success": True, "notes": notes})


@app.route("/api/notes/<int:note_id>", methods=["PATCH", "DELETE"])
def api_note_change(note_id):
    conn = get_db()
    note = conn.execute("SELECT * FROM job_notes WHERE id=?", (note_id,)).fetchone()
    if not note:
        conn.close(); return err("Note not found", 404)
    if request.method == "DELETE":
        conn.execute("DELETE FROM job_notes WHERE id=?", (note_id,))
    else:
        imp = 1 if (request.get_json(silent=True) or {}).get("important") else 0
        conn.execute("UPDATE job_notes SET important=? WHERE id=?", (imp, note_id))
    conn.execute("UPDATE jobs SET updated_at=? WHERE id=?", (now_str(), note["job_id"]))
    conn.commit()
    notes = _notes(conn, note["job_id"]); conn.close()
    return jsonify({"success": True, "notes": notes})


# ── Roll tracking page + activity feed ────────────────────────────────────────
@app.route("/api/floor")
def api_floor():
    """Open jobs that have rolls, with progress, next roll and note counts."""
    conn = get_db()
    rows = [dict(r) for r in conn.execute(f"""
        SELECT j.id, j.job_number, j.job_ticket, j.customer, j.title, j.status, j.due_date,
               j.label_size, j.inlay_type, j.upc, j.created_at, j.notes,
               j.hold_reason, j.hold_note, j.hold_since, j.hold_by, {ROLL_SUMMARY},
               (SELECT MIN(roll_number) FROM job_rolls r WHERE r.job_id=j.id AND r.status='pending') AS next_roll,
               (SELECT GROUP_CONCAT(roll_number || ':' || COALESCE(started_by, '') || ':' || COALESCE(printer, ''), ',') FROM job_rolls r
                  WHERE r.job_id=j.id AND r.status='running') AS running_rolls,
               (SELECT COUNT(*) FROM job_rolls r WHERE r.job_id=j.id AND r.status='paused') AS paused_count,
               (SELECT COUNT(*) FROM job_notes n WHERE n.job_id=j.id) AS note_count,
               (SELECT COUNT(*) FROM job_notes n WHERE n.job_id=j.id AND n.important=1) AS important_count,
               (SELECT MAX(ts) FROM activity a WHERE a.job_id=j.id) AS last_activity
        FROM jobs j
        WHERE j.archived_at IS NULL AND j.status IN ('Pending','In Progress','On Hold')
          AND EXISTS (SELECT 1 FROM job_rolls r WHERE r.job_id=j.id)""")]
    conn.close()
    order = {"In Progress": 0, "On Hold": 1, "Pending": 2}
    # In Progress / On Hold: most recently touched first. Pending: oldest first.
    running = sorted([r for r in rows if r["status"] != "Pending"],
                     key=lambda r: (order[r["status"]], -(_ts_num(r["last_activity"]))))
    queued = sorted([r for r in rows if r["status"] == "Pending"], key=lambda r: r["created_at"])
    return jsonify({"jobs": running + queued})


def _ts_num(ts):
    return int(re.sub(r"\D", "", ts or "") or 0)


@app.route("/api/activity")
def api_activity():
    limit = min(int(request.args.get("limit", 40) or 40), 200)
    job_id = request.args.get("job_id")
    sql = """SELECT a.*, j.job_ticket, j.customer, j.job_number FROM activity a
             LEFT JOIN jobs j ON j.id = a.job_id"""
    params = []
    if job_id:
        sql += " WHERE a.job_id=?"; params.append(int(job_id))
    sql += " ORDER BY a.id DESC LIMIT ?"; params.append(limit)
    conn = get_db()
    rows = [dict(r) for r in conn.execute(sql, params)]
    conn.close()
    return jsonify({"items": rows})


# ══════════════════════════════════════════════════════════════════════════════
# Rewind page: for the rewind operators on tablets and phones (opened by QR code)
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/rewind")
def rewind_page():
    return render_template("rewind.html", version=APP_VERSION, build=BUILD)


def rewind_link():
    """(url, where it came from, usable_by_other_devices). The address other devices
    should open: the one set in Settings, else the address this request used, else this
    PC's network address."""
    base = (get_settings().get("rewind_base_url") or "").strip().rstrip("/")
    if base:
        return (base if base.lower().startswith("http") else "http://" + base) + "/rewind", "set in Settings", True
    host = request.host
    hostname = host.rsplit(":", 1)[0].strip("[]").lower() if not host.startswith("[") else "::1"
    if hostname not in ("localhost", "127.0.0.1", "::1"):
        return f"http://{host}/rewind", "the address this page was opened with", True
    ips = _lan_ips()
    private = [i for i in ips if i.startswith(("10.", "192.168.")) or re.match(r"172\.(1[6-9]|2\d|3[01])\.", i)]
    pick = (private or ips or [None])[0]
    if pick:
        return f"http://{pick}:{PORT}/rewind", "this PC's network address", True
    return f"http://localhost:{PORT}/rewind", "this PC only", False


@app.route("/api/rewind/link")
def api_rewind_link():
    url, source, shareable = rewind_link()
    try:
        import segno  # noqa: F401
        qr = True
    except ImportError:
        qr = False
    return jsonify({"url": url, "source": source, "shareable": shareable, "qr": qr})


@app.route("/rewind/qr.svg")
def rewind_qr_svg():
    try:
        import segno
    except ImportError:
        return Response("The QR package isn't installed. Run run.bat again, or: pip install segno", status=501)
    buf = io.BytesIO()
    segno.make(rewind_link()[0], error="m").save(buf, kind="svg", scale=10, border=3, omitsize=True,
                                                 dark="#111111", light="#ffffff")
    return Response(buf.getvalue(), mimetype="image/svg+xml", headers={"Cache-Control": "no-store"})


@app.route("/rewind/qr")
def rewind_qr_sign():
    """A printable sign with the QR code, to put up next to the rewind stations."""
    url, source, shareable = rewind_link()
    try:
        import segno  # noqa: F401
        qr = '<img src="/rewind/qr.svg" alt="QR code" style="width:5.2in;height:5.2in">'
    except ImportError:
        qr = '<p style="font-size:14pt">The QR package isn\'t installed (run run.bat again). Type the address below instead.</p>'
    warn = ("" if shareable else '<p style="color:#a23b3b;font-size:13pt">This address only works on this PC. '
            "Set the address the tablets use in Settings, or open this page from another PC.</p>")
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Rewind: scan to open</title>
<style>body{{font-family:"Segoe UI",Arial,sans-serif;text-align:center;margin:.5in;color:#111}}
h1{{font-size:44pt;margin:.1in 0 0}}p.s{{font-size:18pt;margin:.05in 0 .25in;color:#444}}
.u{{font-family:Consolas,monospace;font-size:20pt;margin-top:.15in;word-break:break-all}}
@page{{size:letter;margin:.4in}}@media print{{button{{display:none}}}}</style></head><body>
<h1>REWIND</h1><p class="s">Scan with the tablet camera, then pick the job you are rewinding</p>
{qr}<div class="u">{escape(url)}</div>{warn}
<p><button onclick="print()" style="font-size:16pt;padding:8px 20px;margin-top:.2in">Print</button></p></body></html>"""
    return Response(html, mimetype="text/html")


_REWIND_FIELDS = """j.id, j.job_number, j.job_ticket, j.customer, j.title, j.status, j.job_type, j.upc, j.label_size,
    j.inlay_type, j.quantity, j.lpr, j.overage_pct, j.due_date, j.part_number, j.po_number, j.created_at,
    j.updated_at, j.priority_rank, j.hold_reason, j.hold_note, j.hold_since, j.hold_by"""


@app.route("/api/rewind/jobs")
def api_rewind_jobs():
    """Jobs the rewind operators pick from.
    view=open (default): every open job in encoding: priority jobs first, then In Progress,
    then newest first, so a job created a minute ago is near the top.
    view=running: only jobs marked In Progress. view=done: recently completed."""
    view = request.args.get("view", "open")
    where = {"done": "j.status='Complete'",
             "open": "j.status IN ('Pending','In Progress','On Hold')"}.get(view, "j.status='In Progress'")
    order = ("j.updated_at DESC LIMIT 40" if view == "done" else
             "(j.priority_rank IS NULL), j.priority_rank, CASE j.status WHEN 'In Progress' THEN 0 "
             "WHEN 'Pending' THEN 1 ELSE 2 END, j.created_at DESC")
    conn = get_db()
    rows = [dict(r) for r in conn.execute(f"""SELECT {_REWIND_FIELDS},
        (SELECT COUNT(*) FROM job_rolls r WHERE r.job_id=j.id) AS roll_total,
        (SELECT COUNT(*) FROM job_rolls r WHERE r.job_id=j.id AND r.status IN ('complete','verified')) AS roll_done,
        (SELECT COUNT(*) FROM job_rolls r WHERE r.job_id=j.id AND r.status IN ('running','paused')) AS roll_running,
        (SELECT COUNT(*) FROM job_notes n WHERE n.job_id=j.id AND n.important=1) AS important_count,
        (SELECT COUNT(*) FROM job_notes n WHERE n.job_id=j.id) AS note_count
        FROM jobs j WHERE j.archived_at IS NULL AND {where} ORDER BY {order}""")]
    count = lambda w: conn.execute(f"SELECT COUNT(*) FROM jobs j WHERE j.archived_at IS NULL AND {w}").fetchone()[0]
    counts = {"running": count("j.status='In Progress'"), "open": count("j.status IN ('Pending','In Progress','On Hold')")}
    conn.close()
    return jsonify({"jobs": rows, "counts": counts, "open_count": counts["open"], "now": now_str()})


@app.route("/api/rewind/jobs/<int:job_id>")
def api_rewind_job(job_id):
    """One job with its roll data and notes. Light on purpose: no file listing."""
    conn = get_db()
    row = conn.execute(f"SELECT {_REWIND_FIELDS}, j.notes AS ticket_notes, j.archived_at, j.inlay_type "
                       "FROM jobs j WHERE j.id=?", (job_id,)).fetchone()
    if not row:
        conn.close(); return err("Job not found", 404)
    job = dict(row)
    full = _load_job(conn, job_id)
    rolls = ensure_rolls(conn, full)
    notes = _notes(conn, job_id)
    conn.close()
    keep = ("roll_number", "quantity", "serial_start", "serial_end", "epc_start", "epc_end", "status",
            "started_at", "started_by", "paused_at", "paused_by", "completed_at", "completed_by",
            "first_ok_at", "last_ok_at", "verified_at", "verified_by")
    return jsonify({"job": job, "rolls": [{k: r.get(k) for k in keep} for r in rolls], "notes": notes,
                    "total_qty": production_qty(job["quantity"] or 0, job["overage_pct"] or 0) if job["quantity"] else None})


def _clean_epc(raw):
    t = re.sub(r"[\s:\-_]", "", (raw or "").strip()).upper()
    return t[2:] if t.startswith("0X") else t


def _roll_detail(conn, o, serial=None):
    """One roll as the rewind page shows it, plus where a scanned tag sits on it."""
    total = conn.execute("SELECT COUNT(*) FROM job_rolls WHERE job_id=?", (o["job_id"],)).fetchone()[0]
    d = {k: o.get(k) for k in ("job_id", "job_number", "job_ticket", "customer", "title", "upc", "roll_number",
                               "quantity", "serial_start", "serial_end", "epc_start", "epc_end", "status",
                               "started_by", "started_at", "paused_by", "paused_at", "completed_by", "completed_at")}
    d["rolls_total"] = total
    if serial is not None and o.get("serial_start"):
        d.update(serial=serial, label_no=serial - o["serial_start"] + 1,
                 position="first" if serial == o["serial_start"] else "last" if serial == o["serial_end"] else "middle")
    return d


def _lookup(conn, raw, job_id=None):
    """What is this tag? Read-only: nothing is recorded."""
    t = _clean_epc(raw)
    out = {"raw": (raw or "")[:60], "epc": t, "note": None}

    def res(result, message, **extra):
        return {**out, "result": result, "message": message, **extra}

    if not t:
        return res("invalid", "Nothing was scanned.")
    if not re.fullmatch(r"[0-9A-F]+", t):
        runs = re.findall(r"(?<![0-9A-F])[0-9A-F]{24}(?![0-9A-F])", t)
        if len(runs) != 1:
            return res("invalid", "That isn't hex. Serial Magic may be sending the tag as text or decimal. "
                                  "Set it to send the EPC as hex.")
        t = out["epc"] = runs[0]
        out["note"] = "Extra text around the tag was ignored. Clear the prefix and suffix in Serial Magic Keys settings."
    if len(t) != 24:
        return res("invalid", f"That is {len(t)} characters; an EPC is 24. Check that Serial Magic sends the full EPC.")
    if t.startswith("E2"):
        return res("tid", "That looks like a TID (it starts with E2), not an EPC. Set the reader to report the EPC.")
    if not t.startswith("30") or format(int(t, 16), "096b")[11:14] != "101":
        return res("invalid", "That isn't one of our EPCs (they start with 30).")
    d = decode_epc(t)
    upc, serial = d["upc"], d["serial"]
    owners = [dict(o) for o in conn.execute(
        """SELECT r.*, j.job_number, j.job_ticket, j.customer, j.title, j.upc, j.archived_at
           FROM job_rolls r JOIN jobs j ON j.id=r.job_id
           WHERE j.upc=? AND r.serial_start<=? AND r.serial_end>=?
           ORDER BY (j.archived_at IS NULL) DESC, j.id DESC""", (upc, serial, serial))]
    if job_id:
        mine = [o for o in owners if o["job_id"] == job_id]
        if mine:
            return res("found", "", roll=_roll_detail(conn, mine[0], serial))
        if owners:
            o = owners[0]
            return res("other_job", f"This tag is from a different job: {o['customer']}, ticket "
                                    f"{o['job_ticket'] or o['job_number']}, roll {o['roll_number']}.",
                       roll=_roll_detail(conn, o, serial))
        job = conn.execute("SELECT upc FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job and job["upc"] and job["upc"] != upc:
            return res("wrong_product", f"Wrong product: this tag is for UPC {upc}; this job is UPC {job['upc']}.")
    else:
        if len(owners) == 1:
            return res("found", "", roll=_roll_detail(conn, owners[0], serial))
        if len(owners) > 1:
            return res("ambiguous", "That serial is in more than one job. Open the job first, then scan.")
    hit = SS.check_range(upc, serial, serial)
    return res("not_issued", (f"Serial {serial:,} was issued under {', '.join(hit['jobs'])}, but no roll here holds it."
                              if hit["covered"] else f"UPC {upc}, serial {serial:,}: this tag was not made by Job Manager."))


CHECK_FAIL = ("other_job", "wrong_product", "not_issued")      # a real tag that doesn't belong here


def record_check(conn, job_id, expected_roll, res, source, who):
    """Keep a scan made against an open job (tablet or desktop). Scanner setup problems (not hex,
    a TID, wrong length) aren't kept: they say nothing about the roll. A pass on a roll's first or
    last tag is marked on the roll; when both have passed and the roll is done, it is verified.
    A tag from another roll of the same job, when a roll was open, is a fail for the open roll."""
    result, roll = res["result"], res.get("roll") or {}
    if result != "found" and result not in CHECK_FAIL:
        return None
    ts = now_str()
    on_roll = roll.get("roll_number") if result == "found" else expected_roll
    ok, msg = result == "found", res.get("message") or ""
    if ok and expected_roll and roll.get("roll_number") != expected_roll:
        ok, on_roll = False, expected_roll
        msg = f"Tag {roll.get('label_no')} of roll {roll.get('roll_number')}, not roll {expected_roll}."
    elif ok:
        msg = f"Tag {roll.get('label_no'):,} of {roll.get('quantity'):,}" + (
            f" ({roll['position'].upper()} tag)" if roll.get("position") in ("first", "last") else "")
    conn.execute("""INSERT INTO roll_checks (job_id, roll_number, epc, serial, position, result, message, source, who, ts)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                 (job_id, on_roll, res.get("epc"), roll.get("serial"), roll.get("position") if ok else None,
                  "pass" if ok else "fail", msg[:300], source, who, ts))
    out = {"recorded": True, "result": "pass" if ok else "fail", "roll_number": on_roll, "verified": False}
    job = _load_job(conn, job_id)
    if ok and roll.get("serial") is not None:
        if roll["serial"] == roll.get("serial_start"):
            conn.execute("UPDATE job_rolls SET first_ok_at=COALESCE(first_ok_at, ?) WHERE job_id=? AND roll_number=?", (ts, job_id, on_roll))
        if roll["serial"] == roll.get("serial_end"):
            conn.execute("UPDATE job_rolls SET last_ok_at=COALESCE(last_ok_at, ?) WHERE job_id=? AND roll_number=?", (ts, job_id, on_roll))
        if _maybe_verify(conn, job_id, on_roll, ts, who):
            out["verified"] = True
            log_activity(conn, "roll_verify", job_id, f"{job_label(job)}: roll {on_roll} verified (first and last tags scanned)", who)
    elif not ok:
        log_activity(conn, "check_fail", job_id, f"{job_label(job)}: tag check failed"
                     + (f" on roll {on_roll}" if on_roll else "") + f" — {msg[:120]}", who)
    r = conn.execute("SELECT first_ok_at, last_ok_at, status FROM job_rolls WHERE job_id=? AND roll_number=?",
                     (job_id, on_roll)).fetchone() if on_roll else None
    if r:
        out.update(first_ok=bool(r["first_ok_at"]), last_ok=bool(r["last_ok_at"]), roll_status=r["status"])
    conn.commit()
    return out


@app.route("/api/rewind/lookup", methods=["POST"])
def api_rewind_lookup():
    """What is this tag? With a job open, the scan is also kept as a tag check (record_check);
    roll_number = the roll the person has open, if any. record=false looks only."""
    data = request.get_json(silent=True) or {}
    jid = data.get("job_id")
    jid = int(jid) if str(jid or "").isdigit() else None
    rn = data.get("roll_number")
    rn = int(rn) if str(rn or "").isdigit() else None
    conn = get_db()
    try:
        res = _lookup(conn, str(data.get("raw") or ""), jid)
        if jid and data.get("record", True) is not False and _load_job(conn, jid):
            res["check"] = record_check(conn, jid, rn, res, str(data.get("source") or "rewind")[:12],
                                        (data.get("operator") or "").strip()[:40] or actor())
        return jsonify(res)
    finally:
        conn.close()


@app.route("/jobs/<int:job_id>/roll-sheet")
def roll_sheet(job_id):
    """The QC / roll tracking sheet, same layout as the file in the job folder.
    ?print=1 opens the print dialog."""
    conn = get_db()
    job = _load_job(conn, job_id)
    if not job:
        conn.close(); abort(404)
    rolls = ensure_rolls(conn, job); conn.close()
    html, _ = qc_html(job, rolls, auto_print=request.args.get("print") == "1")
    return Response(html, mimetype="text/html")


@app.route("/api/jobs/<int:job_id>/regenerate", methods=["POST"])
def api_regenerate(job_id):
    """Rewrite the checklist and QC sheet from current job data. Never touches
    serials or the encoding database files."""
    conn = get_db()
    job = _load_job(conn, job_id)
    if not job:
        conn.close(); return err("Job not found", 404)
    rolls = ensure_rolls(conn, job); conn.close()
    folder = job["folder_path"]
    if not folder or not os.path.isdir(folder):
        return err("The job folder isn't reachable")
    job["po_clean"] = clean_po(job["po_number"])
    rebuilt = False
    data_dir = os.path.join(folder, "Data")
    has_db = os.path.isdir(data_dir) and any(f.lower().endswith((".xlsx", ".csv")) for f in os.listdir(data_dir))
    if job["upc"] and job["start_serial"] and job["quantity"] and not has_db:
        try:     # same serials as recorded for this job; never allocates new ones
            generate_db_files(folder, job["upc"], job["start_serial"],
                              production_qty(job["quantity"], job["overage_pct"] or 0), job["qty_db"])
            rebuilt = True
        except Exception as e:
            return err(f"Encoding database not rebuilt: {e}", 500)
    out = [generate_checklist_html(folder, job)]
    if rolls:
        out.append(generate_qc_sheet(folder, job, rolls))
    errors = [r["error"] for r in out if not r.get("success")]
    return err("; ".join(errors), 500) if errors else jsonify({"success": True, "rebuilt_database": rebuilt})


@app.route("/api/jobs/<int:job_id>/template", methods=["POST"])
def api_job_template(job_id):
    tid = (request.get_json(silent=True) or {}).get("template_id")
    conn = get_db()
    job = _load_job(conn, job_id); conn.close()
    if not job:
        return err("Job not found", 404)
    if not job["folder_path"] or not os.path.isdir(job["folder_path"]):
        return err("The job folder isn't reachable")
    res = copy_template(int(tid or 0), job["folder_path"], job["job_number"])
    if res.get("error"):
        return err(res["error"])
    conn = get_db()
    conn.execute("UPDATE jobs SET template_path=?, updated_at=? WHERE id=?",
                 (res["dest_path"], now_str(), job_id))
    log_activity(conn, "template", job_id, f"{job_label(job)}: template {os.path.basename(res['dest_path'])} copied into Print")
    conn.commit(); conn.close()
    proofing.job_changed(job_id)
    return jsonify({"success": True, "dest_path": res["dest_path"]})


@app.route("/api/jobs/<int:job_id>/file/<path:rel>")
def api_job_file(job_id, rel):
    conn = get_db()
    job = _load_job(conn, job_id); conn.close()
    if not job or not job["folder_path"]:
        abort(404)
    root   = os.path.realpath(job["folder_path"])
    target = os.path.realpath(os.path.join(root, rel))
    if os.path.commonpath([root, target]) != root or not os.path.isfile(target):
        abort(404)
    return send_file(target, as_attachment=not target.lower().endswith((".html", ".pdf", ".png", ".jpg")))


@app.route("/api/jobs/<int:job_id>/upload", methods=["POST"])
def api_job_upload(job_id):
    """Drop files (proofs, customer art, reports) into the job folder from any PC."""
    sub = request.form.get("folder", "Proof")
    if sub not in ("Proof", "Print", "Data", ""):
        return err("Unknown folder")
    files = request.files.getlist("files")
    if not files:
        return err("Choose at least one file")
    conn = get_db()
    job = _load_job(conn, job_id)
    if not job or not job["folder_path"] or not os.path.isdir(job["folder_path"]):
        conn.close(); return err("The job folder isn't reachable")
    dest_dir = os.path.join(job["folder_path"], sub)
    os.makedirs(dest_dir, exist_ok=True)
    saved = []
    for f in files:
        name = _safe_name(os.path.basename(f.filename or "").strip())
        if not name:
            continue
        stem, ext = os.path.splitext(name)
        target, n = os.path.join(dest_dir, name), 2
        while os.path.exists(target):          # never overwrite
            target = os.path.join(dest_dir, f"{stem} ({n}){ext}"); n += 1
        f.save(target)
        saved.append(os.path.basename(target))
    if saved:
        log_activity(conn, "upload", job_id,
                     f"{job_label(job)}: added {', '.join(saved[:3])}{' and more' if len(saved) > 3 else ''} to {sub or 'job folder'}")
        conn.execute("UPDATE jobs SET updated_at=? WHERE id=?", (now_str(), job_id))
        conn.commit()
    conn.close()
    return jsonify({"success": True, "saved": saved})


_HELPER_VBS = r'''Option Explicit
' Job Manager: opens a job folder from a jobmanager:// link
Dim arg, hexs, p, i, v, roots, r, rr, ok, fso
If WScript.Arguments.Count = 0 Then WScript.Quit
arg = WScript.Arguments(0)
i = InStr(1, arg, "open/", vbTextCompare)
If i = 0 Then WScript.Quit
hexs = Mid(arg, i + 5)
p = ""
For i = 1 To Len(hexs) - 3 Step 4
  v = HexVal(Mid(hexs, i, 4))
  If v < 0 Then Exit For
  p = p & ChrW(v)
Next
roots = Split("__ROOTS__", "|")
ok = False
For Each r In roots
  rr = r
  If Right(rr, 1) <> "\" And Right(rr, 1) <> "/" Then rr = rr & "\"
  If LCase(Left(p, Len(rr))) = LCase(rr) Then ok = True
Next
If InStr(p, "..") > 0 Then ok = False
If Not ok Then
  MsgBox "That folder is outside the job folders:" & vbCrLf & p, 48, "Job Manager"
  WScript.Quit
End If
Set fso = CreateObject("Scripting.FileSystemObject")
If Not fso.FolderExists(p) Then
  MsgBox "Folder not found:" & vbCrLf & p, 48, "Job Manager"
  WScript.Quit
End If
CreateObject("Shell.Application").Explore p
Function HexVal(s)
  Dim k, d
  HexVal = 0
  For k = 1 To Len(s)
    d = InStr("0123456789ABCDEF", UCase(Mid(s, k, 1)))
    If d = 0 Then
      HexVal = -1
      Exit Function
    End If
    HexVal = HexVal * 16 + (d - 1)
  Next
End Function
'''


def _helper_roots() -> list:
    """Folders Open folder may open: the configured job-folder root plus the root every
    existing job folder lives under (layout is root\\customer\\size\\job). This covers
    jobs saved with Z:\\ paths and jobs saved with \\\\server\\share paths."""
    roots = [Z_DRIVE_BASE]
    conn = get_db()
    paths = [r[0] for r in conn.execute("SELECT folder_path FROM jobs WHERE folder_path IS NOT NULL")]
    conn.close()
    seen = {}
    for p in paths:
        parts = re.split(r"[\\/]", p)
        if len(parts) >= 5:
            root = ("\\" if "\\" in p else "/").join(parts[:-3])
            seen[root] = seen.get(root, 0) + 1
    # A root must be shared by several jobs, so one oddly-placed folder can't widen access.
    for root, n in sorted(seen.items(), key=lambda kv: -kv[1]):
        if n >= 5 and root.lower() not in (r.lower() for r in roots):
            roots.append(root)
    return roots[:8]


@app.route("/helper/open-folder-setup.cmd")
def helper_installer():
    """One-time, per-PC setup (no admin rights, no background program): registers a
    jobmanager:// link type so the Open folder button opens Explorer on that PC."""
    roots = "|".join(r.replace('"', '""') for r in _helper_roots())
    vbs = _HELPER_VBS.replace("__ROOTS__", roots)
    def bat_echo(line):
        # cmd.exe: ^-escape special characters outside quotes only; % always doubles.
        out, quoted = [], False
        for ch in line:
            if ch == '"':
                quoted = not quoted
            elif ch == "%":
                ch = "%%"
            elif ch in "^&<>|" and not quoted:
                ch = "^" + ch
            out.append(ch)
        return '>>"%F%" echo(' + "".join(out)
    lines = ["@echo off",
             "echo Setting up Job Manager Open folder for this Windows user...",
             'set "D=%APPDATA%\\JobManager"',
             'if not exist "%D%" mkdir "%D%"',
             'set "F=%D%\\open-folder.vbs"',
             'if exist "%F%" del "%F%"']
    lines += [bat_echo(l) for l in vbs.splitlines()]
    lines += ['reg add "HKCU\\Software\\Classes\\jobmanager" /ve /d "URL:Job Manager folder" /f >nul',
              'reg add "HKCU\\Software\\Classes\\jobmanager" /v "URL Protocol" /d "" /f >nul',
              'reg add "HKCU\\Software\\Classes\\jobmanager\\shell\\open\\command" /ve /d '
              '"wscript.exe \\"%F%\\" \\"%%1\\"" /f >nul',
              "if errorlevel 1 (echo Setup failed. & pause & exit /b 1)",
              "echo.",
              "echo Done. Go back to Job Manager and click Open folder.",
              "echo The browser will ask once whether to open Job Manager links. Tick Always allow.",
              "echo.",
              "echo To remove it later: reg delete HKCU\\Software\\Classes\\jobmanager /f",
              "pause"]
    body = "\r\n".join(lines) + "\r\n"
    return Response(body, mimetype="application/octet-stream",
                    headers={"Content-Disposition": "attachment; filename=JobManager-OpenFolder-setup.cmd"})


@app.route("/api/jobs/<int:job_id>/open-folder", methods=["POST"])
def api_open_folder(job_id):
    """Opens Explorer only when the browser is on the host PC itself. Everyone
    else gets the path copied instead (the old per-PC agent is gone)."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return err("Open folder only works on the host PC", 403)
    conn = get_db()
    job = _load_job(conn, job_id); conn.close()
    folder = job and job["folder_path"]
    if not folder or not os.path.isdir(folder):
        return err("Folder not found")
    cmd = {"Windows": ["explorer", folder], "Darwin": ["open", folder]}.get(SYSTEM, ["xdg-open", folder])
    subprocess.Popen(cmd)
    return jsonify({"success": True})


@app.route("/api/upc/<upc>")
def api_upc(upc):
    v = validate_upc(upc)
    if not v["valid"]:
        return jsonify(v)
    h = SS.history(upc)
    rows = h["rows"]
    last = max(rows, key=lambda r: r["end"]) if rows else None
    conn = get_db()
    prev = conn.execute("""SELECT customer, label_size, inlay_type, lpr, title, part_number
        FROM jobs WHERE upc=? ORDER BY id DESC LIMIT 1""", (upc,)).fetchone()
    conn.close()
    return jsonify({"valid": True, "next_start": h["next_start"], "is_new": not rows,
                    "default_start": SS.DEFAULT_START, "allocations": h["jobs"],
                    "registry_ok": h["registry_ok"],
                    "last_alloc": ({"job_number": last["job_number"], "end": last["end"],
                                    "date": (last["when"] or "")[:10], "note": last["note"]} if last else None),
                    "last_job": dict(prev) if prev else None})


# ── Priority list ─────────────────────────────────────────────────────────────
def _bump_priority(conn):
    conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('priority_version', ?)", (now_str(),))


def _renumber_priority(conn):
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM jobs WHERE priority_rank IS NOT NULL AND archived_at IS NULL ORDER BY priority_rank, id")]
    conn.execute("UPDATE jobs SET priority_rank=NULL WHERE archived_at IS NOT NULL")
    for i, jid in enumerate(ids, start=1):
        conn.execute("UPDATE jobs SET priority_rank=? WHERE id=?", (i, jid))


@app.route("/api/priority")
def api_priority():
    conn = get_db()
    rows = [dict(r) for r in conn.execute(f"""SELECT j.id, j.job_number, j.job_ticket, j.customer, j.title,
        j.status, j.due_date, j.priority_rank, {ROLL_SUMMARY} FROM jobs j
        WHERE j.priority_rank IS NOT NULL AND j.archived_at IS NULL ORDER BY j.priority_rank""")]
    conn.close()
    return jsonify({"jobs": rows})


@app.route("/api/priority", methods=["POST"])
def api_priority_change():
    """{action: add | remove | order, job_id, ids: [...]}"""
    data = request.get_json(silent=True) or {}
    action, jid = data.get("action"), data.get("job_id")
    conn = get_db()
    try:
        if action in ("add", "remove"):
            job = _load_job(conn, int(jid or 0))
            if not job:
                return err("Job not found", 404)
            if action == "add":
                if job["archived_at"]:
                    return err("Archived jobs can't be prioritized")
                if job["priority_rank"] is None:
                    top = conn.execute("SELECT COALESCE(MAX(priority_rank), 0) FROM jobs WHERE archived_at IS NULL").fetchone()[0]
                    pos = data.get("position")
                    if pos == "top":
                        conn.execute("UPDATE jobs SET priority_rank=priority_rank+1 WHERE priority_rank IS NOT NULL")
                        conn.execute("UPDATE jobs SET priority_rank=1 WHERE id=?", (job["id"],))
                    else:
                        conn.execute("UPDATE jobs SET priority_rank=? WHERE id=?", (top + 1, job["id"]))
                    _renumber_priority(conn)
                    rank = conn.execute("SELECT priority_rank FROM jobs WHERE id=?", (job["id"],)).fetchone()[0]
                    log_activity(conn, "priority", job["id"], f"{job_label(job)} added to priority list at #{rank}")
            else:
                conn.execute("UPDATE jobs SET priority_rank=NULL WHERE id=?", (job["id"],))
                _renumber_priority(conn)
                log_activity(conn, "priority", job["id"], f"{job_label(job)} removed from priority list")
        elif action == "order":
            ids = [int(i) for i in data.get("ids") or []]
            current = {r[0] for r in conn.execute("SELECT id FROM jobs WHERE priority_rank IS NOT NULL AND archived_at IS NULL")}
            ids = [i for i in ids if i in current] + sorted(current - set(ids))
            for rank, i in enumerate(ids, start=1):
                conn.execute("UPDATE jobs SET priority_rank=? WHERE id=?", (rank, i))
            moved = data.get("moved_id")
            if moved and int(moved) in current:
                job = _load_job(conn, int(moved))
                log_activity(conn, "priority", job["id"], f"{job_label(job)} moved to #{ids.index(int(moved)) + 1} in priority")
        else:
            return err("Unknown action")
        _bump_priority(conn)
        conn.commit()
    finally:
        conn.close()
    return api_priority()


# ══════════════════════════════════════════════════════════════════════════════
# BarTender templates
# ══════════════════════════════════════════════════════════════════════════════
def _find_in_master(file_name):
    """Look for a template by file name in the Master Templates folder."""
    root = get_settings()["btw_template_dir"]
    if not os.path.isdir(root):
        return None
    direct = os.path.join(root, file_name)
    if os.path.isfile(direct):
        return direct
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.lower() == file_name.lower():
                return os.path.join(dirpath, f)
    return None


def copy_template(tid, job_dir, job_number):
    """Copy a Master Template into the job's Print folder as '<JOB-####> - <name>.btw'.
    If the indexed path is stale, the file is looked up again in Master Templates.
    The copy is checked against the original before it counts."""
    conn = get_db()
    row = conn.execute("SELECT file_path FROM btw_templates WHERE id=?", (tid,)).fetchone()
    conn.close()
    if not row:
        return {"error": "Template not found"}
    src = row["file_path"]
    if not os.path.isfile(src):
        name = re.split(r"[\\/]", src)[-1]
        found = _find_in_master(name)
        if not found:
            return {"error": f"Template not found in Master Templates: {name}"}
        conn = get_db()
        conn.execute("UPDATE btw_templates SET file_path=?, active=1, updated_at=? WHERE id=?", (found, now_str(), tid))
        conn.commit(); conn.close()
        src = found
    dest = os.path.join(job_dir, "Print", f"{job_number} - {os.path.basename(src)}")
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(src, dest)
        if os.path.getsize(dest) != os.path.getsize(src):
            os.remove(dest)
            return {"error": "The copied template didn't match the original; nothing was kept. Try again."}
    except OSError as e:
        return {"error": str(e)}
    return {"dest_path": dest, "source": src}


def _thumb_for(tid):
    try:
        for f in os.listdir(THUMBNAIL_DIR):
            if os.path.splitext(f)[0] == str(tid):
                return os.path.join(THUMBNAIL_DIR, f)
    except OSError:
        pass
    return None


# ── Template matching ─────────────────────────────────────────────────────────
# Master Templates are named "<Customer> - <UPC>.btw" (most of them) or
# "<Customer> - <size / description>.btw". The UPC decides; customer, size, part #
# and what was used last time for this UPC break ties and cover the rest.
_GENERIC = {"label", "labels", "products", "product", "master", "file", "inc", "llc", "co",
            "corp", "company", "the", "group", "solutions", "systems", "system", "tech", "and"}


def _norm(t):
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())


def _tokens(t):
    return {w for w in re.findall(r"[a-z0-9]+", (t or "").lower()) if len(w) >= 3 and w not in _GENERIC}


def _sizes(text):
    """All 'W x H' sizes in a string, as sorted float pairs."""
    out = []
    for a, b in re.findall(r"(\d*\.?\d+)\s*[xX×]\s*(\d*\.?\d+)", text or ""):
        try:
            out.append(tuple(sorted((float(a), float(b)))))
        except ValueError:
            pass
    return out


def _upc_forms(upc):
    """12-digit UPC plus the forms it shows up as in file names."""
    return {"exact": [upc], "variant": list({upc.lstrip("0"), "0" + upc, "00" + upc} - {upc, ""})}


def match_templates(upc="", customer="", label_size="", part_number="", limit=8):
    upc, part_number = (upc or "").strip(), (part_number or "").strip()
    conn = get_db()
    rows = [dict(r) for r in conn.execute("SELECT id, name, file_path FROM btw_templates WHERE active=1")]
    used_upc, used_cust = {}, {}
    if upc or customer:
        for r in conn.execute("""SELECT upc, customer, template_path FROM jobs
                                 WHERE template_path IS NOT NULL ORDER BY id DESC"""):
            base = re.split(r"[\\/]", r["template_path"])[-1]
            base = base.split(" - ", 1)[1] if base.startswith("JOB-") and " - " in base else base
            if upc and r["upc"] == upc:
                used_upc[base.lower()] = used_upc.get(base.lower(), 0) + 1
            if customer and _norm(r["customer"]) == _norm(customer):
                used_cust[base.lower()] = used_cust.get(base.lower(), 0) + 1
    conn.close()

    forms = _upc_forms(upc) if re.fullmatch(r"\d{12}", upc) else {"exact": [], "variant": []}
    c_norm, c_tok = _norm(customer), _tokens(customer)
    sizes = _sizes(label_size)
    out = []
    for t in rows:
        fname = re.split(r"[\\/]", t["file_path"])[-1]
        stem = fname[:-4] if fname.lower().endswith(".btw") else fname
        t_cust = stem.split(" - ", 1)[0]
        digits = set(re.findall(r"\d{11,14}", stem))
        score, why = 0, []
        if any(f in digits for f in forms["exact"]):
            score += 200; why.append("UPC")
        elif any(f in digits for f in forms["variant"]):
            score += 180; why.append("UPC (other format)")
        elif digits and forms["exact"]:
            score -= 40                 # a template for a different UPC
        if used_upc.get(fname.lower()):
            score += 150; why.append("Used before for this UPC")
        tc = _norm(t_cust)
        if c_norm and tc and (tc == c_norm or tc in c_norm or c_norm in tc):
            score += 40; why.append("Customer")
        elif c_tok and c_tok & _tokens(t_cust + " " + t["name"]):
            score += 20; why.append("Customer (partial)")
        if sizes and any(abs(a[0] - b[0]) <= 0.03 and abs(a[1] - b[1]) <= 0.03
                         for a in sizes for b in _sizes(stem)):
            score += 30; why.append("Label size")
        if len(part_number) >= 4 and _norm(part_number) in _norm(stem):
            score += 60; why.append("Part #")
        if "customer" in " ".join(why).lower():
            if used_cust.get(fname.lower()):
                score += 15; why.append("Used before for this customer")
            if "master file" in stem.lower():
                score += 5
        if score >= 20 and why:
            out.append({"id": t["id"], "name": t["name"], "file_path": t["file_path"],
                        "file_name": fname, "score": score, "reasons": why})
    out.sort(key=lambda r: (-r["score"], r["file_name"].lower()))
    seen, uniq = {}, []
    for r in out:                       # same file listed twice (old and new path): keep one
        k = r["file_name"].lower()
        if k in seen:
            if not os.path.isfile(seen[k]["file_path"]) and os.path.isfile(r["file_path"]):
                seen[k].update(id=r["id"], file_path=r["file_path"], name=r["name"])
            continue
        seen[k] = r; uniq.append(r)
    out = uniq[:limit]

    thumbs = set()
    try:
        thumbs = {os.path.splitext(f)[0] for f in os.listdir(THUMBNAIL_DIR)}
    except OSError:
        pass
    for r in out:
        r["has_thumb"] = str(r["id"]) in thumbs

    auto, confidence, ambiguous = None, None, False
    if out:
        top = out[0]
        tied = len(out) > 1 and out[1]["score"] == top["score"]
        if top["score"] >= 150:
            confidence = "upc" if top["reasons"][0].startswith("UPC") else "history"
            ambiguous = tied
            auto = None if tied else top["id"]
        elif top["score"] >= 70 and not tied:
            confidence, auto = "guess", top["id"]
    return {"candidates": out, "auto_id": auto, "confidence": confidence, "ambiguous": ambiguous}


_last_sync = {"t": 0.0}


def match_templates_fresh(upc="", customer="", label_size="", part_number=""):
    """match_templates, but if the UPC isn't in the index yet, rescan Master Templates
    (at most once a minute) so a template saved there minutes ago is still found."""
    m = match_templates(upc, customer, label_size, part_number)
    upc_hit = any(r.startswith("UPC") for c in m["candidates"] for r in c["reasons"])
    if re.fullmatch(r"\d{12}", upc or "") and not upc_hit and time.time() - _last_sync["t"] > 60:
        _last_sync["t"] = time.time()
        try:
            r = sync_templates()
            if r.get("added") or r.get("repointed"):
                m = match_templates(upc, customer, label_size, part_number)
        except Exception:
            pass
    return m


@app.route("/api/templates/match")
def api_templates_match():
    a = request.args
    return jsonify(match_templates_fresh(a.get("upc", ""), a.get("customer", ""),
                                         a.get("label_size", ""), a.get("part_number", "")))


# ── Template library: browse, view and change the Master Templates files ───────
# Nothing here deletes a template for good. A replaced file is kept in "_Previous versions"
# and a removed one is moved to "_Removed", both inside the Master Templates folder.
TPL_RESERVED = {"_Removed", "_Previous versions"}
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
TPL_MAX_BYTES = 200 * 1024 * 1024


def _tpl_root():
    return os.path.normpath(get_settings()["btw_template_dir"])


def _same(p):
    return os.path.normcase(os.path.realpath(p))


def _inside(root, p):
    r, q = _same(root), _same(p)
    return q == r or q.startswith(r + os.sep)


def _tpl_rel_dir(root, file_path):
    """Folder of a template relative to Master Templates, with '/' separators ('' = top level)."""
    try:
        rel = os.path.relpath(os.path.dirname(file_path), root)
    except ValueError:
        return ""
    return "" if rel in (".", "") or rel.startswith("..") else rel.replace("\\", "/")


def _tpl_stat_update(conn, path):
    try:
        st = os.stat(path)
    except OSError:
        return
    conn.execute("UPDATE btw_templates SET file_size=?, file_mtime=? WHERE file_path=?",
                 (st.st_size, datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"), path))


def _clean_name(name):
    """A template file name without '.btw'. Returns (stem, error)."""
    stem = re.sub(r"\.btw$", "", (name or "").strip(), flags=re.I).strip()
    if not stem:
        return None, "Type a name"
    if _BAD_CHARS.search(stem) or stem.endswith("."):
        return None, 'A name can\'t contain  < > : " / \\ | ? *  or end with a dot'
    if len(stem) > 150:
        return None, "That name is too long"
    return stem, None


def _clean_folder(rel):
    """Validated folder parts for a path under Master Templates. Returns (parts, error)."""
    parts = [p.strip() for p in re.split(r"[\\/]+", rel or "") if p.strip()]
    for p in parts:
        if p in (".", "..") or _BAD_CHARS.search(p) or p.endswith(".") or p in TPL_RESERVED:
            return None, f'"{p}" can\'t be used as a folder name'
    if len(parts) > 4:
        return None, "Folders can go four levels deep at most"
    return parts, None


def _unique_path(path):
    base, ext = os.path.splitext(path)
    n, cand = 2, path
    while os.path.exists(cand):
        cand = f"{base} ({n}){ext}"; n += 1
    return cand


def _tpl_get(tid):
    conn = get_db()
    row = conn.execute("SELECT * FROM btw_templates WHERE id=? AND active=1", (tid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _tpl_used_counts(conn):
    """How many jobs got a copy of each template, keyed by the template's lower-case file name.
    A job's copy is '<JOB-####> - <template name>.btw'."""
    used = {}
    for r in conn.execute("SELECT template_path FROM jobs WHERE template_path IS NOT NULL AND template_path != ''"):
        base = re.split(r"[\\/]", r["template_path"])[-1]
        m = re.match(r"^JOB-\d+ - (.+)$", base, re.I)
        key = (m.group(1) if m else base).lower()
        used[key] = used.get(key, 0) + 1
    return used


def _tpl_public(r, root, thumbs, used):
    base = os.path.basename(r["file_path"])
    return {"id": r["id"], "name": r["name"], "file_name": base, "file_path": r["file_path"],
            "folder": _tpl_rel_dir(root, r["file_path"]), "size": r.get("file_size"),
            "modified": r.get("file_mtime"), "has_thumb": str(r["id"]) in thumbs,
            "used": used.get(base.lower(), 0)}


def _thumb_ids():
    try:
        return {os.path.splitext(f)[0] for f in os.listdir(THUMBNAIL_DIR)}
    except OSError:
        return set()


@app.route("/api/templates")
def api_templates():
    """List templates. With no `per` it returns the first 40 matches (the job-picker's shape);
    with `per` it pages. `folder` limits to a folder and its subfolders ('.' = top level only)."""
    a = request.args
    q = a.get("q", "").strip()
    folder = a.get("folder")
    sort = a.get("sort") if a.get("sort") in ("name", "modified", "size", "used") else "name"
    desc = a.get("dir") == "desc"
    sql, params = "SELECT * FROM btw_templates WHERE active=1", []
    for w in re.split(r"[\s\-_]+", q):
        if w:
            sql += " AND (name || ' ' || file_path) LIKE ?"; params.append(f"%{w}%")
    root = _tpl_root()
    conn = get_db()
    rows = [dict(r) for r in conn.execute(sql, params)]
    total = conn.execute("SELECT COUNT(*) FROM btw_templates WHERE active=1").fetchone()[0]
    used = _tpl_used_counts(conn)
    conn.close()
    thumbs = _thumb_ids()
    items = [_tpl_public(r, root, thumbs, used) for r in rows]
    if folder is not None:
        if folder == ".":
            items = [t for t in items if t["folder"] == ""]
        else:
            f = folder.strip("/")
            items = [t for t in items if t["folder"] == f or t["folder"].startswith(f + "/")]
    keys = {"name": lambda t: t["name"].lower(), "modified": lambda t: t["modified"] or "",
            "size": lambda t: t["size"] or 0, "used": lambda t: t["used"]}
    items.sort(key=keys["name"])                          # ties fall back to name order
    if sort != "name" or desc:
        items.sort(key=keys[sort], reverse=desc)
    matched = len(items)
    try:
        per = max(1, min(200, int(a.get("per") or 0))) if a.get("per") else 40
        page = max(1, int(a.get("page") or 1))
    except ValueError:
        per, page = 40, 1
    pages = max(1, -(-matched // per))
    page = min(page, pages)
    items = items[(page - 1) * per: page * per]
    for t in items:
        t["missing"] = not os.path.isfile(t["file_path"])
    return jsonify({"templates": items, "total": total, "matched": matched, "page": page, "pages": pages, "per": per})


@app.route("/api/templates/folders")
def api_template_folders():
    root = _tpl_root()
    conn = get_db()
    paths = [r["file_path"] for r in conn.execute("SELECT file_path FROM btw_templates WHERE active=1")]
    conn.close()
    direct = {}
    for p in paths:
        d = _tpl_rel_dir(root, p)
        direct[d] = direct.get(d, 0) + 1
    dirs = {d for d in direct if d}

    def scan(base, rel, depth):
        if depth > 4:
            return
        try:
            with os.scandir(base) as it:
                for e in it:
                    if e.is_dir() and e.name not in TPL_RESERVED:
                        r = f"{rel}/{e.name}" if rel else e.name
                        dirs.add(r); scan(e.path, r, depth + 1)
        except OSError:
            pass
    root_ok = os.path.isdir(root)
    if root_ok:
        scan(root, "", 1)
    for d in list(dirs):                                  # every ancestor of a folder is a folder too
        parts = d.split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))
    out = []
    for d in sorted(dirs, key=str.lower):
        n = sum(c for k, c in direct.items() if k == d or k.startswith(d + "/"))
        out.append({"path": d, "name": d.split("/")[-1], "depth": d.count("/"), "count": n})
    return jsonify({"root": root, "root_ok": root_ok, "total": len(paths), "top": direct.get("", 0), "folders": out})


@app.route("/api/templates/<int:tid>")
def api_template_detail(tid):
    row = _tpl_get(tid)
    if not row:
        return err("Template not found", 404)
    root = _tpl_root()
    conn = get_db()
    used = _tpl_used_counts(conn)
    base = os.path.basename(row["file_path"])
    jobs = [dict(r) for r in conn.execute(
        """SELECT id, job_number, job_ticket, customer, status FROM jobs
           WHERE template_path IS NOT NULL AND lower(template_path) LIKE ? ORDER BY id DESC LIMIT 8""",
        (f"%- {base.lower()}",))]
    conn.close()
    t = _tpl_public(row, root, _thumb_ids(), used)
    t["missing"] = not os.path.isfile(row["file_path"])
    t["jobs"] = jobs
    t["versions"] = _tpl_versions(root, os.path.splitext(base)[0])
    return jsonify(t)


def _tpl_versions(root, stem):
    d = os.path.join(root, "_Previous versions")
    out = []
    try:
        for f in os.listdir(d):
            if f.startswith(stem + " (replaced ") and f.lower().endswith(".btw"):
                st = os.stat(os.path.join(d, f))
                out.append({"file": f, "size": st.st_size,
                            "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
    except OSError:
        pass
    return sorted(out, key=lambda v: v["file"], reverse=True)


def _tpl_log(conn, msg):
    log_activity(conn, "template", None, msg)


def _tpl_register(conn, path):
    ts = now_str()
    cur = conn.execute("INSERT INTO btw_templates (name, file_path, active, created_at, updated_at) VALUES (?,?,1,?,?)",
                       (os.path.splitext(os.path.basename(path))[0], path, ts, ts))
    _tpl_stat_update(conn, path)
    return cur.lastrowid


def _tpl_save_upload(f, dest):
    """Write an uploaded .btw to dest through a temp file, so a failed upload never leaves half a template."""
    data = f.read()
    if not data:
        return "That file is empty"
    if len(data) > TPL_MAX_BYTES:
        return "That file is larger than 200 MB"
    tmp = dest + ".uploading"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
    except OSError as e:
        try: os.remove(tmp)
        except OSError: pass
        return str(e)
    return None


@app.route("/api/templates/<int:tid>/rename", methods=["POST"])
def api_template_rename(tid):
    row = _tpl_get(tid)
    if not row:
        return err("Template not found", 404)
    stem, bad = _clean_name((request.get_json(silent=True) or {}).get("name"))
    if bad:
        return err(bad)
    old = row["file_path"]
    new = os.path.join(os.path.dirname(old), stem + ".btw")
    if _same(new) == _same(old):
        return jsonify({"success": True})
    if os.path.exists(new):
        return err(f'A template called "{stem}" is already in that folder')
    if not os.path.isfile(old):
        return err("The file isn't on disk any more. Rescan Master Templates.")
    try:
        os.rename(old, new)
        root = _tpl_root()
        old_stem = os.path.splitext(os.path.basename(old))[0]
        for v in _tpl_versions(root, old_stem):           # keep the version history with the template
            src = os.path.join(root, "_Previous versions", v["file"])
            os.rename(src, os.path.join(root, "_Previous versions", stem + v["file"][len(old_stem):]))
    except OSError as e:
        return err(str(e))
    conn = get_db()
    conn.execute("UPDATE btw_templates SET name=?, file_path=?, updated_at=? WHERE id=?", (stem, new, now_str(), tid))
    _tpl_log(conn, f'Template renamed: "{os.path.basename(old)}" to "{stem}.btw"')
    conn.commit(); conn.close()
    return jsonify({"success": True})


@app.route("/api/templates/<int:tid>/move", methods=["POST"])
def api_template_move(tid):
    row = _tpl_get(tid)
    if not row:
        return err("Template not found", 404)
    root = _tpl_root()
    parts, bad = _clean_folder((request.get_json(silent=True) or {}).get("folder"))
    if bad:
        return err(bad)
    dest_dir = os.path.join(root, *parts)
    old = row["file_path"]
    new = os.path.join(dest_dir, os.path.basename(old))
    if not _inside(root, new):
        return err("That folder is outside Master Templates")
    if _same(new) == _same(old):
        return jsonify({"success": True})
    if os.path.exists(new):
        return err("A template with that name is already in that folder")
    if not os.path.isfile(old):
        return err("The file isn't on disk any more. Rescan Master Templates.")
    try:
        os.makedirs(dest_dir, exist_ok=True)
        shutil.move(old, new)
    except OSError as e:
        return err(str(e))
    conn = get_db()
    conn.execute("UPDATE btw_templates SET file_path=?, updated_at=? WHERE id=?", (new, now_str(), tid))
    _tpl_log(conn, f'Template "{os.path.basename(old)}" moved to {"/".join(parts) or "the top level"}')
    conn.commit(); conn.close()
    return jsonify({"success": True})


@app.route("/api/templates/<int:tid>/duplicate", methods=["POST"])
def api_template_duplicate(tid):
    row = _tpl_get(tid)
    if not row or not os.path.isfile(row["file_path"]):
        return err("Template file not found", 404)
    stem, bad = _clean_name((request.get_json(silent=True) or {}).get("name") or
                            os.path.splitext(os.path.basename(row["file_path"]))[0] + " copy")
    if bad:
        return err(bad)
    new = os.path.join(os.path.dirname(row["file_path"]), stem + ".btw")
    if os.path.exists(new):
        return err(f'A template called "{stem}" is already in that folder')
    try:
        shutil.copy2(row["file_path"], new)
    except OSError as e:
        return err(str(e))
    conn = get_db()
    nid = _tpl_register(conn, new)
    _tpl_log(conn, f'Template "{os.path.basename(row["file_path"])}" duplicated as "{stem}.btw"')
    conn.commit(); conn.close()
    return jsonify({"success": True, "id": nid})


@app.route("/api/templates/<int:tid>/replace", methods=["POST"])
def api_template_replace(tid):
    row = _tpl_get(tid)
    if not row:
        return err("Template not found", 404)
    f = request.files.get("file")
    if not f or not f.filename or not f.filename.lower().endswith(".btw"):
        return err("Choose a BarTender .btw file")
    path, root = row["file_path"], _tpl_root()
    if os.path.isfile(path):                              # keep the file being replaced
        vdir = os.path.join(root, "_Previous versions")
        stem = os.path.splitext(os.path.basename(path))[0]
        try:
            os.makedirs(vdir, exist_ok=True)
            shutil.copy2(path, _unique_path(os.path.join(
                vdir, f"{stem} (replaced {datetime.now().strftime('%Y-%m-%d %H%M%S')}).btw")))
        except OSError as e:
            return err(f"Couldn't keep the old version, so nothing was changed: {e}")
    bad = _tpl_save_upload(f, path)
    if bad:
        return err(bad)
    conn = get_db()
    conn.execute("UPDATE btw_templates SET updated_at=? WHERE id=?", (now_str(), tid))
    _tpl_stat_update(conn, path)
    _tpl_log(conn, f'Template "{os.path.basename(path)}" replaced with a new file (the old one is kept in _Previous versions)')
    conn.commit(); conn.close()
    return jsonify({"success": True})


@app.route("/api/templates/<int:tid>/remove", methods=["POST"])
def api_template_remove(tid):
    """Take a template out of the library by moving the file to _Removed. It can be moved back by hand."""
    row = _tpl_get(tid)
    if not row:
        return err("Template not found", 404)
    root, path = _tpl_root(), row["file_path"]
    if os.path.isfile(path):
        try:
            os.makedirs(os.path.join(root, "_Removed"), exist_ok=True)
            shutil.move(path, _unique_path(os.path.join(root, "_Removed", os.path.basename(path))))
        except OSError as e:
            return err(str(e))
    conn = get_db()
    conn.execute("UPDATE btw_templates SET active=0, updated_at=? WHERE id=?", (now_str(), tid))
    _tpl_log(conn, f'Template "{os.path.basename(path)}" removed (the file is in _Removed)')
    conn.commit(); conn.close()
    return jsonify({"success": True})


@app.route("/api/templates/<int:tid>/download")
def api_template_download(tid):
    row = _tpl_get(tid)
    if not row or not os.path.isfile(row["file_path"]):
        return err("Template file not found", 404)
    return send_file(row["file_path"], as_attachment=True, download_name=os.path.basename(row["file_path"]))


@app.route("/api/templates/<int:tid>/versions/<path:fname>")
def api_template_version_download(tid, fname):
    row = _tpl_get(tid)
    root = _tpl_root()
    vdir = os.path.join(root, "_Previous versions")
    p = os.path.join(vdir, os.path.basename(fname))
    stem = os.path.splitext(os.path.basename(row["file_path"]))[0] if row else None
    if not row or not os.path.basename(fname).startswith(stem + " (replaced ") or not os.path.isfile(p):
        return err("Version not found", 404)
    return send_file(p, as_attachment=True, download_name=os.path.basename(p))


@app.route("/api/templates/folders", methods=["POST"])
def api_template_folder_create():
    d = request.get_json(silent=True) or {}
    parts, bad = _clean_folder(f"{d.get('parent') or ''}/{d.get('name') or ''}")
    if bad or not parts:
        return err(bad or "Type a folder name")
    root = _tpl_root()
    if not os.path.isdir(root):
        return err("The Master Templates folder isn't reachable")
    path = os.path.join(root, *parts)
    if os.path.exists(path):
        return err("That folder already exists")
    try:
        os.makedirs(path)
    except OSError as e:
        return err(str(e))
    return jsonify({"success": True, "path": "/".join(parts)})


@app.route("/api/templates/upload", methods=["POST"])
def api_template_upload():
    f = request.files.get("file")
    if not f or not f.filename or not f.filename.lower().endswith(".btw"):
        return err("Choose a BarTender .btw file")
    root = _tpl_root()
    parts, bad = _clean_folder(request.form.get("folder"))
    if bad:
        return err(bad)
    stem, bad = _clean_name(os.path.basename(f.filename))
    if bad:
        return err(bad)
    dest_dir = os.path.join(root, *parts)
    dest = os.path.join(dest_dir, stem + ".btw")
    if os.path.exists(dest):
        return err(f'"{stem}.btw" is already in that folder. Use Replace file on it instead.')
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as e:
        return err(str(e))
    bad = _tpl_save_upload(f, dest)
    if bad:
        return err(bad)
    conn = get_db()
    nid = _tpl_register(conn, dest)
    _tpl_log(conn, f'Template "{stem}.btw" added to {"/".join(parts) or "the top level"}')
    conn.commit(); conn.close()
    return jsonify({"success": True, "id": nid})


def sync_templates() -> dict:
    """Register every .btw under the Master Templates folder that isn't known yet,
    and hide entries whose file is gone. Runs at startup, every 6 hours, and when a UPC
    isn't found.

    If the folder is reached by a different path than the one a template was indexed
    with (Z:\\ versus \\\\server\\share), an entry whose file name appears exactly once
    among the missing entries and once among the new files is repointed, not duplicated,
    so its preview image and history stay with it."""
    root = get_settings()["btw_template_dir"]
    if not os.path.isdir(root):
        return {"success": False, "error": f"Master Templates folder not reachable: {root}"}
    found = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in TPL_RESERVED]      # our own backup folders aren't templates
        found += [os.path.join(dirpath, f) for f in files
                  if f.lower().endswith(".btw") and not f.startswith("~")]
    leaf = lambda p: re.split(r"[\\/]", p)[-1].lower()
    conn = get_db()
    known = {r["file_path"]: (r["id"], r["active"]) for r in
             conn.execute("SELECT id, file_path, active FROM btw_templates")}
    ts, added, repointed = now_str(), 0, 0
    found_set = set(found)
    new_paths = [p for p in found if p not in known]
    missing = {p: tid for p, (tid, act) in known.items() if act and p not in found_set}

    by_old, by_new = {}, {}
    for p in missing: by_old.setdefault(leaf(p), []).append(p)
    for p in new_paths: by_new.setdefault(leaf(p), []).append(p)
    for name, olds in by_old.items():
        news = by_new.get(name, [])
        if len(olds) == 1 and len(news) == 1:
            conn.execute("UPDATE btw_templates SET file_path=?, active=1, updated_at=? WHERE id=?",
                         (news[0], ts, missing[olds[0]]))
            repointed += 1
            new_paths.remove(news[0]); del missing[olds[0]]

    for path in new_paths:
        conn.execute("INSERT INTO btw_templates (name, file_path, active, created_at, updated_at)"
                     " VALUES (?, ?, 1, ?, ?)", (os.path.splitext(os.path.basename(path))[0], path, ts, ts))
        added += 1
    for path in found:                                   # bring back entries that reappeared
        if path in known and not known[path][1]:
            conn.execute("UPDATE btw_templates SET active=1, updated_at=? WHERE id=?", (ts, known[path][0]))
            added += 1

    active_n = sum(1 for _, a in known.values() if a)
    # If most known templates still look "missing", the folder is probably the wrong one
    # or temporarily unreachable. Hide nothing in that case.
    skipped = bool(missing) and len(missing) > active_n / 2
    if not skipped:
        for tid in missing.values():
            conn.execute("UPDATE btw_templates SET active=0, updated_at=? WHERE id=?", (ts, tid))
    for path in found:                                   # size and date for sorting and the detail panel
        _tpl_stat_update(conn, path)
    conn.commit(); conn.close()
    return {"success": True, "found": len(found), "added": added, "repointed": repointed,
            "hidden": 0 if skipped else len(missing),
            "warning": (f"{len(missing)} registered templates weren't found under {root}. Nothing was "
                        "hidden. Check that this is the folder the templates were registered from.")
            if skipped else None}


@app.route("/api/templates/sync", methods=["POST"])
def api_templates_sync():
    r = sync_templates()
    return jsonify(r) if r["success"] else err(r["error"])


def start_template_sync():
    def run():
        while True:
            try:
                r = sync_templates()
                print(f"  [TPL] {r['found']} templates, {r['added']} new" if r["success"] else f"  [TPL] {r['error']}")
            except Exception as e:
                print(f"  [TPL] sync failed: {e}")
            time.sleep(6 * 3600)
    threading.Thread(target=run, daemon=True, name="template-sync").start()


@app.route("/api/templates/<int:tid>/thumbnail")
def api_thumb_get(tid):
    p = _thumb_for(tid)
    return send_file(p, max_age=0) if p else abort(404)


@app.route("/api/templates/<int:tid>/thumbnail", methods=["POST"])
def api_thumb_put(tid):
    f = request.files.get("file")
    if not f or not f.filename:
        return err("Choose an image")
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        return err("Use a PNG, JPG, GIF or WEBP image")
    data = f.read()
    if len(data) > 5 * 1024 * 1024:
        return err("Image is larger than 5 MB")
    old = _thumb_for(tid)
    if old:
        os.remove(old)
    os.makedirs(THUMBNAIL_DIR, exist_ok=True)
    path = os.path.join(THUMBNAIL_DIR, f"{tid}{ext}")
    with open(path, "wb") as fh:
        fh.write(data)
    conn = get_db()
    conn.execute("UPDATE btw_templates SET thumbnail_path=?, updated_at=? WHERE id=?", (path, now_str(), tid))
    conn.commit(); conn.close()
    return jsonify({"success": True})


# ══════════════════════════════════════════════════════════════════════════════
# Settings + backups
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    """Shared settings. Every value is checked first: one bad value saves nothing and says what is wrong.
    Changes are written to the activity log with who made them."""
    data = request.get_json(silent=True) or {}
    allowed = {"btw_template_dir", "default_overage_pct", "backup_dir",
               "backup_interval_hours", "backup_auto", "backup_keep", "rewind_base_url", "printers",
               "auto_archive_hours", "bt_enabled", "bt_trigger_dir", "bt_ack_dir", "bt_printer_map", "shift_starts"}
    clean = {}
    for k, v in data.items():
        if k not in allowed:
            continue
        if k in ("backup_auto", "bt_enabled"):
            v = "1" if v in (True, "1", 1, "true") else "0"
        elif k == "shift_starts":
            v = clean_shift_starts(v)
            if v is None:
                return err("Shift start times must be times like 06:00, 14:00, 22:00. Nothing was saved.")
        elif k == "bt_printer_map":
            v = "\n".join(f"{a} = {b}" for a, b in ((l.split("=", 1)[0].strip(), l.split("=", 1)[1].strip())
                                                     for l in str(v or "").splitlines() if "=" in l) if a and b)
        elif k in ("bt_trigger_dir", "bt_ack_dir"):
            v = str(v or "").strip().strip('"')
        elif k == "printers":
            v = clean_printers(v)
        elif k in NUMBER_SETTINGS:
            label, lo, hi, whole, _ = NUMBER_SETTINGS[k]
            n = _setting_number(k, v)
            if n is None:
                return err(f"{label} must be {'a whole number' if whole else 'a number'} from {lo:,} to {hi:,}. "
                           "Nothing was saved.")
            v = n
        clean[k] = str(v).strip()
    before = get_settings()
    conn = get_db()
    for k, v in clean.items():
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (k, v))
    after_raw = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM app_settings")}
    changed = [k for k, v in clean.items() if str(before.get(k, "")) != v
               and not (k in ("backup_auto", "bt_enabled") and before[k] == (v == "1"))]
    if changed:
        names = {"btw_template_dir": "Master Templates folder", "backup_dir": "backup folder", "backup_auto": "automatic backups",
                 "rewind_base_url": "rewind page address", "printers": "printer list", "bt_enabled": "sending to BarTender",
                 "bt_trigger_dir": "BarTender trigger folder", "bt_ack_dir": "BarTender acknowledgement folder",
                 "bt_printer_map": "BarTender printer names", "shift_starts": "shift start times",
                 **{k: v[0] for k, v in NUMBER_SETTINGS.items()}}
        def show(k, v):
            v = ("on" if v in (True, "1") else "off") if k in ("backup_auto", "bt_enabled") else str(v) if str(v).strip() else "(blank)"
            return v if len(v) <= 60 else v[:57] + "…"
        parts = [names[k] if k in ("printers", "bt_printer_map") else f"{names[k]} {show(k, before.get(k))} → {show(k, after_raw.get(k))}" for k in changed]
        log_activity(conn, "settings", None, "Settings changed: " + "; ".join(parts))
    conn.commit(); conn.close()
    SS.set_journal_dir(get_settings()["backup_dir"])
    start_backup_scheduler()
    threading.Thread(target=check_shares, daemon=True, name="share-check").start()   # folders may have changed
    return jsonify({"success": True, "settings": get_settings()})


_backup_lock = threading.Lock()
BACKUP_DEFAULT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")
BACKUP_STARTUP_DELAY = 30            # seconds after start before an overdue backup is taken
_STARTED = time.time()
_backup_state = {"last_ok_at": None, "last_file": None, "last_error": None, "last_error_at": None, "verify": None}


def backup_dir_effective(s=None):
    """The configured backup folder, or a 'backups' folder next to the app so that a backup
    always exists. A folder on this PC survives a bad edit or a corrupt file but not the loss
    of the PC, so the Data panel warns until a folder on the share is set."""
    s = s or get_settings()
    return s["backup_dir"] or BACKUP_DEFAULT_DIR


def _list_backups(bdir):
    try:
        return sorted(f for f in os.listdir(bdir) if f.startswith("backup_") and f.endswith(".zip"))
    except OSError:
        return []


verify_backup = backup.verify_backup        # kept under this name for the routes below
FAILED_KEEP = 10                           # failed zips kept for inspection


def _unique_name(bdir, label=""):
    base = f"backup_{datetime.now():%Y-%m-%d_%H-%M-%S}{'_' + label if label else ''}"
    name, n = f"{base}.zip", 2
    while os.path.exists(os.path.join(bdir, name)) or os.path.exists(os.path.join(bdir, "failed_" + name)):
        name, n = f"{base}_{n}.zip", n + 1
    return name


def _clean_parts(bdir):
    """Remove half-written zips left by a crash or a dropped share (older than an hour)."""
    try:
        for f in os.listdir(bdir):
            fp = os.path.join(bdir, f)
            if f.endswith(".part") and time.time() - os.path.getmtime(fp) > 3600:
                os.remove(fp)
    except OSError:
        pass


def make_backup(label=""):
    """Snapshot zip of every data source the app owns (see backup.py), written under a
    temporary name and checked before it gets its real name. A zip that fails the check is
    renamed failed_backup_*.zip: kept for inspection, never treated as a good backup, and
    it never causes older good backups to be removed."""
    s = get_settings()
    bdir = backup_dir_effective(s)
    with _backup_lock:
        part = None
        try:
            os.makedirs(bdir, exist_ok=True)
            _clean_parts(bdir)
            if SS.registry_ok(force=True)[0]:          # bring the serial records in step first
                _log_conflicts(SS.sync_pending()["conflicts"])
                SS.repair_all()
            name = _unique_name(bdir, label)
            part = os.path.join(bdir, f".{name}.part")
            backup.write_snapshot(part, db_path=DB_PATH, registry_dir=SERIAL_REGISTRY_DIR,
                                  thumbnail_dir=THUMBNAIL_DIR, config_ini=CONFIG_INI,
                                  journal_path=SS.journal_path(), job_folder_base=Z_DRIVE_BASE,
                                  app_version=APP_VERSION, serial_lock=SS._lock,
                                  extra_sources=proofing.backup_sources())
            check = backup.verify_backup(part)
            check["file"] = name
            if check["ok"]:
                os.replace(part, os.path.join(bdir, name)); part = None
                _backup_state.update(last_file=name, verify=check, last_ok_at=time.time(), last_error=None)
                for old in _list_backups(bdir)[:-max(s["backup_keep"], 5)]:
                    try: os.remove(os.path.join(bdir, old))
                    except OSError: pass
                db_upkeep()                         # good backup in hand: tidy the live database
                return {"success": True, "file": name, "verify": check,
                        "size_kb": round(os.path.getsize(os.path.join(bdir, name)) / 1024, 1)}
            failed = "failed_" + name
            check["file"] = failed
            os.replace(part, os.path.join(bdir, failed)); part = None
            for old in sorted(f for f in os.listdir(bdir) if f.startswith("failed_backup_"))[:-FAILED_KEEP]:
                try: os.remove(os.path.join(bdir, old))
                except OSError: pass
            msg = "The backup was written but failed its check: " + str(check.get("error") or "zip error")
            _backup_state.update(verify=check, last_error=msg, last_error_at=time.time())
            return {"success": False, "error": msg, "file": failed, "verify": check}
        except Exception as e:
            _backup_state.update(last_error=str(e), last_error_at=time.time())
            return {"success": False, "error": str(e)}
        finally:
            if part and os.path.exists(part):
                try: os.remove(part)
                except OSError: pass


_upkeep_state = {"last": None}


def db_upkeep() -> dict:
    """SQLite upkeep, run after every good backup and from Tools: PRAGMA optimize (refreshes the
    query planner's statistics) and a PASSIVE write-ahead-log checkpoint (copies finished changes
    into the main file without waiting on anyone; it never blocks the app). Never raises."""
    try:
        conn = get_db()
        try:
            conn.execute("PRAGMA optimize")
            busy, frames, done = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        finally:
            conn.close()
        r = {"at": now_str(), "optimized": True, "checkpoint": {"busy": busy, "frames": frames, "checkpointed": done}}
    except Exception as e:
        r = {"at": now_str(), "error": str(e)}
        log.warning("database upkeep failed: %s", e)
    _upkeep_state["last"] = r
    return r


def _dir_ok(d):
    """True if the folder exists, or can be created because its parent exists (a dropped share
    has neither)."""
    return os.path.isdir(d) or os.path.isdir(os.path.dirname(d.rstrip("\\/")) or ".")


def init_backup_state():
    """At startup, make sure the default backup folder exists and learn when the newest
    existing backup was made."""
    try:
        if not get_settings()["backup_dir"]:
            os.makedirs(BACKUP_DEFAULT_DIR, exist_ok=True)
    except OSError:
        pass
    try:
        zips = _list_backups(backup_dir_effective())
        if zips:
            p = os.path.join(backup_dir_effective(), zips[-1])
            _backup_state.update(last_file=zips[-1], last_ok_at=os.path.getmtime(p))
    except OSError:
        pass


def _backup_age_hours():
    t = _backup_state["last_ok_at"]
    return None if t is None else max(0.0, (time.time() - t) / 3600.0)


def _fmt_age(h):
    if h is None:
        return "never"
    if h < 1 / 12:
        return "just now"
    if h < 1:
        return f"{int(h * 60)} min ago"
    if h < 48:
        return f"{h:.0f} h ago"
    return f"{h / 24:.0f} days ago"


def backup_health(s=None):
    """{level: ok | warn | bad, text}. Cheap: uses only in-memory state and settings."""
    s = s or get_settings()
    st, age = _backup_state, _backup_age_hours()
    if st["last_error_at"] and (st["last_ok_at"] is None or st["last_error_at"] > st["last_ok_at"]):
        return {"level": "bad", "text": "Last backup failed"}
    if age is None:
        if s["backup_auto"] and time.time() - _STARTED < 180:
            return {"level": "warn", "text": "First backup is being made"}
        return {"level": "bad", "text": "No backup yet"}
    if age > s["backup_interval_hours"] * 2 + 1:
        return {"level": "bad", "text": f"Last backup {_fmt_age(age)}"}
    if not s["backup_auto"]:
        return {"level": "warn", "text": "Automatic backups are off"}
    if not s["backup_dir"]:
        return {"level": "warn", "text": "Backups are on this PC only"}
    return {"level": "ok", "text": f"Backed up {_fmt_age(age)}"}


def _fmt_size(n):
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{max(1, round(n / 1024))} KB"


def _file_info(path):
    try:
        st = os.stat(path)
        return {"exists": True, "size": st.st_size, "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")}
    except OSError:
        return {"exists": False}


@app.route("/api/backups")
def api_backups():
    s = get_settings(); bdir = backup_dir_effective(s)
    files = []
    reachable = _dir_ok(bdir)
    for f in reversed(_list_backups(bdir)):
        info = _file_info(os.path.join(bdir, f))
        files.append({"file": f, "size_kb": round(info.get("size", 0) / 1024, 1), "modified": info.get("modified")})
    return jsonify({"dir": bdir, "explicit": bool(s["backup_dir"]), "reachable": reachable, "backups": files[:30]})


@app.route("/api/backups", methods=["POST"])
def api_backup_now():
    r = make_backup("manual")
    return jsonify(r) if r["success"] else err(r["error"], 500)


@app.route("/api/backups/verify", methods=["POST"])
def api_backup_verify():
    """Check the newest backup the way a restore would open it."""
    bdir = backup_dir_effective()
    zips = _list_backups(bdir)
    if not zips:
        return err("There is no backup to check yet", 404)
    v = verify_backup(os.path.join(bdir, zips[-1]))
    _backup_state["verify"] = v
    return jsonify(v)


@app.route("/api/data/status")
def api_data_status():
    """Where each kind of data lives, whether it can be reached, and whether it is backed up."""
    s = get_settings(); bdir = backup_dir_effective(s)
    conn = get_db()
    n = lambda sql: conn.execute(sql).fetchone()[0]
    counts = {"jobs": n("SELECT COUNT(*) FROM jobs"), "rolls": n("SELECT COUNT(*) FROM job_rolls"),
              "notes": n("SELECT COUNT(*) FROM job_notes"), "activity": n("SELECT COUNT(*) FROM activity"),
              "serial_ranges": n("SELECT COUNT(*) FROM serial_allocations"),
              "templates": n("SELECT COUNT(*) FROM btw_templates WHERE active=1")}
    conn.close()
    h, st = SS.health(probe=False), share_state()          # from the background look: never waits on a share
    reg_files, z_ok, t_ok = st["registry_files"], bool(st["z_ok"]), bool(st["templates_ok"])
    try:
        previews = len(os.listdir(THUMBNAIL_DIR))
    except OSError:
        previews = 0
    db, jp = _file_info(DB_PATH), SS.journal_path()
    jinfo = st["journal_info"] if jp else {"exists": False}
    tdir = s["btw_template_dir"]
    ok_lv = lambda good: "ok" if good else "bad"
    stores = [
        {"key": "db", "name": "App database", "path": DB_PATH, "level": ok_lv(db["exists"]),
         "holds": "Jobs, rolls, notes, activity, the template index, settings, and a copy of every serial range",
         "status": (f"{_fmt_size(db['size'])}, changed {db['modified']}" if db["exists"] else "Not found"),
         "detail": f"{counts['jobs']:,} jobs, {counts['rolls']:,} rolls, {counts['notes']:,} notes, {counts['activity']:,} activity lines",
         "backup": "Yes, in every backup"},
        {"key": "registry", "name": "Serial registry", "path": SERIAL_REGISTRY_DIR, "level": ok_lv(h["registry_ok"]),
         "holds": "The primary record of every serial range, one text file per UPC",
         "status": (f"{reg_files:,} UPC files" if h["registry_ok"] else "Not reachable"),
         "detail": f"{counts['serial_ranges']:,} ranges also copied in the app database", "backup": "Yes, in every backup"},
        {"key": "journal", "name": "Serial journal", "path": jp or "Off: set a backup folder on the share",
         "level": "ok" if (jp and jinfo["exists"]) else ("warn" if not jp else "bad"),
         "holds": "An off-PC copy of every serial range, appended as it is made",
         "status": (f"{_fmt_size(jinfo['size'])}, changed {jinfo['modified']}" if jinfo["exists"] else ("Not created yet" if jp else "Off")),
         "detail": "", "backup": "It lives in the backup folder; it is not zipped"},
        {"key": "folders", "name": "Job folders", "path": Z_DRIVE_BASE, "level": "ok" if (z_ok and not Z_IS_DEFAULT) else ("warn" if z_ok else "bad"),
         "holds": "Encoding databases, checklists, QC sheets, template copies, proofs",
         "status": ("Shared" if not Z_IS_DEFAULT else "This PC only") if z_ok else "Not reachable",
         "detail": "", "backup": "No: back up the share itself"},
        {"key": "templates", "name": "Master Templates", "path": tdir, "level": ok_lv(t_ok),
         "holds": "The BarTender templates the app copies from",
         "status": (f"{counts['templates']:,} indexed" if t_ok else "Not reachable"),
         "detail": "", "backup": "No: back up the share itself"},
        {"key": "previews", "name": "Template previews", "path": THUMBNAIL_DIR, "level": "ok",
         "holds": "Preview images added on the Templates page", "status": f"{previews:,} images", "detail": "", "backup": "Yes, in every backup"},
        {"key": "config", "name": "config.ini", "path": CONFIG_INI, "level": ok_lv(os.path.isfile(CONFIG_INI)),
         "holds": "Paths and the port", "status": "Found" if os.path.isfile(CONFIG_INI) else "Not found", "detail": "", "backup": "Yes, in every backup"},
        proofing.data_store(),
    ]
    zips = _list_backups(bdir) if st["backup_ok"] else []
    newest = None
    if zips:
        info = _file_info(os.path.join(bdir, zips[-1]))
        newest = {"file": zips[-1], "modified": info.get("modified"), "size": _fmt_size(info.get("size", 0))}
    age = _backup_age_hours()
    health = backup_health(s)
    backups = {"dir": bdir, "explicit": bool(s["backup_dir"]), "reachable": bool(st["backup_ok"]), "auto": s["backup_auto"],
               "interval_hours": s["backup_interval_hours"], "keep": s["backup_keep"], "count": len(zips),
               "newest": newest, "age_text": _fmt_age(age), "verify": _backup_state["verify"],
               "last_error": _backup_state["last_error"]}
    warnings = []
    if health["level"] != "ok":
        txt = {"Backups are on this PC only": "Backups are being saved on this PC only. That protects against a bad edit or a corrupt "
               "file, not against losing the PC. Set a backup folder on the share below; it also switches on the serial journal.",
               "Last backup failed": f"The last backup failed: {_backup_state['last_error']}"}.get(health["text"], health["text"] + ".")
        warnings.append({"level": health["level"], "text": txt})
    if not backups["reachable"]:
        warnings.append({"level": "bad", "text": f"The backup folder can't be reached: {bdir}"})
    if not h["registry_ok"]:
        warnings.append({"level": "bad", "text": f"The serial registry can't be reached: {h['registry_message']}"})
    if _registry_gap_count["n"]:
        warnings.append({"level": "bad", "text": f"{_registry_gap_count['n']} UPC(s) have jobs past the end of their registry file: the old app's serial_registry folder was probably not copied."})
    if h["conflicts"]:
        warnings.append({"level": "bad", "text": f"{len(h['conflicts'])} serial conflict(s) need review on the Tools page."})
    if s["backup_dir"] and not h["journal_ok"]:
        warnings.append({"level": "warn", "text": "The serial journal folder can't be reached, so ranges are not being copied there."})
    if Z_IS_DEFAULT:
        warnings.append({"level": "warn", "text": "Job folders are being created on this PC only. Set z_drive_base in config.ini to the shared folder."})
    return jsonify({"stores": stores, "backups": backups, "warnings": warnings, "health": health, "counts": counts})


_sched = {"stop": threading.Event(), "thread": None}


def _backup_overdue(interval_hours):
    age = _backup_age_hours()
    return age is None or age >= interval_hours


def start_backup_scheduler():
    _sched["stop"].set()
    if _sched["thread"]:
        _sched["thread"].join(timeout=2)
    s = get_settings()
    if not (s["backup_auto"] and s["backup_interval_hours"] > 0):
        print("  [BAK] automatic backups are OFF")
        return
    stop = threading.Event(); _sched["stop"] = stop
    interval = s["backup_interval_hours"] * 3600

    def one():
        r = make_backup()
        print(f"  [BAK] {'saved ' + r['file'] if r['success'] else 'FAILED: ' + r['error']}")

    def run():
        if stop.wait(BACKUP_STARTUP_DELAY):
            return
        if _backup_overdue(s["backup_interval_hours"]):
            one()                      # first start, or the app was off for a while: back up now
        while not stop.wait(interval):
            one()
    _sched["thread"] = threading.Thread(target=run, daemon=True, name="backup")
    _sched["thread"].start()
    where = "" if s["backup_dir"] else "  (this PC only: set a folder on the share in Settings)"
    print(f"  [BAK] every {s['backup_interval_hours']}h → {backup_dir_effective(s)}{where}")


# ══════════════════════════════════════════════════════════════════════════════
# Serial records: startup checks, background sync, lookups
# ══════════════════════════════════════════════════════════════════════════════
_registry_gap_count = {"n": 0}


def serial_startup():
    SS.set_journal_dir(get_settings()["backup_dir"])
    ok, msg = SS.registry_ok(force=True)
    if not ok:
        print(f"  [SER] WARNING serial registry not reachable: {msg}")
        return
    gaps = SS.registry_gaps()
    if gaps:
        _registry_gap_count["n"] = len(gaps)
        print("  " + "!" * 62)
        print(f"  [SER] WARNING: {len(gaps)} UPC(s) have jobs that used serials past the end of their")
        print("        registry file. The serial_registry folder from the old app was probably not")
        print("        copied. Stop the app, copy it into this folder, and start again.")
        print("        (Until then the app still starts new jobs after the highest job in the")
        print("        database, so nothing overlaps a recorded job.)")
        print("  " + "!" * 62)
    added = SS.backfill_from_registry()
    fixed = SS.repair_all()
    synced = SS.sync_pending()
    print(f"  [SER] records in step: {added} lines copied to the app database, "
          f"{fixed} restored to the registry, {synced['synced']} waiting ranges written")
    _log_conflicts(synced["conflicts"])


def _log_conflicts(conflicts):
    if not conflicts:
        return
    conn = get_db()
    for c in conflicts:
        log_activity(conn, "serial_conflict", None,
                     f"SERIAL CONFLICT: {c['job_number']} took {c['start']:,}–{c['end']:,} for UPC {c['upc']} while "
                     f"the registry was offline, and {', '.join(c['clashes_with'])} used an overlapping range. "
                     "Check before encoding.", "System")
    conn.commit(); conn.close()


def auto_archive_completed():
    """Archive jobs that have been Complete for longer than the 'auto_archive_hours' setting (0 = off).
    Same effect as the Archive button: the record, folder and serials are untouched."""
    hours = get_settings()["auto_archive_hours"]
    if hours <= 0:
        return 0
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    jobs = [dict(r) for r in conn.execute(
        """SELECT * FROM jobs WHERE status='Complete' AND archived_at IS NULL
           AND completed_at IS NOT NULL AND completed_at <= ?""", (cutoff,))]
    ts, ranked = now_str(), False
    for job in jobs:
        conn.execute("UPDATE jobs SET archived_at=?, updated_at=?, priority_rank=NULL WHERE id=?", (ts, ts, job["id"]))
        ranked = ranked or job["priority_rank"] is not None
        log_activity(conn, "archived", job["id"],
                     f"{job_label(job)} archived automatically ({hours} h after it was completed)", "System")
    if ranked:
        _renumber_priority(conn); _bump_priority(conn)
    conn.commit(); conn.close()
    return len(jobs)


def start_auto_archive():
    def run():
        while True:
            try:
                n = auto_archive_completed()
                if n:
                    print(f"  [ARCHIVE] {n} completed job(s) archived automatically")
            except Exception as e:
                print(f"  [ARCHIVE] auto-archive failed: {e}")
            time.sleep(600)
    threading.Thread(target=run, daemon=True, name="auto-archive").start()


def start_serial_keeper():
    def run():
        while True:
            time.sleep(60)
            try:
                if SS.health()["pending"]:
                    _log_conflicts(SS.sync_pending()["conflicts"])
            except Exception as e:
                print(f"  [SER] sync failed: {e}")
    threading.Thread(target=run, daemon=True, name="serial-keeper").start()


@app.route("/api/serials/health")
def api_serials_health():
    return jsonify({**SS.health(probe=False), "registry_gaps": _registry_gap_count["n"]})


@app.route("/api/serials/<upc>")
def api_serials_history(upc):
    v = validate_upc(upc)
    if not v["valid"]:
        return err(v["error"])
    return jsonify(SS.history(upc))


@app.route("/api/serials/<upc>", methods=["POST"])
def api_serials_change(upc):
    """kind=reserve: set aside a range (no job). kind=history: record serials used
    outside this app up to last_used. Both are forward-only."""
    v = validate_upc(upc)
    if not v["valid"]:
        return err(v["error"])
    data = request.get_json(silent=True) or {}
    who = (data.get("operator") or "").strip()[:40] or actor()
    note = (data.get("note") or "").strip()[:200]
    if not who:
        return err("Enter your name")
    if not note:
        return err("Add a note saying why")
    try:
        if data.get("kind") == "history":
            last = _int(data.get("last_used"), "Last serial used", required=True)
            r = SS.record_history(upc, last, f"used outside this app: {note}", who)
            msg = f"UPC {upc}: serials up to {last:,} recorded as used outside the app — {note}"
        else:
            qty = _int(data.get("qty"), "Quantity", required=True)
            start = _int(data.get("start"), "Start serial")
            a = SS.reserve(upc, "RESERVED", qty, f"reserved: {note}", min_start=start, who=who)
            r = {"changed": True, **a}
            msg = f"UPC {upc}: reserved {a['start']:,}–{a['end']:,} — {note}"
    except SS.SerialsOffline as e:
        return err(f"{e} Nothing was changed.", 423)
    except ValueError as e:
        return err(str(e))
    if r.get("changed"):
        conn = get_db(); log_activity(conn, "serials", None, msg, who); conn.commit(); conn.close()
    return jsonify({"success": True, **r, "history": SS.history(upc)})


@app.route("/api/serials/conflicts/<int:row_id>/resolve", methods=["POST"])
def api_serials_resolve(row_id):
    data = request.get_json(silent=True) or {}
    who = (data.get("operator") or "").strip()[:40]
    note = (data.get("note") or "").strip()[:200]
    if not who or not note:
        return err("Enter your name and what you checked")
    try:
        row = SS.resolve_conflict(row_id, who, note)
    except SS.SerialsOffline as e:
        return err(str(e), 423)
    except ValueError as e:
        return err(str(e))
    conn = get_db()
    log_activity(conn, "serials", None, f"Serial conflict for {row['job_number']} (UPC {row['upc']}, "
                 f"{row['start']:,}–{row['end']:,}) reviewed: {note}. The range stays recorded.", who)
    conn.commit(); conn.close()
    return jsonify({"success": True, "health": SS.health()})


@app.route("/api/serials/<upc>/check")
def api_serials_check(upc):
    try:
        start, end = int(request.args["start"]), int(request.args["end"])
    except (KeyError, ValueError):
        return err("start and end are required")
    return jsonify(SS.check_range(upc, start, end))


# ══════════════════════════════════════════════════════════════════════════════
# Needs attention: open jobs with something wrong or waiting on someone
# ══════════════════════════════════════════════════════════════════════════════
ATTN_STALE_ROLL_HOURS = 12      # a roll still in progress this long after it was started was probably left open
ATTN_FOLDER_EVERY = 300         # seconds between background checks of the job folders on the share
# kind: (level, label, sort order). bad = something is wrong; warn = needs someone to look.
ATTN_KINDS = {
    "serial_conflict":     ("bad",  "Serial conflict", 0),
    "serial_check":        ("bad",  "Serial check", 0),        # from the integrity check (level per problem)
    "data_check":          ("warn", "Database check", 9),
    "folder_missing":      ("bad",  "Job folder missing", 1),
    "overdue":             ("bad",  "Overdue", 2),
    "complete_open_rolls": ("bad",  "Complete, rolls not done", 3),
    "due_soon":            ("warn", "Due soon, not started", 4),
    "on_hold":             ("warn", "On hold", 5),
    "stale_rolls":         ("warn", "Roll left in progress", 6),
    "rolls_done":          ("warn", "All rolls done", 7),
    "no_template":         ("warn", "No template", 8),
    "tag_check":           ("bad",  "Tag check failed", 1),
    "print_failed":        ("bad",  "BarTender didn't print", 2),
    "print_waiting":       ("warn", "Waiting on BarTender", 6),
}

# Job folders live on the share. Requests never look at them: a background thread does, every
# ATTN_FOLDER_EVERY seconds, and requests read the last result, so a slow or dropped share can't
# hold up the page.
_folders = {"at": None, "base_ok": None, "jobs": {}, "busy": False, "error": None}
_folders_lock = threading.Lock()


def check_job_folders() -> bool:
    """Look at every open job's folder: does it exist, and is there a .btw in its Print folder?
    Returns False without doing anything if a check is already running."""
    with _folders_lock:
        if _folders["busy"]:
            return False
        _folders["busy"] = True
    try:
        conn = get_db()
        rows = conn.execute("""SELECT id, folder_path FROM jobs WHERE archived_at IS NULL AND status != 'Cancelled'
                               AND folder_path IS NOT NULL AND folder_path != ''""").fetchall()
        conn.close()
        base_ok, jobs = os.path.isdir(Z_DRIVE_BASE), {}
        if base_ok:
            for r in rows:
                exists, btw = os.path.isdir(r["folder_path"]), False
                if exists:
                    try:
                        btw = any(e.name.lower().endswith(".btw") and e.is_file()
                                  for e in os.scandir(os.path.join(r["folder_path"], "Print")))
                    except OSError:
                        pass
                jobs[r["id"]] = {"exists": exists, "btw": btw}
        _folders.update(at=now_str(), base_ok=base_ok, jobs=jobs, error=None)
    except Exception as e:
        _folders.update(error=str(e))
    finally:
        _folders["busy"] = False
    return True


def start_folder_watch():
    def run():
        while True:
            check_job_folders()
            time.sleep(ATTN_FOLDER_EVERY)
    threading.Thread(target=run, daemon=True, name="folder-watch").start()


def _due(s):
    """A due date as typed (YYYY-MM-DD, or MM/DD/YY or MM/DD/YYYY, as the Jobs list reads them), or None."""
    s = (s or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})", s)
        if not m:
            return None
        mo, d, y = int(m[1]), int(m[2]), int(m[3])
        y += 2000 if y < 100 else 0
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def attention(conn, today=None) -> dict:
    """Every open job with at least one issue, worst first, plus issues that aren't about one job.
    Reads the database and the last background folder check only (see check_job_folders)."""
    today = today or date.today()
    stale_before = (datetime.now() - timedelta(hours=ATTN_STALE_ROLL_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    jobs = {r["id"]: dict(r) for r in conn.execute(f"""
        SELECT j.id, j.job_number, j.job_ticket, j.customer, j.title, j.status, j.due_date, j.upc, j.job_type,
               j.template_path, j.folder_path, j.hold_reason, j.hold_note, j.hold_since, j.hold_by, {ROLL_SUMMARY}
        FROM jobs j WHERE j.archived_at IS NULL AND j.status != 'Cancelled'""")}
    issues = {jid: [] for jid in jobs}
    glob = []

    def add(jid, kind, detail):
        level, label, _ = ATTN_KINDS[kind]
        (issues[jid] if jid is not None else glob).append({"kind": kind, "level": level, "label": label, "detail": detail})

    folders = dict(_folders)
    if folders["at"] and folders["base_ok"] is False:
        glob.append({"kind": "folder_missing", "level": "bad", "label": "Job folders not reachable",
                     "detail": f"The host PC can't reach {Z_DRIVE_BASE}, so job folders weren't checked."})
    for jid, j in jobs.items():
        open_ = j["status"] not in ("Complete", "Cancelled")
        due = _due(j["due_date"])
        if open_ and due:
            days = (due - today).days
            if days < 0:
                add(jid, "overdue", f"Due {j['due_date']}, {-days} day{'s' if days != -1 else ''} late")
            elif days <= 1 and j["status"] == "Pending" and not (j["roll_running"] or j["roll_paused"] or j["roll_done"]):
                add(jid, "due_soon", f"Due {'today' if days == 0 else 'tomorrow'} and not started")
        if j["status"] == "On Hold":
            span = _span(j["hold_since"])
            add(jid, "on_hold", (f"{j['hold_reason']}: {j['hold_note']}" if j["hold_reason"] else "No reason recorded")
                + (f" ({span})" if span else ""))
        total, done = j["roll_total"] or 0, j["roll_done"] or 0
        if total and done == total and open_:
            add(jid, "rolls_done", f"All {total} rolls are done, but the job isn't marked complete")
        if total and done < total and j["status"] == "Complete":
            add(jid, "complete_open_rolls", f"Marked complete with {total - done} of {total} rolls not done")
        f = folders["jobs"].get(jid) if folders["base_ok"] else None
        if open_ and not j["folder_path"]:
            add(jid, "folder_missing", "No job folder was created")
        elif open_ and f and not f["exists"]:
            add(jid, "folder_missing", f"Not found: {j['folder_path']}")
        if j["status"] == "Pending" and j["job_type"] == "standard" and not j["template_path"] and f and f["exists"] and not f["btw"]:
            add(jid, "no_template", "Queued with no BarTender template in its Print folder")

    stale = {}
    for r in conn.execute("""SELECT r.job_id, r.roll_number, r.printer, r.started_at, r.started_by, r.status
            FROM job_rolls r JOIN jobs j ON j.id = r.job_id
            WHERE j.archived_at IS NULL AND j.status != 'Cancelled' AND r.status IN ('running', 'paused')
              AND r.started_at IS NOT NULL AND r.started_at <= ? ORDER BY r.job_id, r.roll_number""", (stale_before,)):
        stale.setdefault(r["job_id"], []).append(r)
    for jid, rolls in stale.items():
        if jid in issues:
            first = rolls[0]
            where = f" on {first['printer']}" if first["printer"] else ""
            by = f" by {first['started_by']}" if first["started_by"] else ""
            more = f" (and {len(rolls) - 1} more)" if len(rolls) > 1 else ""
            add(jid, "stale_rolls", f"Roll {first['roll_number']} started{where}{by} {_span(first['started_at'])} ago "
                                    f"and still in progress{more}")

    # Tag checks: a roll whose latest check (last 3 days) failed and that hasn't been verified since.
    since = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    for c in conn.execute("""SELECT c.job_id, c.roll_number, c.message, c.ts, c.who FROM roll_checks c
            JOIN job_rolls r ON r.job_id = c.job_id AND r.roll_number = c.roll_number
            WHERE c.ts >= ? AND c.result = 'fail' AND (r.verified_at IS NULL OR r.verified_at < c.ts)
              AND c.id = (SELECT MAX(id) FROM roll_checks x WHERE x.job_id = c.job_id AND x.roll_number = c.roll_number)
            ORDER BY c.job_id, c.roll_number""", (since,)):
        if c["job_id"] in issues:
            add(c["job_id"], "tag_check", f"Roll {c['roll_number']}: {c['message']} ({c['ts'][11:16]}"
                                          + (f", {c['who']}" if c["who"] else "") + "). Scan it again once it's sorted.")
    # BarTender: each roll's latest request.
    stuck_before = (datetime.now() - timedelta(minutes=F.BT_STUCK_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    for p in conn.execute("""SELECT p.* FROM print_requests p WHERE p.id = (SELECT MAX(id) FROM print_requests x
            WHERE x.job_id = p.job_id AND x.roll_number = p.roll_number AND x.part = p.part)
            AND p.state IN ('failed','writing','sent') ORDER BY p.job_id, p.roll_number"""):
        if p["job_id"] not in issues:
            continue
        roll = f"Roll {p['roll_number']}" + (f" part {p['part']}" if p["parts"] > 1 else "")
        if p["state"] == "failed" and p["created_at"] >= since:
            add(p["job_id"], "print_failed", f"{roll}: {p['message'] or 'BarTender reported an error'}")
        elif p["state"] in ("writing", "sent") and p["created_at"] <= stuck_before:
            add(p["job_id"], "print_waiting", f"{roll} sent {_span(p['created_at'])} ago and not picked up yet. "
                                              "Is the BarTender Integration running?" if p["state"] == "sent" else
                                              f"{roll}: the trigger folder hasn't taken the request for {_span(p['created_at'])}.")
    if _bt["error"]:
        glob.append({"kind": "print_waiting", "level": "warn", "label": "BarTender folders", "detail": _bt["error"]})

    by_number = {j["job_number"]: jid for jid, j in jobs.items()}
    last = _integrity["result"]          # the last serial and database check (see run_integrity)
    for p in (last or {}).get("problems", []):
        kind = "data_check" if p["kind"] == "database" else "serial_check"
        item = {"kind": kind, "level": p["level"], "label": "Serial check" if kind == "serial_check" else "Database check",
                "detail": p["detail"]}
        if p.get("job_id") in issues:
            issues[p["job_id"]].append(item)
        else:
            glob.append(item)
    for c in conn.execute('SELECT job_number, upc, start, "end" FROM serial_allocations WHERE registry_state=-1'):
        detail = f"UPC {c['upc']}, serials {c['start']:,}–{c['end']:,} overlap another range. Check before encoding."
        jid = by_number.get(c["job_number"])
        if jid is not None:
            add(jid, "serial_conflict", detail)
        else:
            glob.append({"kind": "serial_conflict", "level": "bad", "label": f"Serial conflict ({c['job_number']})", "detail": detail})

    keep = ("id", "job_number", "job_ticket", "customer", "title", "status", "due_date", "upc")
    items = []
    for jid, found in issues.items():
        if found:
            found.sort(key=lambda i: ATTN_KINDS[i["kind"]][2])
            items.append({"job": {k: jobs[jid][k] for k in keep}, "issues": found,
                          "level": "bad" if any(i["level"] == "bad" for i in found) else "warn"})
    items.sort(key=lambda it: (it["level"] != "bad", ATTN_KINDS[it["issues"][0]["kind"]][2],
                               str(_due(it["job"]["due_date"]) or "9999"), str(it["job"]["job_ticket"] or it["job"]["job_number"])))
    counts = {}
    for it in items:
        for i in it["issues"]:
            counts[i["kind"]] = counts.get(i["kind"], 0) + 1
    return {"items": items, "global": glob, "counts": counts,
            "kinds": [{"kind": k, "level": v[0], "label": v[1]} for k, v in sorted(ATTN_KINDS.items(), key=lambda kv: kv[1][2])],
            "bad": sum(1 for it in items if it["level"] == "bad") + sum(1 for g in glob if g["level"] == "bad"),
            "total": len(items) + len(glob), "folders_checked_at": folders["at"],
            "stale_roll_hours": ATTN_STALE_ROLL_HOURS, "now": now_str()}


# ── Serial and database check (integrity.py) ──────────────────────────────────
# Read-only. Runs a couple of minutes after startup and every INTEGRITY_EVERY hours, and on demand
# (Settings > Data, "Check now"). Problems go into Needs attention. Database only: no share is touched.
INTEGRITY_EVERY = 6
_integrity = {"result": None, "error": None}
_integrity_lock = threading.Lock()


def run_integrity() -> dict:
    with _integrity_lock:
        conn = get_db()
        try:
            r = integrity.run(conn)
            _integrity.update(result=r, error=None)
        except Exception as e:
            _integrity.update(error=str(e))
            log.exception("integrity check failed")
            raise
        finally:
            conn.close()
    bad = r["counts"].get("bad", 0)
    if bad:
        log.warning("integrity check: %d serious problem(s): %s", bad,
                    "; ".join(p["detail"] for p in r["problems"] if p["level"] == "bad")[:2000])
    return r


def start_integrity_watch():
    def run():
        time.sleep(120)
        while True:
            try:
                run_integrity()
            except Exception as e:
                print(f"  [CHECK] serial/database check failed: {e}")
            time.sleep(INTEGRITY_EVERY * 3600)
    threading.Thread(target=run, daemon=True, name="integrity").start()


@app.route("/api/integrity")
def api_integrity():
    return jsonify({"result": _integrity["result"], "error": _integrity["error"], "every_hours": INTEGRITY_EVERY})


@app.route("/api/integrity", methods=["POST"])
def api_integrity_run():
    try:
        r = run_integrity()
    except Exception as e:
        return err(f"The check couldn't run: {e}", 500)
    conn = get_db()
    log_activity(conn, "check", None, "Serial and database check: " + (
        "everything adds up" if r["ok"] else
        f"{r['counts'].get('bad', 0)} problem(s), {r['counts'].get('warn', 0)} to look at"))
    conn.commit(); conn.close()
    return jsonify({"result": r})


@app.route("/api/tools/db-upkeep", methods=["POST"])
def api_db_upkeep():
    r = db_upkeep()
    return err(r["error"], 500) if r.get("error") else jsonify(r)


@app.route("/api/tools/host-check")
def api_host_check():
    """Tools > Check this PC: the automatic part of the host PC check (host_check.py), run inside the
    app, saved to check_reports/ like the JM_TOOLS.bat version. Each check has a time limit."""
    started = time.time()
    results = H.run_automatic(include_server=False)
    results.append(H._res("Job Manager running", H.PASS, f"Answering on port {PORT} (this check ran inside it)"))
    rep = H.report(results, f"Tools page ({actor() or 'unnamed PC'})", started)
    try:
        rep["saved_to"] = H.save(rep)
    except OSError as e:
        rep["saved_to"] = None; rep["save_error"] = str(e)
    return jsonify(rep)


@app.route("/api/attention")
def api_attention():
    """?refresh=1 also starts a new folder check in the background (the answer doesn't wait for it)."""
    if request.args.get("refresh") == "1" and not _folders["busy"]:
        threading.Thread(target=check_job_folders, daemon=True, name="folder-check").start()
    conn = get_db()
    try:
        return jsonify(attention(conn))
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# Encoding floor: BarTender print requests, tag checks, reprints, pace, shift summary, repeat job
# (logic in floor_ops.py)
# ══════════════════════════════════════════════════════════════════════════════
BT_WRITE_WAIT = 10                   # seconds a request waits for the trigger file to be written
BT_WATCH_EVERY = 5                   # seconds between looks at the trigger and acknowledgement folders
BT_OPEN = ("writing", "sent", "picked_up")
_bt = {"at": None, "error": None, "busy": False}


def _floor_detail(conn, job_id, rolls):
    """Extra parts of a job's detail: tag checks, reprints, print requests, pace."""
    q = lambda sql: [dict(r) for r in conn.execute(sql, (job_id,))]
    checks = q("SELECT * FROM roll_checks WHERE job_id=? ORDER BY id DESC LIMIT 300")
    reprints = q("SELECT * FROM reprints WHERE job_id=? ORDER BY id DESC")
    prints = q("SELECT * FROM print_requests WHERE job_id=? ORDER BY id DESC LIMIT 300")
    s = get_settings()
    return {"checks": checks, "reprints": reprints, "prints": prints, "pace": F.pace(rolls),
            "reprint_reasons": F.REPRINT_REASONS, "reprinted_total": sum(r["qty"] for r in reprints),
            "bt": {"enabled": s["bt_enabled"], "mapped": sorted(F.printer_map(s["bt_printer_map"]))}}


def _db_files(job, total):
    """The job's encoding database file name(s), in order, as the generators name them."""
    parts = len(db_file_split(total, job.get("qty_db")))
    if job.get("upc"):
        ext = ".xlsx" if XLSX_AVAILABLE else ".csv"
        return [f"{job['upc']}{f'_DB{i}' if parts > 1 else ''}{ext}" for i in range(1, parts + 1)]
    if job.get("data_sheet"):
        return go2_file_names(job.get("job_ticket") or job["job_number"], parts)
    return []


def _safe_id(text):
    return re.sub(r"[^A-Za-z0-9]+", "-", str(text)).strip("-")[:40]


def _bt_write(rows, trigger_dir):
    """Background: write each request's file, then mark it sent (or failed). Own connection."""
    conn = get_db()
    try:
        os.makedirs(trigger_dir, exist_ok=True)
        for r in rows:
            path = F.write_request(trigger_dir, r["req_id"], r["text"])
            conn.execute("UPDATE print_requests SET state='sent', trigger_path=?, updated_at=? WHERE req_id=? AND state='writing'",
                         (path, now_str(), r["req_id"]))
            conn.commit()
    except OSError as e:
        conn.execute(f"""UPDATE print_requests SET state='failed', message=?, updated_at=?
                         WHERE state='writing' AND req_id IN ({','.join('?' * len(rows))})""",
                     (f"Couldn't write to the BarTender trigger folder: {e}"[:300], now_str(), *[r["req_id"] for r in rows]))
        conn.commit()
    finally:
        conn.close()


@app.route("/api/jobs/<int:job_id>/rolls/<int:roll_number>/print", methods=["POST"])
def api_roll_print(job_id, roll_number):
    """Send one roll to BarTender: a BTXML file per database file the roll's records are in, written into the
    trigger folder that a BarTender Integration watches. A roll can't be sent while an earlier request for it
    is still waiting, and a roll already printed needs again=true. A roll not started yet is started."""
    data = request.get_json(silent=True) or {}
    who = (data.get("operator") or "").strip()[:40] or actor()
    s = get_settings()
    if not s["bt_enabled"]:
        return err("Sending to BarTender is turned off (Settings > BarTender).")
    if not s["bt_trigger_dir"]:
        return err("No BarTender trigger folder is set (Settings > BarTender).")
    conn = get_db()
    try:
        job = _load_job(conn, job_id)
        if not job:
            return err("Job not found", 404)
        if job["status"] in ("On Hold", "Cancelled"):
            return err(f"This job is {job['status'].lower()}. Resume it before printing.")
        if not job.get("template_path"):
            return err("This job has no BarTender template in its Print folder. Pick one in the job first.")
        rolls = ensure_rolls(conn, job)
        roll = next((r for r in rolls if r["roll_number"] == roll_number), None)
        if not roll:
            return err("Roll not found", 404)
        if roll["status"] in ("complete", "verified"):
            return err("This roll is done. Reopen it to print it again, or log a reprint.")
        prev = [dict(r) for r in conn.execute("SELECT * FROM print_requests WHERE job_id=? AND roll_number=? ORDER BY id DESC",
                                              (job_id, roll_number))]
        waiting = [p for p in prev if p["state"] in BT_OPEN]
        if waiting:
            p = waiting[0]
            return err(f"Roll {roll_number} was sent to BarTender at {p['created_at'][11:16]} and BarTender hasn't "
                       f"{'picked it up' if p['state'] != 'picked_up' else 'answered'} yet. Wait for it, or cancel it first.",
                       409, waiting=True)
        done = [p for p in prev if p["state"] in ("printed", "taken")]
        if done and not data.get("again"):
            return err(f"Roll {roll_number} was already sent to BarTender at {done[0]['updated_at'][5:16]} "
                       f"by {done[0]['who'] or 'someone'}. Send it again?", 409, needs_again=True)
        printer = " ".join(str(data.get("printer") or roll.get("printer") or "").split())[:30] or None
        bt_printer = F.printer_map(s["bt_printer_map"]).get((printer or "").lower())
        total = sum(r["quantity"] for r in rolls)
        names = _db_files(job, total)
        segs = F.segments(rolls, roll_number, job.get("qty_db"))
        ts, rows = now_str(), []
        for i, (f, a, b) in enumerate(segs, start=1):
            req_id = f"JM-{_safe_id(job['job_ticket'] or job['job_number'])}-R{roll_number}" + (
                f"-P{i}" if len(segs) > 1 else "") + f"-{secrets.token_hex(3)}"
            db_path = os.path.join(job["folder_path"], "Data", names[f - 1]) if job.get("folder_path") and len(names) >= f else None
            label = f"{job_label(job)} roll {roll_number}" + (f" part {i} of {len(segs)}" if len(segs) > 1 else "")
            text = F.btxml(req_id, job["template_path"], a, b, bt_printer, db_path, label)
            conn.execute("""INSERT INTO print_requests (req_id, job_id, roll_number, part, parts, template, database, rec_from,
                            rec_to, printer, bt_printer, state, who, created_at, updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,'writing',?,?,?)""",
                         (req_id, job_id, roll_number, i, len(segs), job["template_path"], db_path, a, b, printer, bt_printer, who, ts, ts))
            rows.append({"req_id": req_id, "text": text, "file": f, "from": a, "to": b, "database": db_path})
        if roll["status"] == "pending":
            conn.execute("""UPDATE job_rolls SET status='running', started_at=?, started_by=?, printer=COALESCE(?, printer)
                            WHERE job_id=? AND roll_number=?""", (ts, who, printer, job_id, roll_number))
            if job["status"] == "Pending":
                conn.execute("UPDATE jobs SET status='In Progress' WHERE id=?", (job_id,))
        elif printer and printer != roll.get("printer"):
            conn.execute("UPDATE job_rolls SET printer=? WHERE job_id=? AND roll_number=?", (printer, job_id, roll_number))
        rec = ", ".join(f"records {r['from']}–{r['to']}" + (f" of DB{r['file']}" if len(names) > 1 else "") for r in rows)
        log_activity(conn, "print", job_id, f"{job_label(job)}: roll {roll_number} sent to BarTender ({rec})"
                     + (f" on {printer}" if printer else "") + (" — again" if done else ""), who)
        conn.execute("UPDATE jobs SET updated_at=? WHERE id=?", (ts, job_id))
        conn.commit()
    finally:
        conn.close()
    t = threading.Thread(target=_bt_write, args=(rows, s["bt_trigger_dir"]), daemon=True, name="bt-write")
    t.start(); t.join(BT_WRITE_WAIT)
    conn = get_db()
    got = [dict(r) for r in conn.execute(f"SELECT * FROM print_requests WHERE req_id IN ({','.join('?' * len(rows))}) ORDER BY part",
                                         [r["req_id"] for r in rows])]
    conn.close()
    warn = []
    if printer and not bt_printer:
        warn.append(f"{printer} has no Windows printer name in Settings > BarTender, so BarTender uses the printer saved in the template.")
    elif not printer:
        warn.append("The roll has no printer, so BarTender uses the printer saved in the template.")
    if len(names) > 1:
        warn.append("This job's database is split into files. BarTender prints from the file the template is pointed at, "
                    "so check it is " + " / ".join(os.path.basename(r["database"] or "") for r in rows) + ".")
    failed = [g_ for g_ in got if g_["state"] == "failed"]
    if failed:
        return err(failed[0]["message"] or "The print request couldn't be written.", 502, requests=got)
    if t.is_alive():
        warn.append("The trigger folder is slow to answer; the request will go when it can be written.")
    return jsonify({"success": True, "requests": got, "warnings": warn})


@app.route("/api/print-requests/<int:rid>/cancel", methods=["POST"])
def api_print_cancel(rid):
    """Take back a request BarTender hasn't picked up yet (its file is removed from the trigger folder)."""
    conn = get_db()
    row = conn.execute("SELECT * FROM print_requests WHERE id=?", (rid,)).fetchone()
    if not row:
        conn.close(); return err("Not found", 404)
    row = dict(row)
    if row["state"] == "picked_up":
        conn.close()
        return err("BarTender has already taken this request. Stop it in BarTender (Print Portal or the printer queue) if needed.", 409)
    if row["state"] not in ("writing", "sent"):
        conn.close(); return err("This request is finished; nothing to cancel.", 409)
    path = row["trigger_path"]
    gone = {"ok": not path}

    def rm():
        try:
            os.remove(path)
            gone["ok"] = True
        except FileNotFoundError:
            gone["picked"] = True                  # BarTender took it a moment ago
        except OSError as e:
            gone["error"] = str(e)
    if path:
        t = threading.Thread(target=rm, daemon=True); t.start(); t.join(BT_WRITE_WAIT)
    if gone.get("picked"):
        conn.execute("UPDATE print_requests SET state='picked_up', updated_at=? WHERE id=?", (now_str(), rid))
        conn.commit(); conn.close()
        return err("BarTender picked it up just now, so it can't be cancelled.", 409)
    if not gone.get("ok"):
        conn.close()
        return err(f"Couldn't remove the request from the trigger folder: {gone.get('error') or 'the folder is not answering'}", 502)
    who = actor()
    conn.execute("UPDATE print_requests SET state='cancelled', message=?, updated_at=? WHERE id=?",
                 (f"Cancelled by {who or 'someone'}", now_str(), rid))
    log_activity(conn, "print", row["job_id"], f"{job_label(_load_job(conn, row['job_id']))}: BarTender request for roll "
                                              f"{row['roll_number']} cancelled", who)
    conn.commit(); conn.close()
    return jsonify({"success": True})


def check_print_requests():
    """Background (BT_WATCH_EVERY): a request whose file has left the trigger folder was picked up; a file in
    the acknowledgement folder naming the request (or containing its ID) says printed or failed.
    With no acknowledgement folder set, picked up is as far as it goes ('taken')."""
    if _bt["busy"]:
        return
    _bt["busy"] = True
    conn = get_db()
    try:
        open_ = [dict(r) for r in conn.execute(
            "SELECT * FROM print_requests WHERE state IN ('sent','picked_up') AND created_at >= ?",
            ((datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"),))]
        conn.execute("""UPDATE print_requests SET state='failed', message='No answer from BarTender within 2 days', updated_at=?
                        WHERE state IN ('sent','picked_up') AND created_at < ?""",
                     (now_str(), (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        if not open_:
            _bt.update(at=now_str(), error=None)
            return
        ack = get_settings()["bt_ack_dir"]
        files = []
        if ack:
            try:
                files = [e for e in os.scandir(ack) if e.is_file()]
            except OSError as e:
                _bt.update(at=now_str(), error=f"Can't read the acknowledgement folder {ack}: {e}")
                files = None
        oldest = min(datetime.strptime(p["created_at"][:19], "%Y-%m-%d %H:%M:%S").timestamp() for p in open_) - 120
        recent = []
        for e in files or []:
            try:
                if e.stat().st_mtime >= oldest:
                    recent.append(e)
            except OSError:
                pass
        recent = recent[:200]
        texts = {}
        for p in open_:
            state, msg = None, None
            hit = next((e.path for e in recent if p["req_id"].lower() in e.name.lower()), None)
            if not hit:
                for e in recent:
                    if e.path not in texts:
                        try:
                            with open(e.path, encoding="utf-8", errors="replace") as fh:
                                texts[e.path] = fh.read(65536)
                        except OSError:
                            texts[e.path] = ""
                    if p["req_id"] in texts[e.path]:
                        hit = e.path; break
            if hit:
                try:
                    with open(hit, encoding="utf-8", errors="replace") as fh:
                        state, msg = F.read_reply(fh.read(200_000))
                except OSError:
                    pass
            elif p["state"] == "sent" and p["trigger_path"] and not os.path.exists(p["trigger_path"]):
                state = "picked_up" if ack else "taken"
                msg = None if ack else "Taken by BarTender (no acknowledgement folder is set, so printing isn't confirmed)"
            if state and state != p["state"]:
                conn.execute("UPDATE print_requests SET state=?, message=?, updated_at=? WHERE id=?", (state, msg, now_str(), p["id"]))
                if state in ("printed", "failed"):
                    job = _load_job(conn, p["job_id"])
                    part = f" (part {p['part']} of {p['parts']})" if p["parts"] > 1 else ""
                    log_activity(conn, "print" if state == "printed" else "print_fail", p["job_id"],
                                 f"{job_label(job) if job else 'Job'}: roll {p['roll_number']}{part} "
                                 + ("printed by BarTender" if state == "printed" else f"BarTender couldn't print it — {msg}"), "BarTender")
                conn.commit()
        if files is not None:
            _bt.update(at=now_str(), error=None)
    except Exception as e:
        _bt.update(at=now_str(), error=str(e))
        log.exception("print request check failed")
    finally:
        conn.close()
        _bt["busy"] = False


def start_print_watch():
    def run():
        while True:
            check_print_requests()
            time.sleep(BT_WATCH_EVERY)
    threading.Thread(target=run, daemon=True, name="bt-watch").start()


@app.route("/api/jobs/<int:job_id>/reprints", methods=["POST"])
def api_reprint_add(job_id):
    """Log labels reprinted or spoiled on a roll: how many, why, optionally which serials."""
    data = request.get_json(silent=True) or {}
    who = (data.get("operator") or "").strip()[:40] or actor()
    conn = get_db()
    try:
        job = _load_job(conn, job_id)
        if not job:
            return err("Job not found", 404)
        ensure_rolls(conn, job)
        try:
            n = int(data.get("roll_number"))
        except (TypeError, ValueError):
            return err("Pick a roll")
        roll = conn.execute("SELECT * FROM job_rolls WHERE job_id=? AND roll_number=?", (job_id, n)).fetchone()
        if not roll:
            return err("Roll not found", 404)
        reason = str(data.get("reason") or "").strip()
        if reason not in F.REPRINT_REASONS:
            return err("Pick a reason")
        note = " ".join(str(data.get("note") or "").split())[:300]
        if reason == "Other" and not note:
            return err("Say what happened")
        sf, st_ = data.get("serial_from"), data.get("serial_to")
        if sf not in (None, "") or st_ not in (None, ""):
            try:
                sf, st_ = int(str(sf).replace(",", "")), int(str(st_ if st_ not in (None, "") else sf).replace(",", ""))
            except ValueError:
                return err("Serials must be numbers")
            if sf > st_:
                sf, st_ = st_, sf
            if roll["serial_start"] is None or sf < roll["serial_start"] or st_ > roll["serial_end"]:
                return err(f"Those serials aren't on roll {n}" + (f" ({roll['serial_start']:,}–{roll['serial_end']:,})"
                                                                  if roll["serial_start"] is not None else ""))
            qty = st_ - sf + 1
        else:
            sf = st_ = None
            try:
                qty = int(str(data.get("qty") or "").replace(",", ""))
            except ValueError:
                return err("How many labels?")
        if not 1 <= qty <= roll["quantity"]:
            return err(f"How many labels? (1 to {roll['quantity']:,}, the roll's size)")
        ts = now_str()
        conn.execute("""INSERT INTO reprints (job_id, roll_number, qty, serial_from, serial_to, reason, note, who, ts)
                        VALUES (?,?,?,?,?,?,?,?,?)""", (job_id, n, qty, sf, st_, reason, note or None, who, ts))
        log_activity(conn, "reprint", job_id, f"{job_label(job)}: roll {n}, {qty:,} label{'s' if qty != 1 else ''} reprinted — "
                     f"{reason}" + (f": {note[:80]}" if note else "") + (f" (serials {sf:,}–{st_:,})" if sf else ""), who)
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()


@app.route("/api/reprints/<int:rid>", methods=["DELETE"])
def api_reprint_delete(rid):
    conn = get_db()
    row = conn.execute("SELECT * FROM reprints WHERE id=?", (rid,)).fetchone()
    if not row:
        conn.close(); return err("Not found", 404)
    conn.execute("DELETE FROM reprints WHERE id=?", (rid,))
    job = _load_job(conn, row["job_id"])
    log_activity(conn, "reprint", row["job_id"], f"{job_label(job) if job else 'Job'}: reprint entry removed "
                 f"(roll {row['roll_number']}, {row['qty']:,} labels, {row['reason']}, logged by {row['who'] or '?'})")
    conn.commit(); conn.close()
    return jsonify({"success": True})


@app.route("/api/shift")
def api_shift():
    which = "previous" if request.args.get("which") == "previous" else "current"
    start, end, label = F.shift_window(get_settings()["shift_starts"], which)
    conn = get_db()
    try:
        return jsonify({"label": label, "which": which, **F.shift_summary(conn, start, end)})
    finally:
        conn.close()


@app.route("/shift")
def shift_page():
    """Printable shift hand-off: ?which=current|previous, &print=1 opens the print dialog."""
    which = "previous" if request.args.get("which") == "previous" else "current"
    start, end, label = F.shift_window(get_settings()["shift_starts"], which)
    conn = get_db()
    try:
        d = F.shift_summary(conn, start, end)
    finally:
        conn.close()
    return Response(F.shift_html(d, f"{'Previous' if which == 'previous' else 'This'} shift, {label}",
                                 request.args.get("print") == "1"), mimetype="text/html")


REPEAT_FIELDS = ("customer", "title", "label_size", "inlay_type", "part_number", "upc", "lpr", "qty_db",
                 "overage_pct", "job_type", "data_sheet")


@app.route("/api/jobs/<int:job_id>/repeat")
def api_job_repeat(job_id):
    """What a new job like this one starts with: the product (customer, item, size, inlay, part, UPC,
    labels per roll, rows per database file, overage, data sheet) and the same template. Never the
    ticket, PO, quantity, due date or serials: those belong to the new order."""
    conn = get_db()
    job = _load_job(conn, job_id)
    if not job:
        conn.close(); return err("Job not found", 404)
    out = {k: job.get(k) for k in REPEAT_FIELDS}
    tpl = None
    if job.get("template_path"):
        base = re.split(r"[\\/]", job["template_path"])[-1]
        m = re.match(r"^JOB-\d+ - (.+)$", base, re.I)
        name = (m.group(1) if m else base).lower()
        for r in conn.execute("SELECT id, name, file_path FROM btw_templates WHERE active=1"):
            if re.split(r"[\\/]", r["file_path"])[-1].lower() == name:
                tpl = {"id": r["id"], "name": r["name"], "file_name": re.split(r"[\\/]", r["file_path"])[-1], "file_path": r["file_path"]}
                break
        if not tpl:
            missing = m.group(1) if m else base
    conn.close()
    return jsonify({"fields": out, "template": tpl, "template_missing": missing if job.get("template_path") and not tpl else None,
                    "from": job_label(job)})


# ══════════════════════════════════════════════════════════════════════════════
# Tools: standalone checklist, roll tracker sheet, encoding database
# ══════════════════════════════════════════════════════════════════════════════
def _tool_job(a):
    g = lambda k: (a.get(k) or "").strip()
    def n(k):
        try:
            return int(float(g(k).replace(",", ""))) if g(k) else 0
        except ValueError:
            return 0
    return {"customer": g("customer"), "job_ticket": g("job_ticket"), "title": g("title"), "upc": g("upc"),
            "po_number": g("po_number"), "part_number": g("part_number"), "inlay_type": g("inlay_type"),
            "label_size": g("label_size"), "due_date": g("due_date"), "quantity": n("quantity"),
            "lpr": n("lpr"), "qty_db": n("qty_db") or None, "start_serial": n("start_serial") or None,
            "overage_pct": float(g("overage_pct") or 0), "job_number": g("job_ticket") or "TOOL"}


@app.route("/tools/roll-sheet")
def tool_roll_sheet():
    """Printable roll tracker / QC sheet from form values (no job, nothing recorded)."""
    j = _tool_job(request.args)
    total = production_qty(j["quantity"], j["overage_pct"])
    if j["upc"] and j["start_serial"] and validate_upc(j["upc"])["valid"]:
        rolls = roll_plan(j["upc"], j["start_serial"], total, j["lpr"])
    else:
        rolls = roll_plan_plain(total, j["lpr"])
    html, _ = qc_html(j, rolls, auto_print=request.args.get("print") == "1")
    return Response(html, mimetype="text/html")


@app.route("/tools/checklist")
def tool_checklist():
    j = _tool_job(request.args)
    tmp = tempfile.mkdtemp()
    try:
        r = generate_checklist_html(tmp, j)
        if not r.get("success"):
            return Response(f"Checklist error: {escape(str(r.get('error')))}", status=500)
        html = open(r["path"], encoding="utf-8").read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if request.args.get("print") == "1":
        html = html.replace("</body>", "<script>addEventListener('load',()=>setTimeout(()=>print(),300))</script></body>")
    return Response(html, mimetype="text/html")


@app.route("/api/tools/database", methods=["POST"])
def tool_database():
    """mode=new: reserve a NEW range for a UPC (recorded like a job) and download the files.
    mode=job: download the encoding files for an existing job's recorded range."""
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "new")
    tmp = tempfile.mkdtemp()
    try:
        if mode == "job":
            conn = get_db()
            job = _load_job(conn, int(data.get("job_id") or 0)); conn.close()
            if not job or not job["upc"] or not job["start_serial"]:
                return err("That job has no serial range (special jobs have no encoding database)")
            upc, start = job["upc"], job["start_serial"]
            total = production_qty(job["quantity"], job["overage_pct"] or 0)
            qty_db, label = job["qty_db"], job["job_number"]
        else:
            upc = (data.get("upc") or "").strip()
            v = validate_upc(upc)
            if not v["valid"]:
                return err(v["error"])
            try:
                qty = _int(data.get("quantity"), "Quantity", required=True)
                qty_db = _int(data.get("qty_db"), "Qty per DB file")
            except ValueError as e:
                return err(str(e))
            who = (data.get("operator") or "").strip()[:40] or actor()
            reason = (data.get("note") or "").strip()[:120]
            if not who or not reason:
                return err("Enter your name and what the file is for")
            total = production_qty(qty, float(data.get("overage_pct") or 0))
            label = f"TOOL-{datetime.now():%y%m%d-%H%M%S}"
            try:
                a = SS.reserve(upc, label, total, f"Tools database: {reason}", who=who)
            except SS.SerialsOffline as e:
                return err(f"{e} The Tools page never gives out serials without the registry.", 423)
            except ValueError as e:
                return err(str(e))
            start = a["start"]
            conn = get_db()
            log_activity(conn, "serials", None, f"Tools: {total:,} serials {start:,}–{a['end']:,} for UPC {upc} "
                                                 f"recorded as {label} — {reason}", who)
            conn.commit(); conn.close()
        info = generate_db_files(tmp, upc, start, total, qty_db)
        files = info["files"]
        if len(files) == 1:
            buf = io.BytesIO(open(files[0], "rb").read())
            name = f"{label} - {os.path.basename(files[0])}"
        else:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for f in files:
                    z.write(f, os.path.basename(f))
            name = f"{label} - {upc} databases.zip"
        buf.seek(0)
        resp = send_file(buf, as_attachment=True, download_name=name)
        resp.headers["X-Serial-Range"] = f"{start}-{start + total - 1}"
        resp.headers["X-File-Name"] = name
        return resp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.errorhandler(500)
def _err500(e):
    orig = getattr(e, "original_exception", None) or e
    rid = getattr(g, "rid", "") or secrets.token_hex(4)
    who = ""
    try:
        who = actor() or ""
    except Exception:
        pass
    log.error("error ref=%s %s %s operator=%r\n%s", rid, request.method, request.full_path.rstrip("?"), who,
              "".join(traceback.format_exception(type(orig), orig, orig.__traceback__)).rstrip())
    if request.path.startswith(("/api/", "/proofing/api/")):
        return err(f"Server error: {orig} (ref {rid})", 500, ref=rid)
    return Response(f"Server error (ref {rid}). The details are in logs/jobmanager.log on the host PC.",
                    status=500, mimetype="text/plain")


def _lan_ips():
    """This PC's network addresses, for the startup message."""
    import socket
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1)); ips.add(s.getsockname()[0]); s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


# RFID proofing at /proofing. Registered here, after everything it borrows is defined.
proofing.init_app(app, get_db=get_db, log_activity=log_activity, job_label=job_label, now_str=now_str,
                  match_templates=match_templates_fresh, jm_settings=get_settings, z_drive_base=Z_DRIVE_BASE)


def port_in_use(host, port) -> bool:
    """True if something already answers on this port. Checked before anything else at startup:
    on Windows a second copy could otherwise share the port (waitress sets SO_REUSEADDR) and run its
    own backups, auto-archive and serial sync against the same data."""
    probe = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    try:
        with socket.create_connection((probe, int(port)), timeout=0.5):
            return True
    except OSError:
        return False


if __name__ == "__main__":
    if port_in_use(HOST, PORT):
        print(f"[APP] Job Manager is already running on port {PORT} (another window, or another program).")
        print("      Nothing was started. Use the one that is running, or close it first.")
        if os.environ.get("JM_NO_BROWSER") != "1":
            __import__("webbrowser").open(f"http://localhost:{PORT}")
        sys.exit(1)
    setup_log()
    log.info("starting Job Manager %s, page build %s", APP_VERSION, BUILD)
    validate_and_prepare()
    init_db()
    serial_startup()
    init_backup_state()
    start_backup_scheduler()
    start_template_sync()
    start_serial_keeper()
    start_auto_archive()
    start_folder_watch()
    start_share_watch()
    start_integrity_watch()
    start_print_watch()
    proofing.start()
    print(f"[APP] Job Manager {APP_VERSION} is running.")
    print(f"      On this PC open:   http://localhost:{PORT}")
    for ip in _lan_ips():
        print(f"      From other PCs:    http://{ip}:{PORT}")
    print("      (Leave this window open. Closing it stops the app for everyone.)")
    if os.environ.get("JM_NO_BROWSER") != "1":
        def open_when_ready():
            """Open the browser once the server answers (up to a minute), not after a fixed delay,
            so a slow start never shows a "can't connect" page."""
            import urllib.request
            for _ in range(120):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/stamp", timeout=2):
                        break
                except Exception:
                    time.sleep(0.5)
            __import__("webbrowser").open(f"http://localhost:{PORT}")
        threading.Thread(target=open_when_ready, daemon=True, name="open-browser").start()
    try:
        from waitress import serve        # multi-user production server (pure Python, Windows-friendly)
        print("[APP] Serving with waitress")
        serve(app, host=HOST, port=PORT, threads=16)
    except ImportError:
        print("[APP] waitress not installed; using Flask's built-in server")
        app.run(host=HOST, port=PORT, debug=False, threaded=True)
