"""The app never creates a database by itself. A missing database stops it with a clear message,
so a copy of the app started from the wrong folder can't quietly begin a second, empty one.
A new install is made on purpose:  python app.py --create-database  (new_install.bat)."""
import os
import subprocess
import sys

import pytest


def _remove_db(jm):
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(jm.config.DB_PATH + ext):
            os.remove(jm.config.DB_PATH + ext)


def _run(jm, *args):
    env = {**os.environ, "JM_NO_BROWSER": "1", "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, "app.py", *args], cwd=jm.root, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)


def test_missing_database_is_refused_and_nothing_is_created(jm):
    _remove_db(jm)
    with pytest.raises(jm.migrations.DatabaseMissing, match="No database"):
        jm.app.init_db()
    assert not os.path.exists(jm.config.DB_PATH)


def test_empty_file_is_not_a_database(jm):
    _remove_db(jm)
    open(jm.config.DB_PATH, "w").close()
    with pytest.raises(jm.migrations.DatabaseMissing):
        jm.app.init_db()
    assert os.path.getsize(jm.config.DB_PATH) == 0


def test_some_other_sqlite_file_is_not_adopted(jm):
    """A file with no jobs table is not a Job Manager database; it is left exactly as it is."""
    import sqlite3
    _remove_db(jm)
    c = sqlite3.connect(jm.config.DB_PATH)
    c.execute("CREATE TABLE other (x)"); c.commit(); c.close()
    with pytest.raises(jm.migrations.DatabaseMissing):
        jm.app.init_db()
    c = sqlite3.connect(jm.config.DB_PATH)
    try:
        assert [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")] == ["other"]
    finally:
        c.close()


def test_existing_database_starts_normally(jm):
    jm.job(1)
    jm.app.init_db()
    with jm.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_create_makes_a_new_database_only_when_asked(jm):
    _remove_db(jm)
    jm.app.init_db(create=True)
    with jm.db.connect() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == jm.migrations.LATEST


def test_starting_the_app_without_a_database_stops_with_instructions(jm):
    _remove_db(jm)
    r = _run(jm)
    assert r.returncode == 1
    assert "[STOP]" in r.stdout and "No database" in r.stdout
    assert "new_install.bat" in r.stdout and "RESTORE.md" in r.stdout
    assert not os.path.exists(jm.config.DB_PATH)


def test_create_database_command_makes_one_and_exits(jm):
    _remove_db(jm)
    r = _run(jm, "--create-database")
    assert r.returncode == 0, r.stdout + r.stderr
    assert os.path.getsize(jm.config.DB_PATH) > 0
    assert "run.bat" in r.stdout


def test_create_database_command_never_touches_an_existing_database(jm):
    jm.job(1)
    r = _run(jm, "--create-database")
    assert r.returncode == 1 and "already" in r.stdout
    with jm.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
