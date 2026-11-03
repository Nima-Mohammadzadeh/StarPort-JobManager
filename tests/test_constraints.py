"""M1: the database itself refuses data the app would never write: an unknown status, a
quantity of zero, a serial range that ends before it starts, two active jobs on one ticket.
Rows that broke a rule before the rules existed are reported, not deleted, and stay editable."""
import os
import sqlite3

import pytest


def _db(jm):
    c = sqlite3.connect(jm.config.DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _job_id(jm, number):
    c = _db(jm)
    try:
        return c.execute("SELECT id FROM jobs WHERE job_number=?", (number,)).fetchone()[0]
    finally:
        c.close()


INSERT_JOB = ("INSERT INTO jobs (job_number, job_ticket, title, customer, status, created_at, updated_at) "
              "VALUES (?, ?, 'Item', 'Acme', ?, '2026-01-01 00:00:00', '2026-01-01 00:00:00')")


# ── single-value rules ────────────────────────────────────────────────────────
@pytest.mark.parametrize("sql, args", [
    (INSERT_JOB, ("JOB-9001", "X1", "Complet")),                                   # status typo
    ("UPDATE jobs SET status='Done' WHERE job_number='JOB-0001'", ()),
    ("UPDATE jobs SET quantity=0 WHERE job_number='JOB-0001'", ()),
    ("UPDATE jobs SET lpr=-5 WHERE job_number='JOB-0001'", ()),
    ("UPDATE jobs SET job_type='other' WHERE job_number='JOB-0001'", ()),
    ("UPDATE jobs SET overage_pct=150 WHERE job_number='JOB-0001'", ()),
    ("UPDATE jobs SET start_serial=0 WHERE job_number='JOB-0001'", ()),
    ("UPDATE job_rolls SET status='finished' WHERE roll_number=1", ()),
    ("UPDATE job_rolls SET quantity=0 WHERE roll_number=1", ()),
    ("UPDATE job_rolls SET serial_end=serial_start-1 WHERE roll_number=1", ()),
    ("""INSERT INTO serial_allocations (upc, job_number, start, "end", qty, created_at)
        VALUES ('012345678905', 'JOB-X', 500, 499, 1, 'now')""", ()),               # ends before it starts
    ("""INSERT INTO serial_allocations (upc, job_number, start, "end", qty, created_at)
        VALUES ('012345678905', 'JOB-X', 0, 10, 11, 'now')""", ()),                 # serial 0
    ("""INSERT INTO serial_allocations (upc, job_number, start, "end", qty, created_at)
        VALUES ('012345678905', 'JOB-X', 1, 10, -1, 'now')""", ()),
    ("""INSERT INTO serial_allocations (upc, job_number, start, "end", qty, created_at, registry_state)
        VALUES ('012345678905', 'JOB-X', 1, 10, 10, 'now', 7)""", ()),
    ("""INSERT INTO sheet_serials (sheet, job_number, start, "end", qty, created_at)
        VALUES ('cdo_seq', 'JOB-X', 10, 9, 1, 'now')""", ()),
])
def test_database_refuses_values_the_app_never_writes(jm, sql, args):
    jm.job(1)
    c = _db(jm)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(sql, args)
    finally:
        c.close()


def test_values_the_app_does_write_are_accepted(jm):
    """HISTORY lines have qty 0, special jobs have no quantity, old imported rolls say 'verified'."""
    jm.job(1)
    c = _db(jm)
    try:
        c.execute("""INSERT INTO serial_allocations (upc, job_number, start, "end", qty, created_at)
                     VALUES ('012345678905', 'HISTORY', 1, 10, 0, 'now')""")
        c.execute("UPDATE jobs SET quantity=NULL, lpr=NULL, job_type='special' WHERE job_number='JOB-0001'")
        c.execute("UPDATE job_rolls SET status='verified' WHERE roll_number=1")
        for s in ("Pending", "In Progress", "On Hold", "Complete", "Cancelled"):
            c.execute("UPDATE jobs SET status=? WHERE job_number='JOB-0001'", (s,))
    finally:
        c.close()


# ── old rows that break a rule ────────────────────────────────────────────────
def _plant_bad_status(jm):
    """A row written before the rules existed (the trigger is dropped to put it there)."""
    c = _db(jm)
    c.execute("DROP TRIGGER chk_jobs_status_ins")
    c.execute(INSERT_JOB, ("JOB-0500", "OLD-1", "Done"))
    c.commit(); c.close()
    jm.app.migrate_db()                       # nothing to migrate: the trigger stays dropped …
    c = _db(jm)
    jm.app.install_rule_triggers(c.cursor()); c.commit(); c.close()      # … so put it back


def test_rows_from_before_the_rules_are_reported_not_removed(jm):
    jm.job(1)
    _plant_bad_status(jm)
    c = _db(jm)
    try:
        found = jm.app.rule_violations(c)
    finally:
        c.close()
    assert [(v["table"], v["count"]) for v in found] == [("jobs", 1)]
    assert "JOB-0500" in found[0]["examples"]
    assert _job_id(jm, "JOB-0500")


def test_old_bad_row_can_still_have_other_fields_edited(jm):
    """Rules on a column only run when that column is written, so fixing a title doesn't fail."""
    jm.job(1)
    _plant_bad_status(jm)
    jid = _job_id(jm, "JOB-0500")
    r = jm.app.app.test_client().patch(f"/api/jobs/{jid}", json={"title": "Renamed"})
    assert r.status_code == 200, r.get_json()


def test_data_panel_lists_rule_breaking_rows(jm):
    jm.job(1)
    _plant_bad_status(jm)
    d = jm.app.app.test_client().get("/api/data/status").get_json()
    assert any("JOB-0500" in w["text"] for w in d["warnings"])


# ── one active job per ticket, enforced by the database ───────────────────────
def test_second_active_job_on_a_ticket_is_refused_by_the_database(jm):
    jm.job(1, ticket="T-77")
    c = _db(jm)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(INSERT_JOB, ("JOB-9002", " t-77 ", "Pending"))      # case and spaces don't matter
        c.execute(INSERT_JOB, ("JOB-9003", "T-77", "Cancelled"))         # a cancelled one may share it
        c.execute(INSERT_JOB, ("JOB-9004", "", "Pending"))                # no ticket: not checked
        c.execute(INSERT_JOB, ("JOB-9005", "", "Pending"))
    finally:
        c.close()


def test_simultaneous_duplicate_ticket_gets_a_clear_refusal_not_a_server_error(jm, monkeypatch):
    """If the app-level check is ever bypassed (a race, a future bug), the database catches it and
    the person gets the same 409 'ticket already has a job' answer."""
    jm.job(1, ticket="T-88")
    monkeypatch.setattr(jm.app, "find_duplicate", lambda *a, **k: None)
    res, code = jm.app.create_job({"customer": "Acme", "title": "Item 2", "job_ticket": "T-88",
                                   "upc": jm.upc(2), "quantity": 100, "lpr": 50})
    assert code == 409, res
    assert "T-88" in res["error"]
    c = _db(jm)
    try:
        assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    finally:
        c.close()


def test_existing_duplicate_tickets_are_reported_and_the_rule_waits_until_fixed(jm):
    jm.job(1, ticket="T-5")
    c = _db(jm)
    c.execute("DROP INDEX ux_jobs_active_ticket")
    c.execute(INSERT_JOB, ("JOB-0600", "T-5", "Pending"))        # duplicate from before the rule
    c.commit(); c.close()

    c = _db(jm)
    try:
        dups = jm.app.ensure_ticket_index(c)
        assert dups == [{"ticket": "t-5", "jobs": ["JOB-0001", "JOB-0600"]}]
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='ux_jobs_active_ticket'").fetchone()
        d = jm.app.app.test_client().get("/api/data/status").get_json()
        assert any("JOB-0600" in w["text"] for w in d["warnings"])

        c.execute("UPDATE jobs SET status='Cancelled' WHERE job_number='JOB-0600'"); c.commit()
        assert jm.app.ensure_ticket_index(c) == []
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='ux_jobs_active_ticket'").fetchone()
    finally:
        c.close()


# ── the tools that copy rows in bulk ──────────────────────────────────────────
def test_registry_line_that_breaks_a_rule_is_skipped_on_startup_not_fatal(jm):
    """A hand-edited registry line that ends before it starts must not stop the app from starting."""
    upc = jm.upc(3)
    with open(os.path.join(jm.config.SERIAL_REGISTRY_DIR, f"{upc}.txt"), "w", encoding="utf-8") as fh:
        fh.write("2026-01-01 00:00:00 | JOB-0100 | 500 | 499 | 1 | typo\n"
                 "2026-01-01 00:00:00 | JOB-0101 | 600 | 699 | 100 | ok\n")
    assert jm.SS.backfill_from_registry() == 1


def test_restore_keeping_a_job_that_reused_a_cancelled_ticket(jm):
    """Backup has JOB-0001 on T-9. Afterwards JOB-0001 is cancelled and JOB-0002 takes T-9.
    Keeping new work must copy the cancellation before the new job, or the ticket rule refuses it."""
    rb = jm.restore()
    rb.app_is_running = lambda: False
    jm.job(1, ticket="T-9")
    z = jm.app.make_backup()
    assert z["success"], z
    jid = _job_id(jm, "JOB-0001")
    assert jm.app.app.test_client().patch(f"/api/jobs/{jid}", json={"status": "Cancelled"}).status_code == 200
    jm.job(2, ticket="T-9")

    rb.apply_restore(os.path.join(jm.backups, z["file"]), keep=True)

    c = _db(jm)
    try:
        rows = dict(c.execute("SELECT job_number, status FROM jobs").fetchall())
    finally:
        c.close()
    assert rows == {"JOB-0001": "Cancelled", "JOB-0002": "Pending"}


def test_import_from_old_app_skips_jobs_that_break_a_rule_and_says_which(jm, tmp_path, monkeypatch, capsys):
    import importlib
    import sys
    old = tmp_path / "old"; old.mkdir()
    c = sqlite3.connect(old / "local.db")
    c.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, job_number TEXT, job_ticket TEXT, title TEXT, "
              "customer TEXT, status TEXT, created_at TEXT, updated_at TEXT, archived_at TEXT)")
    c.execute("CREATE TABLE job_rolls (id INTEGER PRIMARY KEY, job_id INTEGER, roll_number INTEGER, "
              "quantity INTEGER, status TEXT, created_at TEXT, notes TEXT)")
    ts = "2025-01-01 00:00:00"
    c.execute("INSERT INTO jobs VALUES (1, 'JOB-0700', 'OLD-1', 'Good', 'Acme', 'Complete', ?, ?, NULL)", (ts, ts))
    c.execute("INSERT INTO jobs VALUES (2, 'JOB-0701', 'OLD-2', 'Bad', 'Acme', 'Shipped', ?, ?, NULL)", (ts, ts))
    c.execute("INSERT INTO job_rolls VALUES (1, 1, 1, 50, 'verified', ?, NULL)", (ts,))
    c.execute("INSERT INTO job_rolls VALUES (2, 2, 1, 50, 'complete', ?, 'roll note on the bad job')", (ts,))
    c.commit(); c.close()

    sys.path.insert(0, jm.root)
    try:
        sys.modules.pop("import_old", None)
        IO = importlib.import_module("import_old")
    finally:
        sys.path.remove(jm.root)
    monkeypatch.setattr(sys, "argv", ["import_old.py", str(old)])
    IO.main()

    out = capsys.readouterr().out
    assert "1 skipped" in out and "JOB-0701" in out and "job status must be one of" in out
    d = _db(jm)
    try:
        assert {r[0] for r in d.execute("SELECT job_number FROM jobs")} == {"JOB-0700"}
        assert d.execute("SELECT COUNT(*) FROM job_rolls").fetchone()[0] == 1
        assert d.execute("SELECT COUNT(*) FROM job_notes WHERE body='roll note on the bad job'").fetchone()[0] == 0
    finally:
        d.close()


def test_new_rules_arrive_through_a_migration(jm):
    """Existing installs get the rules from a numbered migration (see test_schema)."""
    assert any("rule" in desc for _, desc, _ in jm.app.MIGRATIONS)
