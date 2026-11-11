"""migrations.py: the database knows its own version, and each upgrade runs exactly once."""
import glob
import os
import sqlite3

import pytest


def _version(jm):
    with jm.db.connect() as c:
        return c.execute("PRAGMA user_version").fetchone()[0]


def _cols(jm, table):
    with jm.db.connect() as c:
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}


def _tables(jm):
    with jm.db.connect() as c:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _sql(jm, *statements):
    with jm.db.connect() as c:
        for s in statements:
            c.execute(s)
        c.commit()


def test_new_database_ends_at_the_latest_version_with_every_table(jm):
    assert _version(jm) == jm.migrations.LATEST >= 1
    assert {"jobs", "job_rolls", "job_notes", "job_additions", "activity", "app_settings", "btw_templates",
            "serial_allocations", "sheet_serials"} <= _tables(jm)
    assert {"completed_at", "data_sheet", "job_type", "priority_rank", "template_path"} <= _cols(jm, "jobs")
    assert {"printer", "started_by", "paused_by", "addition_id"} <= _cols(jm, "job_rolls")


def test_todays_database_without_a_version_is_stamped_and_nothing_changes(jm):
    jm.job(1); jm.job(2)
    with jm.db.connect() as c:
        before = [tuple(r) for r in c.execute("SELECT * FROM jobs ORDER BY id")]
        rolls = c.execute("SELECT COUNT(*) FROM job_rolls").fetchone()[0]
    _sql(jm, "PRAGMA user_version=0")
    jm.app.init_db()
    assert _version(jm) == jm.migrations.LATEST
    with jm.db.connect() as c:
        assert [tuple(r) for r in c.execute("SELECT * FROM jobs ORDER BY id")] == before
        assert c.execute("SELECT COUNT(*) FROM job_rolls").fetchone()[0] == rolls


def test_old_shape_database_is_upgraded_and_repaired(jm):
    job = jm.job(1)["job"]
    _sql(jm,
         "DROP INDEX IF EXISTS ix_jobs_archived",
         "ALTER TABLE jobs DROP COLUMN completed_at",
         "ALTER TABLE jobs DROP COLUMN data_sheet",
         "ALTER TABLE job_rolls DROP COLUMN printer",
         "UPDATE jobs SET status='Complete'",
         "UPDATE job_rolls SET notes='ribbon wrinkled' WHERE roll_number=1",
         "DELETE FROM job_rolls WHERE roll_number>1",
         "PRAGMA user_version=0")
    _sql(jm, "DELETE FROM job_rolls")                     # an old job with no roll tracker at all
    _sql(jm, "INSERT INTO job_rolls (job_id, roll_number, quantity, notes, created_at) "
             f"SELECT {job['id']}, 1, 50, 'ribbon wrinkled', '2025-01-01 00:00:00'")
    jm.app.init_db()
    assert _version(jm) == jm.migrations.LATEST
    assert {"completed_at", "data_sheet"} <= _cols(jm, "jobs") and "printer" in _cols(jm, "job_rolls")
    with jm.db.connect() as c:
        assert c.execute("SELECT completed_at FROM jobs").fetchone()[0]          # filled in for a Complete job
        assert [r[0] for r in c.execute("SELECT body FROM job_notes")] == ["ribbon wrinkled"]
        assert c.execute("SELECT notes FROM job_rolls").fetchone()[0] is None


def test_old_job_without_rolls_gets_a_roll_tracker_when_version_1_is_applied(jm):
    jm.job(1)
    _sql(jm, "DELETE FROM job_rolls", "PRAGMA user_version=0")
    jm.app.init_db()
    with jm.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM job_rolls").fetchone()[0] > 0


def test_second_start_runs_nothing(jm):
    """The one-time repairs are one-time: a roll note put back by hand stays where it is."""
    jm.job(1)
    _sql(jm, "UPDATE job_rolls SET notes='left on purpose' WHERE roll_number=1")
    jm.app.init_db()
    jm.app.init_db()
    with jm.db.connect() as c:
        assert c.execute("SELECT notes FROM job_rolls WHERE roll_number=1").fetchone()[0] == "left on purpose"
        assert c.execute("SELECT COUNT(*) FROM job_notes").fetchone()[0] == 0


def test_failing_upgrade_rolls_back_and_keeps_the_old_version(jm, monkeypatch):
    M = jm.migrations

    def bad(conn):
        conn.execute("ALTER TABLE jobs ADD COLUMN half_done TEXT")
        conn.execute("INSERT INTO app_settings (key, value) VALUES ('half', 'done')")
        raise RuntimeError("disk full")

    monkeypatch.setattr(M, "MIGRATIONS", M.MIGRATIONS + [(M.LATEST + 1, "bad step", bad)])
    monkeypatch.setattr(M, "LATEST", M.LATEST + 1)
    with pytest.raises(M.SchemaError, match="disk full"):
        jm.app.init_db()
    assert _version(jm) == M.LATEST - 1
    assert "half_done" not in _cols(jm, "jobs")
    with jm.db.connect() as c:
        assert c.execute("SELECT 1 FROM app_settings WHERE key='half'").fetchone() is None


