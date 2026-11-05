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
from html import escape
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request, send_file, abort, Response

from config import (DB_PATH, Z_DRIVE_BASE, Z_IS_DEFAULT, SERIAL_REGISTRY_DIR, THUMBNAIL_DIR, CONFIG_INI,
                    HOST, PORT, SYSTEM, validate_and_prepare)
from serial_registry import (allocate_serials, peek_next_start, get_upc_history,
                             SERIAL_START_DEFAULT)
from generators import (validate_upc, production_qty, roll_plan, roll_plan_plain, db_file_split,
                        generate_db_files, generate_go2_files, generate_cdo_files, DATA_SHEETS, GO2_SHEET, CDO_SHEET,
                        sheet_customer, roll_plan_seq, read_go2_epcs, go2_file_names, sheet_size_error, generate_qc_sheet, qc_html, _safe_name, safe_folder_name)
import serial_registry as R
import serial_store as SS
import sheet_serials as SHEETS_SEQ
import backup
from generators import decode_epc
from checklist_generator import generate_checklist_html
from lists import CUSTOMERS, LABEL_SIZES

APP_VERSION = "3.3"
DEFAULT_TEMPLATE_DIR = r"Z:\3 Encoding and Printing Files\Master Templates"
STATUSES    = ["Pending", "In Progress", "On Hold", "Complete", "Cancelled"]
JUNK_PO     = {"0", "00", "000", "na", "n/a", "none", "-", "--", "tbd", "x"}
EDITABLE    = ["title", "job_ticket", "customer", "status", "due_date", "notes",
               "inlay_type", "part_number", "po_number", "label_size"]

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 250 * 1024 * 1024   # uploads into job folders


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
    SS.init_additions_schema(cur)               # data added to a job after it was made
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
    # Jobs already marked Complete before this column existed: use when they were set to Complete.
    cur.execute("""UPDATE jobs SET completed_at = COALESCE(
                       (SELECT MAX(ts) FROM activity a WHERE a.job_id = jobs.id AND a.kind = 'status'
                          AND a.message LIKE '%set to Complete'), updated_at)
                   WHERE status = 'Complete' AND completed_at IS NULL""")
    cur.execute("UPDATE jobs SET job_type='special' WHERE (upc IS NULL OR upc='') AND job_type='standard'")
    SS.init_schema(cur)
    SHEETS_SEQ.init_schema(cur)
    add_col("job_rolls", "completed_by", "completed_by TEXT")
    add_col("job_rolls", "started_at", "started_at TEXT")
    add_col("job_rolls", "started_by", "started_by TEXT")
    add_col("job_rolls", "paused_at", "paused_at TEXT")
    add_col("job_rolls", "paused_by", "paused_by TEXT")
    add_col("job_rolls", "printer", "printer TEXT")
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


