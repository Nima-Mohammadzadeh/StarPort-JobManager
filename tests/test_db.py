"""db.py: the one way to open the app database. A connection opened with db.connect()
is always closed, and an error rolls back whatever was not committed."""
import sqlite3

import pytest


def _closed(conn):
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


def test_connection_is_closed_after_a_normal_exit(jm):
    with jm.db.connect() as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    assert _closed(conn)


def test_connection_is_closed_after_an_early_return(jm):
    seen = []

    def handler():
        with jm.db.connect() as conn:
            seen.append(conn)
            return "early"

    assert handler() == "early"
    assert _closed(seen[0])


def test_error_closes_the_connection_and_rolls_back(jm):
    seen = []
    with pytest.raises(RuntimeError):
        with jm.db.connect() as conn:
            seen.append(conn)
            conn.execute("INSERT INTO app_settings (key, value) VALUES ('probe', 'x')")
            raise RuntimeError("boom")
    assert _closed(seen[0])
    with jm.db.connect() as conn:
        assert conn.execute("SELECT 1 FROM app_settings WHERE key='probe'").fetchone() is None


def test_nothing_is_saved_without_an_explicit_commit(jm):
    with jm.db.connect() as conn:
        conn.execute("INSERT INTO app_settings (key, value) VALUES ('probe', 'x')")
    with jm.db.connect() as conn:
        assert conn.execute("SELECT 1 FROM app_settings WHERE key='probe'").fetchone() is None


def test_committed_work_is_kept(jm):
    with jm.db.connect() as conn:
        conn.execute("INSERT INTO app_settings (key, value) VALUES ('probe', 'x')")
        conn.commit()
    with jm.db.connect() as conn:
        assert conn.execute("SELECT value FROM app_settings WHERE key='probe'").fetchone()[0] == "x"


def test_every_module_gets_the_same_kind_of_connection(jm):
    """Rows by name, foreign keys enforced, WAL: the same for the app and the serial modules."""
    for opener in (jm.db.open, jm.app.get_db):
        conn = opener()
        try:
            assert conn.row_factory is sqlite3.Row
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        finally:
            conn.close()


def test_no_private_copies_of_the_opener_remain(jm):
    assert not hasattr(jm.SS, "_db")
    assert not hasattr(jm.SHEETS, "_db")
