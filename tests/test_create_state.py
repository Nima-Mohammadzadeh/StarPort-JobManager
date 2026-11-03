"""H2: a job that was never finished being created (crash, or a failure after the number was
reserved) is marked, listed with the serials it took, and can be cancelled to free its ticket.
A job still being created right now is not reported."""
import sqlite3
from datetime import datetime, timedelta

import pytest


def _db(jm):
    c = sqlite3.connect(jm.config.DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _row(jm, number):
    c = _db(jm)
    try:
        r = c.execute("SELECT * FROM jobs WHERE job_number=?", (number,)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def _age(jm, number, minutes):
    """Pretend the job was started `minutes` ago."""
    ts = (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    c = _db(jm)
    c.execute("UPDATE jobs SET created_at=? WHERE job_number=?", (ts, number)); c.commit(); c.close()


def _unfinished(jm, min_age=0):
    c = _db(jm)
    try:
        return jm.app.unfinished_jobs(c, min_age_minutes=min_age)
    finally:
        c.close()


def _spec(jm, n, ticket):
    return {"customer": "Acme", "title": f"Item {n}", "job_ticket": ticket,
            "upc": jm.upc(n), "quantity": 100, "lpr": 50}


def _fail_saving(jm, monkeypatch):
    """Make step 6 (saving the finished job) fail, after serials were taken and files written."""
    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(jm.app, "_insert_rolls", boom)


def test_a_finished_job_is_marked_ready(jm):
    res = jm.job(1)
    assert _row(jm, res["job_number"])["create_state"] == "ready"
    assert _unfinished(jm) == []


def test_failure_while_saving_leaves_a_marked_job_with_its_serials_listed(jm, monkeypatch):
    _fail_saving(jm, monkeypatch)
    res, code = jm.app.create_job(_spec(jm, 2, "T-2"))
    assert code == 500, res
    assert _row(jm, "JOB-0001")["create_state"] == "reserving"
    [u] = _unfinished(jm)
    assert u["job_number"] == "JOB-0001" and u["job_ticket"] == "T-2"
    assert len(u["serials"]) == 1 and u["serials"][0]["upc"] == jm.upc(2)


def test_data_panel_reports_only_jobs_stuck_for_a_while(jm, monkeypatch):
    _fail_saving(jm, monkeypatch)
    jm.app.create_job(_spec(jm, 2, "T-2"))
    client = jm.app.app.test_client()

    texts = [w["text"] for w in client.get("/api/data/status").get_json()["warnings"]]
    assert not any("JOB-0001" in t for t in texts)             # may still be being created

    _age(jm, "JOB-0001", jm.app.UNFINISHED_AFTER_MINUTES + 1)
    texts = [w["text"] for w in client.get("/api/data/status").get_json()["warnings"]]
    hit = [t for t in texts if "JOB-0001" in t]
    assert hit and "serials" in hit[0] and "Cancelled" in hit[0]


def test_crash_between_reserving_and_saving_is_found_at_the_next_start(jm, capsys):
    """Simulate a crash right after step 1: only the reserved row exists."""
    c = _db(jm)
    c.execute("INSERT INTO jobs (job_number, job_ticket, title, customer, upc, quantity, status, created_at, "
              "updated_at, create_state) VALUES ('JOB-0042', 'T-42', 'Item', 'Acme', ?, 100, 'Pending', "
              "'2026-01-01 08:00:00', '2026-01-01 08:00:00', 'reserving')", (jm.upc(42),))
    c.commit(); c.close()
    capsys.readouterr()
    jm.app.init_db()
    out = capsys.readouterr().out
    assert "JOB-0042" in out and "never finished" in out


def test_cleanup_that_fails_is_reported_and_the_job_stays_marked(jm, monkeypatch, capsys):
    """Before: _discard_job swallowed every error, so a failed cleanup left an invisible ghost."""
    c = _db(jm)
    c.execute("CREATE TRIGGER test_no_delete BEFORE DELETE ON jobs BEGIN SELECT RAISE(ABORT, 'locked'); END")
    c.commit(); c.close()

    def refuse(*a, **k):
        raise ValueError("no serials today")
    monkeypatch.setattr(jm.app.SS, "reserve", refuse)
    res, code = jm.app.create_job(_spec(jm, 3, "T-3"))
    assert code == 400, res

    out = capsys.readouterr().out
    assert "JOB-0001" in out and "could not be undone" in out
    assert [u["job_number"] for u in _unfinished(jm)] == ["JOB-0001"]
    assert _unfinished(jm)[0]["serials"] == []


def test_ticket_held_by_an_unfinished_job_says_how_to_free_it(jm, monkeypatch):
    _fail_saving(jm, monkeypatch)
    jm.app.create_job(_spec(jm, 4, "T-4"))
    monkeypatch.undo()
    _age(jm, "JOB-0001", jm.app.UNFINISHED_AFTER_MINUTES + 1)

    res, code = jm.app.create_job(_spec(jm, 4, "T-4"))
    assert code == 409
    assert "never finished" in res["error"] and "Cancelled" in res["error"]


def test_cancelling_an_unfinished_job_frees_its_ticket_and_keeps_its_serials_used(jm, monkeypatch):
    _fail_saving(jm, monkeypatch)
    jm.app.create_job(_spec(jm, 5, "T-5"))
    monkeypatch.undo()
    taken = _unfinished(jm)[0]["serials"][0]
    jid = _row(jm, "JOB-0001")["id"]

    r = jm.app.app.test_client().patch(f"/api/jobs/{jid}", json={"status": "Cancelled"})
    assert r.status_code == 200, r.get_json()
    assert _unfinished(jm) == []

    res, code = jm.app.create_job(_spec(jm, 5, "T-5"))
    assert code == 201, res
    assert res["serial_alloc"]["start"] > taken["end"]          # the stuck job's serials stay used


def test_a_job_still_being_created_is_not_flagged(jm):
    c = _db(jm)
    c.execute("INSERT INTO jobs (job_number, title, customer, status, created_at, updated_at, create_state) "
              "VALUES ('JOB-0077', 'Item', 'Acme', 'Pending', ?, ?, 'reserving')",
              (jm.app.now_str(), jm.app.now_str()))
    c.commit(); c.close()
    assert [u["job_number"] for u in _unfinished(jm, min_age=0)] == ["JOB-0077"]
    assert _unfinished(jm, min_age=jm.app.UNFINISHED_AFTER_MINUTES) == []


def test_upgrade_marks_every_existing_job_ready(jm):
    """Jobs from before this change were all finished (or are the ghosts we can't tell apart):
    they become 'ready' so nothing old is suddenly flagged."""
    jm.job(1)
    c = _db(jm)
    for t in ("chk_jobs_create_state_ins", "chk_jobs_create_state_upd"):
        c.execute(f"DROP TRIGGER {t}")
    c.execute("ALTER TABLE jobs DROP COLUMN create_state")
    c.execute("UPDATE _meta SET value='2' WHERE key='schema_version'")
    c.commit(); c.close()

    jm.app.init_db()
    assert _row(jm, "JOB-0001")["create_state"] == "ready"
    c = _db(jm)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE jobs SET create_state='half' WHERE job_number='JOB-0001'")
    finally:
        c.close()