def get_settings() -> dict:
    conn = get_db()
    s = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM app_settings")}
    conn.close()
    return {
        "btw_template_dir":      s.get("btw_template_dir") or DEFAULT_TEMPLATE_DIR,
        "default_overage_pct":   s.get("default_overage_pct", "0"),
        "backup_dir":            s.get("backup_dir", ""),
        "backup_interval_hours": int(s.get("backup_interval_hours", "6") or 6),
        "backup_auto":           s.get("backup_auto", "1") == "1",
        "backup_keep":           int(s.get("backup_keep", "60") or 60),
        "rewind_base_url":       s.get("rewind_base_url", ""),
        "printers":              s.get("printers", DEFAULT_PRINTERS),
        "auto_archive_hours":    max(0, min(8760, int(s.get("auto_archive_hours", "32") or 0))),
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
    # How the serials are chosen. The form always says; callers that don't (batch, scripts) get the
    # old behaviour: automatic unless a start serial is given.
    serial_mode = (g("serial_mode") or "").lower() or None
    if serial_mode not in (None, "auto", "manual"):
        return {"error": "Serial mode must be auto or manual"}, 400
    if upc and serial_mode == "manual" and not start_override:
        return {"error": "You chose to specify the start serial. Enter it, or choose Automatic."}, 400
    if upc and serial_mode == "auto" and start_override:
        return {"error": "Automatic serials were chosen, but a start serial was also given. Remove one."}, 400
    manual_start = bool(upc and start_override)

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
        if start_override:
            # A start the person typed may sit anywhere, as long as none of its range was used before.
            rc = SS.range_conflicts(upc, start_override, total_qty)
            if not rc["ok"]:
                return {"error": str(R.SerialOverlap(rc["start"], rc["end"], rc["conflicts"])),
                        "conflicts": rc["conflicts"], "next_start": nxt}, 400
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
        if manual_start:
            note += f"; start serial specified by {created_by or 'operator'}"
        try:
            alloc = SS.reserve(upc, job_number, total_qty, note, min_start=None if manual_start else start_override,
                               exact_start=start_override if manual_start else None,
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
                     + (f" — {total_qty:,} labels in {len(rolls)} rolls" if rolls else "")
                     + (f"; serials {alloc['start']:,}–{alloc['end']:,} ({'specified by user' if manual_start else 'automatic'})"
                        if alloc else ""), created_by)
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


def job_additions(conn, job_id) -> list:
    return [dict(r) for r in conn.execute("SELECT * FROM job_additions WHERE job_id=? ORDER BY seq", (job_id,))]


def job_total_qty(conn, job: dict) -> int:
    """Labels to encode for the job: the original order (with overage) plus every addition."""
    base = production_qty(job.get("quantity") or 0, job.get("overage_pct") or 0) if job.get("quantity") else 0
    extra = conn.execute("SELECT COALESCE(SUM(quantity),0) FROM job_additions WHERE job_id=?", (job["id"],)).fetchone()[0]
    return base + int(extra)


def job_serial_ranges(conn, job: dict) -> list:
    """Serial ranges the job holds: the original block, then one per addition (not necessarily adjacent)."""
    out = []
    if job.get("start_serial") and job.get("quantity"):
        n = production_qty(job["quantity"], job.get("overage_pct") or 0)
        out.append({"start": int(job["start_serial"]), "end": int(job["start_serial"]) + n - 1, "label": "Original"})
    for a in job_additions(conn, job["id"]):
        if a["serial_start"]:
            out.append({"start": a["serial_start"], "end": a["serial_end"], "label": f"Added #{a['seq']}"})
    return out


# ══════════════════════════════════════════════════════════════════════════════
# Adding data to a job that already exists
# ══════════════════════════════════════════════════════════════════════════════
MAX_ADDITION = 1_000_000
_add_locks, _add_locks_guard = {}, threading.Lock()


def _add_lock(job_id):
    with _add_locks_guard:
        return _add_locks.setdefault(job_id, threading.Lock())


def add_data(job_id: int, data: dict) -> tuple[dict, int]:
    """Add `quantity` more labels to an existing job (standard, CDO or GO2).

    The job's own quantity / start serial are never changed. The extra labels get a new
    serial range (not necessarily next to the original), new rolls that continue the job's
    numbering, and new data files named _ADD<n>. Serials, once taken, are never released.
    data: quantity, reason, added_by, preview (True = report what would happen, reserve nothing),
    offline_start / offline_confirm (registry unreachable, same as a new job)."""
    data = data or {}
    try:
        qty = _int(data.get("quantity"), "Quantity", required=True)
    except ValueError as e:
        return {"error": str(e)}, 400
    if qty > MAX_ADDITION:
        return {"error": f"Add at most {MAX_ADDITION:,} labels at a time."}, 400
    reason = (str(data.get("reason") or "")).strip()[:200] or None
    who = ((str(data.get("added_by") or "")).strip() or actor() or "")[:40] or None
    preview = bool(data.get("preview"))
    try:
        offline_start = _int(data.get("offline_start"), "Start serial") if data.get("offline_confirm") else None
    except ValueError as e:
        return {"error": str(e)}, 400

    with _add_lock(job_id):
        conn = get_db()
        job = _load_job(conn, job_id)
        if not job:
            conn.close(); return {"error": "Job not found"}, 404
        if job["archived_at"]:
            conn.close(); return {"error": "This job is archived. Move it back to jobs before adding data."}, 409
        kind = "standard" if job["upc"] else job["data_sheet"] if job["data_sheet"] in DATA_SHEETS else None
        if not kind:
            conn.close(); return {"error": "This job has no encoded data (no UPC, no data sheet), so there is nothing to add to."}, 400
        folder = job["folder_path"]
        if not folder or not os.path.isdir(folder):
            conn.close()
            return {"error": "The job folder isn't reachable from the host PC. Nothing was added."}, 503
        seq = (conn.execute("SELECT COALESCE(MAX(seq),0) FROM job_additions WHERE job_id=?", (job_id,)).fetchone()[0]) + 1
        first_roll = (conn.execute("SELECT COALESCE(MAX(roll_number),0) FROM job_rolls WHERE job_id=?", (job_id,)).fetchone()[0]) + 1
        total_before = job_total_qty(conn, job)
        lpr, qty_db, tag = job["lpr"] or 0, job["qty_db"], f"_ADD{seq}"
        label = f"{job['job_ticket'] or job['job_number']}{tag}"
        conn.close()
        n_rolls = -(-qty // lpr) if lpr > 0 else 0
        n_files = len(db_file_split(qty, qty_db))
        suffix = lambda i: f"_DB{i}" if n_files > 1 else ""

        if preview:
            out = {"preview": True, "quantity": qty, "new_total": total_before + qty, "seq": seq,
                   "first_roll": first_roll if n_rolls else None,
                   "last_roll": first_roll + n_rolls - 1 if n_rolls else None, "rolls": n_rolls}
            if kind == "standard":
                nxt = SS.next_start(job["upc"])
                out.update(serial_start=nxt, serial_end=nxt + qty - 1,
                           files=[f"{job['upc']}{tag}{suffix(i)}.xlsx" for i in range(1, n_files + 1)])
                if nxt + qty - 1 > SS.MAX_SERIAL:
                    return {"error": "That range runs past the largest serial an EPC can hold."}, 400
            elif kind == CDO_SHEET:
                nxt = SHEETS_SEQ.next_start(CDO_SHEET)
                out.update(serial_start=nxt, serial_end=nxt + qty - 1, files=go2_file_names(label, n_files))
            else:
                out.update(serial_start=None, serial_end=None, files=go2_file_names(label, n_files))
            return out, 200

        # ── take the serials ──────────────────────────────────────────────────
        alloc, seq_alloc, warnings = None, None, []
        start = None
        try:
            if kind == "standard":
                alloc = SS.reserve(job["upc"], job["job_number"], qty, f"addition #{seq}", who=who, offline_start=offline_start)
                start = alloc["start"]
                if alloc["offline"]:
                    warnings.append("Added while the serial registry was offline. The range is saved in the app "
                                    "database and will be written to the registry when it is back.")
            elif kind == CDO_SHEET:
                seq_alloc = SHEETS_SEQ.reserve(CDO_SHEET, job["job_number"], qty, who=who, note=f"addition #{seq}")
                start = seq_alloc["start"]
        except SS.SerialsOffline as e:
            return {"error": f"{e} Nothing was added.", "serial_offline": True, **e.info}, 423
        except (ValueError, SHEETS_SEQ.SheetSerialError) as e:
            return {"error": str(e)}, 400
        except Exception as e:
            return {"error": f"Serial allocation failed, nothing was added: {e}"}, 500
        end = start + qty - 1 if start else None

        # ── rolls and files ───────────────────────────────────────────────────
        if not lpr:
            rolls = []
        elif kind == "standard":
            rolls = roll_plan(job["upc"], start, qty, lpr, first_roll)
        elif kind == CDO_SHEET:
            rolls = roll_plan_seq(start, qty, lpr, first_roll)
        else:
            rolls = roll_plan_plain(qty, lpr, first_roll)
        try:
            if kind == "standard":
                gen = generate_db_files(folder, job["upc"], start, qty, qty_db, tag=tag)
            elif kind == CDO_SHEET:
                gen = generate_cdo_files(folder, label, start, qty, qty_db)
            else:
                gen = generate_go2_files(folder, label, qty, qty_db, exclude=read_go2_epcs(folder))
        except Exception as e:
            if seq_alloc:
                SHEETS_SEQ.void(seq_alloc, f"addition data not written for {job['job_number']}")
            return {"error": f"The data files could not be written, so nothing was added: {e}"
                             + (f" Serials {start:,}–{end:,} stay recorded as used, so they are never handed out again." if start else "")}, 500
        names = [os.path.basename(f) for f in gen["files"]]

        # ── save ──────────────────────────────────────────────────────────────
        conn = get_db()
        try:
            ts = now_str()
            cur = conn.execute(
                "INSERT INTO job_additions (job_id, seq, quantity, serial_start, serial_end, first_roll, last_roll,"
                " files, reason, added_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, seq, qty, start, end, rolls[0]["roll_number"] if rolls else 0,
                 rolls[-1]["roll_number"] if rolls else 0, json.dumps(names), reason, who, ts))
            add_id = cur.lastrowid
            _insert_rolls(conn, job_id, rolls, addition_id=add_id)
            reopened = job["status"] == "Complete"
            if reopened:
                conn.execute("UPDATE jobs SET status='In Progress', completed_at=NULL WHERE id=?", (job_id,))
            conn.execute("UPDATE jobs SET updated_at=? WHERE id=?", (ts, job_id))
            log_activity(conn, "data_added", job_id,
                         f"{job_label(job)}: added {qty:,} labels"
                         + (f", serials {start:,}–{end:,}" if start else "")
                         + (f", rolls {rolls[0]['roll_number']}–{rolls[-1]['roll_number']}" if rolls else "")
                         + (" (job reopened)" if reopened else "")
                         + (f" — {reason}" if reason else ""), who)
            conn.commit()
            addition = dict(conn.execute("SELECT * FROM job_additions WHERE id=?", (add_id,)).fetchone())
            saved = _load_job(conn, job_id)
        except Exception as e:
            conn.rollback(); conn.close()
            for f in gen["files"]:
                try: os.remove(f)
                except OSError: pass
            if seq_alloc:
                SHEETS_SEQ.void(seq_alloc, f"addition not saved for {job['job_number']}")
            return {"error": f"The addition could not be saved, so nothing was added: {e}"
                             + (f" Serials {start:,}–{end:,} stay recorded as used." if start else "")}, 500
        conn.close()
        try:
            warnings += [f"Checklist / QC sheet not rewritten: {m}" for m in regenerate_sheets(job_id)]
        except Exception as e:
            warnings.append(f"Checklist / QC sheet not rewritten: {e}")
        return {"success": True, "addition": addition, "job": saved, "rolls_added": len(rolls),
                "files": names, "warnings": warnings}, 201


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


def _insert_rolls(conn, job_id, rolls, addition_id=None):
    ts = now_str()
    for r in rolls:
        conn.execute("""INSERT OR IGNORE INTO job_rolls (job_id, roll_number, quantity,
            serial_start, serial_end, epc_start, epc_end, status, created_at, addition_id)
            VALUES (?,?,?,?,?,?,?,'pending',?,?)""",
            (job_id, r["roll_number"], r["quantity"], r["serial_start"], r["serial_end"],
             r["epc_start"], r["epc_end"], ts, addition_id))


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
    return render_template("index.html", version=APP_VERSION)


@app.route("/favicon.ico")
def favicon():
    return Response(status=204)


_folder_cache = {"t": 0, "names": []}


def _customer_folders():
    if time.time() - _folder_cache["t"] > 300:
        try:
            _folder_cache["names"] = sorted(
                e.name for e in os.scandir(Z_DRIVE_BASE) if e.is_dir() and not e.name.startswith("."))
        except OSError:
            _folder_cache["names"] = []
        _folder_cache["t"] = time.time()
    return _folder_cache["names"]


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
        "z_reachable": os.path.isdir(Z_DRIVE_BASE), "z_shared": not Z_IS_DEFAULT,
        "customers": _dedupe(CUSTOMERS + hist_c + _customer_folders()),
        "label_sizes": _dedupe(LABEL_SIZES + hist_s),
        "inlays": _dedupe(inlays), "statuses": STATUSES,
        "names": _recent_names(), "serials": {**SS.health(), "registry_gaps": _registry_gap_count["n"]},
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
    h = {**SS.health(), "registry_gaps": _registry_gap_count["n"]}
    return jsonify({"stamp": f"{r['c']}|{r['m']}|{r['a']}|{r['p']}",
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
    sql  = f"SELECT j.*, {ROLL_SUMMARY} FROM jobs j WHERE "
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
    additions = job_additions(conn, job_id)
    ranges = job_serial_ranges(conn, job)
    total = job_total_qty(conn, job) if job["quantity"] else None
    conn.close()
    return jsonify({"job": job, "rolls": rolls, "notes": notes, "files": _list_files(job["folder_path"]),
                    "total_qty": total, "additions": additions, "serial_ranges": ranges,
                    "end_serial": ranges[-1]["end"] if ranges else None,
                    "folder_exists": bool(job["folder_path"] and os.path.isdir(job["folder_path"]))})


@app.route("/api/jobs/<int:job_id>/additions", methods=["POST"])
def api_job_additions(job_id):
    """Add more labels to a job that already exists (see add_data)."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return err("Send JSON with a quantity.")
    body, code = add_data(job_id, data)
    return jsonify(body), code


@app.route("/api/jobs/<int:job_id>", methods=["PATCH"])
def api_job_edit(job_id):
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
    if not sets:
        return err("Nothing to update")
    sets.append("updated_at=?"); vals += [now_str(), job_id]
    conn = get_db()
    before = _load_job(conn, job_id)
    who = (data.get("operator") or "").strip()[:40] or actor()
    if before:
        new_ticket = (data["job_ticket"] if "job_ticket" in data else before.get("job_ticket")) or ""
        new_status = data.get("status", before["status"])
        changed = ("job_ticket" in data and new_ticket.strip().lower() != (before.get("job_ticket") or "").strip().lower()) \
                  or (before["status"] == "Cancelled" and new_status != "Cancelled")
        if changed and new_status != "Cancelled":
            same = _same_ticket(conn, new_ticket, exclude_id=job_id)
            if same:
                conn.close()
                return err(f"Ticket {new_ticket.strip()} already belongs to {', '.join(j['job_number'] for j in same)}. "
                           "Two jobs can't share a ticket.", 409)
    cur = conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", vals)
    if before:
        if "status" in data and data["status"] != before["status"]:
            log_activity(conn, "status", job_id, f"{job_label(before)} set to {data['status']}", who)
            conn.execute("UPDATE jobs SET completed_at=? WHERE id=?",
                         (now_str() if data["status"] == "Complete" else None, job_id))
        changed = [f.replace("_", " ") for f in EDITABLE if f != "status" and f in data
                   and (data[f] or None) != (before.get(f) or None)]
        if changed:
            log_activity(conn, "edited", job_id, f"{job_label(before)}: changed {', '.join(changed)}")
    conn.commit()
    job = _load_job(conn, job_id); conn.close()
    if not cur.rowcount:
        return err("Job not found", 404)
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
    """Roll states: pending (not started) → running (in progress) ⇄ paused → complete.
    Any state can be reset to pending. An optional note (e.g. why it was paused) is
    saved on the roll."""
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
        if not row or row["status"] == status:
            continue
        before = row["status"]
        if status == "complete":
            conn.execute("""UPDATE job_rolls SET status='complete', completed_at=?, completed_by=?,
                started_at=COALESCE(started_at, ?), started_by=COALESCE(started_by, ?), paused_at=NULL, paused_by=NULL
                WHERE job_id=? AND roll_number=?""", (ts, who, ts, who, job_id, n))
            words.append("done")
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
    conn.commit(); conn.close()
    return jsonify({"success": True, "changed": changed})


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
               j.label_size, j.inlay_type, j.upc, j.created_at, j.notes, {ROLL_SUMMARY},
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
    return render_template("rewind.html", version=APP_VERSION)


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
    j.updated_at, j.priority_rank"""


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
            "started_at", "started_by", "paused_at", "paused_by", "completed_at", "completed_by")
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


@app.route("/api/rewind/lookup", methods=["POST"])
def api_rewind_lookup():
    data = request.get_json(silent=True) or {}
    jid = data.get("job_id")
    conn = get_db()
    try:
        return jsonify(_lookup(conn, str(data.get("raw") or ""), int(jid) if str(jid or "").isdigit() else None))
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


def regenerate_sheets(job_id) -> list:
    """Rewrite the checklist and QC sheet from the job as it is now, additions included.
    Returns a list of error messages (empty when both were written)."""
    conn = get_db()
    try:
        job = _load_job(conn, job_id)
        rolls = ensure_rolls(conn, job)
        job["po_clean"] = clean_po(job["po_number"])
        job["additions"] = job_additions(conn, job_id)
        job["roll_count"] = len(rolls)
    finally:
        conn.close()
    out = [generate_checklist_html(job["folder_path"], job)]
    if rolls:
        out.append(generate_qc_sheet(job["folder_path"], job, rolls))
    return [r["error"] for r in out if not r.get("success")]


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
    rebuilt = False
    data_dir = os.path.join(folder, "Data")
    # The original encoding database: any data file that is not an _ADD<n> file.
    has_db = os.path.isdir(data_dir) and any(f.lower().endswith((".xlsx", ".csv")) and "_ADD" not in f
                                             for f in os.listdir(data_dir))
    if job["upc"] and job["start_serial"] and job["quantity"] and not has_db:
        try:     # same serials as recorded for this job; never allocates new ones
            generate_db_files(folder, job["upc"], job["start_serial"],
                              production_qty(job["quantity"], job["overage_pct"] or 0), job["qty_db"])
            rebuilt = True
        except Exception as e:
            return err(f"Encoding database not rebuilt: {e}", 500)
    errors = regenerate_sheets(job_id)
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


@app.route("/api/upc/<upc>/range")
def api_upc_range(upc):
    """Can this UPC use `qty` serials from `start`? Lists the earlier ranges it would overlap."""
    v = validate_upc(upc)
    if not v["valid"]:
        return jsonify(v), 400
    try:
        start, qty = _int(request.args.get("start"), "Start serial", required=True),                      _int(request.args.get("qty"), "Quantity", required=True)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(SS.range_conflicts(upc, start, qty))


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
    data = request.get_json(silent=True) or {}
    allowed = {"btw_template_dir", "default_overage_pct", "backup_dir",
               "backup_interval_hours", "backup_auto", "backup_keep", "rewind_base_url", "printers",
               "auto_archive_hours"}
    conn = get_db()
    for k, v in data.items():
        if k in allowed:
            if k == "backup_auto":
                v = "1" if v in (True, "1", 1, "true") else "0"
            if k == "printers":
                v = clean_printers(v)
            if k == "auto_archive_hours":
                try:
                    v = max(0, min(8760, int(float(v))))
                except (TypeError, ValueError):
                    v = 32
            conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (k, str(v).strip()))
    conn.commit(); conn.close()
    SS.set_journal_dir(get_settings()["backup_dir"])
    start_backup_scheduler()
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
                                  app_version=APP_VERSION, serial_lock=SS._lock)
            check = backup.verify_backup(part)
            check["file"] = name
            if check["ok"]:
                os.replace(part, os.path.join(bdir, name)); part = None
                _backup_state.update(last_file=name, verify=check, last_ok_at=time.time(), last_error=None)
                for old in _list_backups(bdir)[:-max(s["backup_keep"], 5)]:
                    try: os.remove(os.path.join(bdir, old))
                    except OSError: pass
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
    h = SS.health()
    try:
        reg_files = sum(1 for f in os.listdir(SERIAL_REGISTRY_DIR) if f.endswith(".txt"))
    except OSError:
        reg_files = 0
    try:
        previews = len(os.listdir(THUMBNAIL_DIR))
    except OSError:
        previews = 0
    db, jp = _file_info(DB_PATH), SS.journal_path()
    jinfo = _file_info(jp) if jp else {"exists": False}
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
        {"key": "folders", "name": "Job folders", "path": Z_DRIVE_BASE, "level": "ok" if (os.path.isdir(Z_DRIVE_BASE) and not Z_IS_DEFAULT) else ("warn" if os.path.isdir(Z_DRIVE_BASE) else "bad"),
         "holds": "Encoding databases, checklists, QC sheets, template copies, proofs",
         "status": ("Shared" if not Z_IS_DEFAULT else "This PC only") if os.path.isdir(Z_DRIVE_BASE) else "Not reachable",
         "detail": "", "backup": "No: back up the share itself"},
        {"key": "templates", "name": "Master Templates", "path": tdir, "level": ok_lv(os.path.isdir(tdir)),
         "holds": "The BarTender templates the app copies from",
         "status": (f"{counts['templates']:,} indexed" if os.path.isdir(tdir) else "Not reachable"),
         "detail": "", "backup": "No: back up the share itself"},
        {"key": "previews", "name": "Template previews", "path": THUMBNAIL_DIR, "level": "ok",
         "holds": "Preview images added on the Templates page", "status": f"{previews:,} images", "detail": "", "backup": "Yes, in every backup"},
        {"key": "config", "name": "config.ini", "path": CONFIG_INI, "level": ok_lv(os.path.isfile(CONFIG_INI)),
         "holds": "Paths and the port", "status": "Found" if os.path.isfile(CONFIG_INI) else "Not found", "detail": "", "backup": "Yes, in every backup"},
    ]
    zips = _list_backups(bdir)
    newest = None
    if zips:
        info = _file_info(os.path.join(bdir, zips[-1]))
        newest = {"file": zips[-1], "modified": info.get("modified"), "size": _fmt_size(info.get("size", 0))}
    age = _backup_age_hours()
    health = backup_health(s)
    backups = {"dir": bdir, "explicit": bool(s["backup_dir"]), "reachable": _dir_ok(bdir), "auto": s["backup_auto"],
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
    return jsonify({**SS.health(), "registry_gaps": _registry_gap_count["n"]})


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
    if request.path.startswith("/api/"):
        return err(f"Server error: {getattr(e, 'original_exception', e)}", 500)
    return e


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


if __name__ == "__main__":
    validate_and_prepare()
    init_db()
    serial_startup()
    init_backup_state()
    start_backup_scheduler()
    start_template_sync()
    start_serial_keeper()
    start_auto_archive()
    print(f"[APP] Job Manager {APP_VERSION} is running.")
    print(f"      On this PC open:   http://localhost:{PORT}")
    for ip in _lan_ips():
        print(f"      From other PCs:    http://{ip}:{PORT}")
    print("      (Leave this window open. Closing it stops the app for everyone.)")
    if os.environ.get("JM_NO_BROWSER") != "1":
        threading.Timer(2.0, lambda: __import__("webbrowser").open(f"http://localhost:{PORT}")).start()
    try:
        from waitress import serve        # multi-user production server (pure Python, Windows-friendly)
        print("[APP] Serving with waitress")
        serve(app, host=HOST, port=PORT, threads=16)
    except ImportError:
        print("[APP] waitress not installed; using Flask's built-in server")
        app.run(host=HOST, port=PORT, debug=False, threaded=True)
