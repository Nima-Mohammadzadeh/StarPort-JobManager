"""
New job: the person says whether serials are automatic or a start serial they specify.
Whichever they pick, the range must land in all the serial records for the UPC (registry file,
app database mirror, journal) and the job, and a contradictory request must create nothing.
"""
import os


def make(jm, n, **kw):
    payload = {"customer": "Acme", "title": f"Item {n}", "job_ticket": f"SC{n}", "upc": jm.upc(n),
               "quantity": 100, "lpr": 50, "overage_pct": 0, "created_by": "Pat", **kw}
    return jm.app.create_job(payload)


def mirror(jm, upc):
    c = jm.SS._db()
    try:
        return [dict(r) for r in c.execute("SELECT * FROM serial_allocations WHERE upc=? ORDER BY start", (upc,))]
    finally:
        c.close()


def journal_rows(jm, upc):
    with open(jm.SS.journal_path(), encoding="utf-8") as fh:
        return [ln for ln in fh if upc in ln]


def test_manual_start_is_recorded_everywhere(jm):
    upc = jm.upc(1)
    first = jm.SS.next_start(upc)
    start = first + 5_000
    res, code = make(jm, 1, serial_mode="manual", start_serial=start)
    assert code == 201, res
    assert res["job"]["start_serial"] == start
    assert res["serial_alloc"]["end"] == start + 99
    reg = [r for r in jm.R.read_records(upc) if r["job_number"] == res["job_number"]]
    assert [(r["start"], r["end"]) for r in reg] == [(start, start + 99)]
    mir = [r for r in mirror(jm, upc) if r["job_number"] == res["job_number"]]
    assert [(r["start"], r["end"], r["registry_state"]) for r in mir] == [(start, start + 99, 1)]
    assert any(str(start) in ln for ln in journal_rows(jm, upc))
    assert jm.SS.next_start(upc) == start + 100


def test_manual_start_note_says_who_specified_it(jm):
    upc = jm.upc(2)
    res, code = make(jm, 2, serial_mode="manual", start_serial=jm.SS.next_start(upc) + 10)
    assert code == 201, res
    note = [r for r in mirror(jm, upc) if r["job_number"] == res["job_number"]][0]["note"]
    assert "specified by Pat" in note


def test_manual_without_a_start_creates_nothing(jm):
    res, code = make(jm, 3, serial_mode="manual")
    assert code == 400 and "start serial" in res["error"].lower()
    assert mirror(jm, jm.upc(3)) == []
    assert jm.app.get_db().execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_auto_with_a_start_is_a_contradiction(jm):
    upc = jm.upc(4)
    res, code = make(jm, 4, serial_mode="auto", start_serial=jm.SS.next_start(upc) + 1)
    assert code == 400
    assert mirror(jm, upc) == []


def test_auto_takes_the_next_free_serial_and_records_it(jm):
    a, _ = make(jm, 5, serial_mode="auto")
    b, code = make(jm, 5, serial_mode="auto", job_ticket="SC5b")
    assert code == 201, b
    assert b["job"]["start_serial"] == a["job"]["start_serial"] + 100
    assert len(mirror(jm, jm.upc(5))) == 2


def test_manual_start_below_whats_used_is_refused_and_not_recorded(jm):
    upc = jm.upc(6)
    a, _ = make(jm, 6, serial_mode="auto")
    res, code = make(jm, 6, serial_mode="manual", start_serial=a["job"]["start_serial"], job_ticket="SC6b")
    assert code == 400
    assert len(mirror(jm, upc)) == 1
    assert len(jm.R.read_records(upc)) == 1


def test_unknown_mode_is_refused(jm):
    res, code = make(jm, 7, serial_mode="whatever")
    assert code == 400
    assert mirror(jm, jm.upc(7)) == []


def test_calls_without_a_mode_still_work_as_before(jm):
    res, code = make(jm, 8)
    assert code == 201, res


def test_activity_log_says_how_serials_were_chosen(jm):
    upc = jm.upc(9)
    start = jm.SS.next_start(upc) + 3
    res, code = make(jm, 9, serial_mode="manual", start_serial=start)
    assert code == 201, res
    c = jm.app.get_db()
    try:
        text = " ".join(str(dict(r)) for r in c.execute("SELECT * FROM activity"))
    finally:
        c.close()
    assert "specified by user" in text
