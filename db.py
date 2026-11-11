"""
db.py — the one way to open the app database
============================================
Every module opens local.db through here, so the connection is set up the same
way everywhere (15 s lock wait, rows by column name, WAL, foreign keys enforced).

    with db.connect() as conn:
        conn.execute("UPDATE ...")
        conn.commit()

connect() always closes the connection when the block ends, including on an early
return or an error. An error also rolls back anything that was not committed.
Nothing is committed for you: call conn.commit() when the work should be kept.

open() returns a plain connection for the few places that manage it themselves
(they must close it in a finally block).
"""
import os
import sqlite3
from contextlib import contextmanager

from config import DB_PATH

TIMEOUT_SECONDS = 15


def open():
    conn = sqlite3.connect(DB_PATH, timeout=TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def exists() -> bool:
    """True when DB_PATH holds a Job Manager database (a readable SQLite file with a jobs table).
    Only looks: a missing, empty or foreign file is left exactly as it is."""
    try:
        if os.path.getsize(DB_PATH) == 0:
            return False
    except OSError:
        return False
    conn = sqlite3.connect(DB_PATH, timeout=TIMEOUT_SECONDS)
    try:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone() is not None
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()


@contextmanager
def connect():
    conn = open()
    try:
        yield conn
    except BaseException:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