def test_upgrade_of_an_existing_database_saves_a_safety_copy_first(jm, monkeypatch):
    M = jm.migrations
    jm.job(1)
    monkeypatch.setattr(M, "MIGRATIONS", M.MIGRATIONS + [
        (M.LATEST + 1, "add a column", lambda conn: conn.execute("ALTER TABLE jobs ADD COLUMN extra TEXT"))])
    monkeypatch.setattr(M, "LATEST", M.LATEST + 1)
    jm.app.init_db()
    assert _version(jm) == M.LATEST and "extra" in _cols(jm, "jobs")
    copies = glob.glob(jm.config.DB_PATH + f".before-v{M.LATEST}-*")
    assert len(copies) == 1
    c = sqlite3.connect(copies[0])
    try:
        assert c.execute("PRAGMA user_version").fetchone()[0] == M.LATEST - 1
        assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert "extra" not in {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
    finally:
        c.close()


def test_no_safety_copy_when_there_is_nothing_to_upgrade(jm):
    jm.job(1)
    jm.app.init_db()
    assert glob.glob(jm.config.DB_PATH + ".before-v*") == []


def test_database_newer_than_the_app_refuses_to_start(jm):
    _sql(jm, f"PRAGMA user_version={jm.migrations.LATEST + 5}")
    with pytest.raises(jm.migrations.SchemaError, match="newer"):
        jm.app.init_db()
    assert _version(jm) == jm.migrations.LATEST + 5         # untouched


def test_restored_backup_from_before_versions_upgrades_on_next_start(jm):
    jm.job(1)
    _sql(jm, "ALTER TABLE job_rolls DROP COLUMN printer", "PRAGMA user_version=0")     # a pre-version database
    r = jm.app.make_backup(); assert r["success"], r
    jm.app.init_db(); jm.job(2)                               # the app moves on
    RB = jm.restore(); RB.app_is_running = lambda: False
    RB.apply_restore(os.path.join(jm.backups, r["file"]), ask=lambda p: False)       # take the backup exactly
    assert _version(jm) == 0 and "printer" not in _cols(jm, "job_rolls")
    jm.app.init_db()
    assert _version(jm) == jm.migrations.LATEST and "printer" in _cols(jm, "job_rolls")


def test_jobs_brought_in_by_the_import_tool_are_repaired(jm, tmp_path, monkeypatch):
    """The import runs after the database is already at the latest version, so it does the repairs itself."""
    import importlib
    import sys
    old = tmp_path / "old_app"
    os.makedirs(old / "serial_registry")
    c = sqlite3.connect(old / "local.db")
    c.execute("""CREATE TABLE jobs (id INTEGER PRIMARY KEY, job_number TEXT, job_ticket TEXT, title TEXT, customer TEXT,
                 status TEXT, upc TEXT, quantity INTEGER, lpr INTEGER, start_serial INTEGER, overage_pct REAL,
                 created_at TEXT, updated_at TEXT)""")
    c.execute("INSERT INTO jobs VALUES (1, 'JOB-0901', 'OLD1', 'Old item', 'Acme', 'Complete', ?, 100, 50, 5000, 0, "
              "'2025-01-02 08:00:00', '2025-01-03 09:00:00')", (jm.upc(7),))
    c.execute("INSERT INTO jobs VALUES (2, 'JOB-0902', 'OLD2', 'No UPC item', 'Acme', 'Pending', NULL, NULL, NULL, NULL, 0, "
              "'2025-01-02 08:00:00', '2025-01-03 09:00:00')")
    c.commit(); c.close()
    sys.path.insert(0, jm.root)
    try:
        imp = importlib.import_module("import_old")
    finally:
        sys.path.remove(jm.root)
    monkeypatch.setattr(sys, "argv", ["import_old.py", str(old)])
    imp.main()
    with jm.db.connect() as conn:
        done = dict(conn.execute("SELECT * FROM jobs WHERE job_number='JOB-0901'").fetchone())
        plain = dict(conn.execute("SELECT * FROM jobs WHERE job_number='JOB-0902'").fetchone())
        rolls = conn.execute("SELECT COUNT(*) FROM job_rolls WHERE job_id=?", (done["id"],)).fetchone()[0]
    assert done["completed_at"] == "2025-01-03 09:00:00"
    assert plain["job_type"] == "special"
    assert rolls == 2
    assert _version(jm) == jm.migrations.LATEST
