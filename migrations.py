"""
migrations.py — the database knows its own version
==================================================
The version is SQLite's built-in user_version number. It lives in the file itself,
so it travels with every backup and restore.

At startup run() reads the version and applies only the steps above it, in order,
each exactly once:

  * each step and its version number are saved together or not at all, so a step
    that fails leaves the database exactly as it was;
  * before a step changes a database that already holds data, a copy is saved next
    to it as  local.db.before-v<N>-<date>-<time>;
  * a database with a higher version than this app knows is refused, so an old copy
    of the app can never be run against a newer database.

Adding a change to the database
    Write a function that takes the connection and makes the change, then add it to
    the END of MIGRATIONS with the next number. Never edit or reorder a step that
    has shipped: a database that already ran it will not run it again.
"""
import sqlite3
from datetime import datetime

import serial_store as SS
import sheet_serials as SHEETS_SEQ


class SchemaError(Exception):
    """The database can't be brought to the version this app needs."""


def _add_col(conn, table, col, ddl):
    if col not in [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _v1_baseline(conn):
    """Everything the app built up before versions existed. Databases from that time are at
    unknown points along the way, so this step checks for each table and column and adds what
    is missing. It is the only step written that way; later steps just make their change.
    Tables left over from the old app (inventory, audits, rewind, rules) are left alone."""
    conn.execute("CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT OR IGNORE INTO _meta VALUES ('created_at', datetime('now'))")
    conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_number TEXT NOT NULL UNIQUE, job_ticket TEXT,
        title TEXT NOT NULL, customer TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'Pending', priority TEXT NOT NULL DEFAULT 'Medium',
        due_date TEXT, notes TEXT, upc TEXT, quantity INTEGER, lpr INTEGER,
        qty_db INTEGER, start_serial INTEGER, overage_pct REAL NOT NULL DEFAULT 0,
        label_size TEXT, inlay_type TEXT, part_number TEXT, po_number TEXT,
        folder_path TEXT, archived_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS job_rolls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        roll_number INTEGER NOT NULL, quantity INTEGER NOT NULL,
        serial_start INTEGER, serial_end INTEGER, epc_start TEXT, epc_end TEXT,
        status TEXT NOT NULL DEFAULT 'pending', completed_at TEXT, tag_printed_at TEXT,
        notes TEXT, created_at TEXT NOT NULL, UNIQUE(job_id, roll_number))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS btw_templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, file_path TEXT NOT NULL,
        description TEXT, notes TEXT, thumbnail_path TEXT,
        active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS job_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        roll_number INTEGER, author TEXT, body TEXT NOT NULL,
        important INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_notes_job ON job_notes(job_id)")
    SS.init_additions_schema(conn)               # data added to a job after it was made
    conn.execute("""CREATE TABLE IF NOT EXISTS activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, job_id INTEGER,
        kind TEXT NOT NULL, actor TEXT, message TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_activity_job ON activity(job_id)")

    _add_col(conn, "jobs", "archived_at", "archived_at TEXT")
    _add_col(conn, "jobs", "template_path", "template_path TEXT")
    _add_col(conn, "jobs", "job_type", "job_type TEXT NOT NULL DEFAULT 'standard'")
    _add_col(conn, "jobs", "priority_rank", "priority_rank INTEGER")
    _add_col(conn, "jobs", "created_by", "created_by TEXT")
    _add_col(conn, "jobs", "completed_at", "completed_at TEXT")
    _add_col(conn, "jobs", "data_sheet", "data_sheet TEXT")      # GO2 / CDO data sheet kind on special jobs
    SS.init_schema(conn)
    SHEETS_SEQ.init_schema(conn)
    _add_col(conn, "job_rolls", "completed_by", "completed_by TEXT")
    _add_col(conn, "job_rolls", "started_at", "started_at TEXT")
    _add_col(conn, "job_rolls", "started_by", "started_by TEXT")
    _add_col(conn, "job_rolls", "paused_at", "paused_at TEXT")
    _add_col(conn, "job_rolls", "paused_by", "paused_by TEXT")
    _add_col(conn, "job_rolls", "printer", "printer TEXT")
    _add_col(conn, "btw_templates", "thumbnail_path", "thumbnail_path TEXT")
    _add_col(conn, "btw_templates", "file_size", "file_size INTEGER")
    _add_col(conn, "btw_templates", "file_mtime", "file_mtime TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_jobs_ticket ON jobs(job_ticket)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_jobs_archived ON jobs(archived_at)")


# (version, what it does, function). Add new steps at the end; never change one that has shipped.
MIGRATIONS = [
    (1, "tables and columns as of Job Manager 3.3", _v1_baseline),
]
LATEST = MIGRATIONS[-1][0]


def version(conn) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _has_data(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone() is not None


def _safety_copy(conn, db_path, target) -> str:
    dest = f"{db_path}.before-v{target}-{datetime.now():%Y%m%d-%H%M%S}"
    out = sqlite3.connect(dest)
    try:
        conn.backup(out)
    finally:
        out.close()
    return dest


def run(conn, db_path=None, after=None, log=print) -> list:
    """Bring the database up to LATEST. Returns the versions applied (empty when up to date).
    after: {version: function(conn)} run inside that step, after its changes and before the version
    is saved (the app uses it for the one-time data repairs that go with version 1)."""
    steps = sorted(MIGRATIONS)
    if [v for v, _, _ in steps] != list(range(1, len(steps) + 1)) or steps[-1][0] != LATEST:
        raise SchemaError("migrations.MIGRATIONS must be numbered 1, 2, 3... with no gaps.")
    have = version(conn)
    if have > LATEST:
        raise SchemaError(
            f"This database is version {have}, but this copy of Job Manager only knows up to version {LATEST}. "
            "It was last used by a newer copy of the app. Start the newer copy; nothing was changed.")
    applied = []
    for v, what, fn in steps:
        if v <= have:
            continue
        if db_path and v > 1 and _has_data(conn):
            log(f"  [DB]   safety copy before version {v}: {_safety_copy(conn, db_path, v)}")
        try:
            conn.execute("BEGIN IMMEDIATE")
            fn(conn)
            if after and v in after:
                after[v](conn)
            conn.execute(f"PRAGMA user_version={int(v)}")
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise SchemaError(f"Updating the database to version {v} ({what}) failed: {e}. "
                              f"Nothing was changed; the database is still version {version(conn)}.") from e
        applied.append(v)
        log(f"  [DB]   updated to version {v}: {what}")
    return applied
