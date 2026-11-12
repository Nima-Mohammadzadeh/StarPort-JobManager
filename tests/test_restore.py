"""T3: restore tool. Serial numbers and job numbers never go backwards; keep/drop is asked
only when there is work since the backup."""
import os
import shutil
import sqlite3

import pytest


def _zip(jm):
    r = jm.app.make_backup()
    assert r["success"], r
    return os.path.join(jm.backups, r["file"])


def _jobs(jm):
    c = sqlite3.connect(jm.config.DB_PATH)
    try:
        return {r[0] for r in c.execute("SELECT job_number FROM jobs")}
    finally:
        c.close()


def _all_next(jm, RB):
    c = sqlite3.connect(jm.config.DB_PATH)
    try:
        return RB.next_starts(RB._all_upcs(c))
    finally:
        c.close()          # an open connection keeps local.db locked on Windows


@pytest.fixture
def RB(jm):
    rb = jm.restore()
    rb.app_is_running = lambda: False
    return rb


def test_no_question_when_nothing_changed(jm, RB):
    jm.job(1); jm.ranges(count=5)
    z = _zip(jm)
    plan = RB.plan_restore(z)
    assert plan["check"]["ok"] and not plan["conflict"]
    asked = []
    r = RB.apply_restore(z, ask=lambda p: asked.append(1) or True)
    assert asked == [] and r["serials_went_back"] == {}


def test_work_since_backup_requires_a_choice(jm, RB):
    jm.job(1)
    z = _zip(jm)
    jm.job(2)
    plan = RB.plan_restore(z)
    assert plan["conflict"] and [j["job_number"] for j in plan["new_jobs"]] == ["JOB-0002"]
    with pytest.raises(RB.RestoreError, match="keep-new-work"):
        RB.apply_restore(z)                       # no answer given, no terminal: refuse, change nothing
    assert _jobs(jm) == {"JOB-0001", "JOB-0002"}
    assert not any(d.startswith("restore_safety_") for d in os.listdir(jm.root))


def test_drop_old_backup_never_lowers_any_serial(jm, RB):
    jm.job(1); jm.ranges(count=40)
    z = _zip(jm)
    jm.job(2, upc=jm.upc(1)); jm.ranges(count=40)          # more serials after the backup
    before = _all_next(jm, RB)
    lines_before = {u: jm.R.read_records(u) for u in before}
    r = RB.apply_restore(z, keep=False)
    after = _all_next(jm, RB)
    for u, recs in lines_before.items():                     # no registry line is ever lost
        now = {(x["job_number"], x["start"]) for x in jm.R.read_records(u)}
        assert {(x["job_number"], x["start"]) for x in recs} <= now, u
    assert all(after[u] >= before[u] for u in before)
    assert r["serials_went_back"] == {}
    assert _jobs(jm) == {"JOB-0001"}                         # the new job was dropped...
    c = sqlite3.connect(jm.config.DB_PATH)
    assert c.execute("SELECT COUNT(*) FROM serial_allocations WHERE job_number='JOB-0002'").fetchone()[0] == 1
    c.close()                                                # ...but its serials stay used
    jm.load()
    new = jm.job(3)
    assert new["job_number"] == "JOB-0003"                   # and its job number is never reused


def test_keep_brings_new_and_changed_work_into_the_restored_database(jm, RB):
    a = jm.job(1)
    z = _zip(jm)
    with jm.app.app.test_request_context():
        jm.app._set_rolls(a["job"]["id"], [1, 2], "complete", "Nima")   # change after the backup
    jm.job(2)
    r = RB.apply_restore(z, keep=True)
    assert set(r["kept_jobs"]) == {"JOB-0001", "JOB-0002"}
    c = sqlite3.connect(jm.config.DB_PATH)
    done = c.execute("SELECT COUNT(*) FROM job_rolls r JOIN jobs j ON j.id=r.job_id "
                     "WHERE j.job_number='JOB-0001' AND r.status='complete'").fetchone()[0]
    rolls2 = c.execute("SELECT COUNT(*) FROM job_rolls r JOIN jobs j ON j.id=r.job_id "
                       "WHERE j.job_number='JOB-0002'").fetchone()[0]
    c.close()
    assert done == 2 and rolls2 == 3


def test_lost_registry_and_database_come_back_from_the_zip(jm, RB):
    jm.job(1); jm.ranges(count=25)
    z = _zip(jm)
    expected = _all_next(jm, RB)
    shutil.rmtree(jm.config.SERIAL_REGISTRY_DIR)
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(jm.config.DB_PATH + ext):
            os.remove(jm.config.DB_PATH + ext)
    r = RB.apply_restore(z)
    assert r["registry"]["copied"] == 26
    assert _all_next(jm, RB) == expected
    assert _jobs(jm) == {"JOB-0001"}


def test_damaged_database_is_replaced(jm, RB):
    jm.job(1)
    z = _zip(jm)
    with open(jm.config.DB_PATH, "r+b") as fh:
        fh.seek(0); fh.write(b"this is not sqlite" * 50)
    for ext in ("-wal", "-shm"):
        if os.path.exists(jm.config.DB_PATH + ext):
            os.remove(jm.config.DB_PATH + ext)
    plan = RB.plan_restore(z)
    assert not plan["current_readable"] and not plan["conflict"]
    RB.apply_restore(z)
    assert _jobs(jm) == {"JOB-0001"}


def test_refuses_while_app_is_running(jm, RB):
    jm.job(1)
    z = _zip(jm)
    RB.app_is_running = lambda: True
    with pytest.raises(RB.RestoreError, match="running"):
        RB.apply_restore(z)


def test_refuses_a_backup_that_fails_its_check(jm, RB, tmp_path):
    jm.ranges(count=3)
    z = _zip(jm)
    import zipfile
    bad = str(tmp_path / "bad.zip")
    with zipfile.ZipFile(z) as zin, zipfile.ZipFile(bad, "w") as zout:
        for n in zin.namelist():
            if n != f"serial_registry/{jm.upc(1000)}.txt":
                zout.writestr(n, zin.read(n))
    with pytest.raises(RB.RestoreError, match="failed its check"):
        RB.apply_restore(bad)


def test_safety_copy_and_log_are_written(jm, RB):
    jm.job(1)
    z = _zip(jm)
    jm.job(2)
    r = RB.apply_restore(z, keep=False)
    s = r["safety_copy"]
    assert os.path.isfile(os.path.join(s, "local.db"))
    assert os.path.isdir(os.path.join(s, "serial_registry"))
    log = open(os.path.join(s, "restore_log.txt")).read()
    assert "JOB-0002" in log


def test_running_twice_is_safe(jm, RB):
    jm.job(1); jm.ranges(count=5)
    z = _zip(jm)
    RB.apply_restore(z)
    first = _all_next(jm, RB)
    r = RB.apply_restore(z)
    assert _all_next(jm, RB) == first and r["registry"]["merged"] == 0


def test_preview_changes_nothing(jm, RB, capsys):
    jm.job(1)
    z = _zip(jm)
    jm.job(2)
    assert RB.main([z]) == 0
    assert "Preview only" in capsys.readouterr().out
    assert _jobs(jm) == {"JOB-0001", "JOB-0002"}
