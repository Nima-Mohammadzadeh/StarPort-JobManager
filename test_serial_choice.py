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
    c = jm.db.open()
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


# ── specified start: refused only when the range was already used for this UPC ──────────────
def gap_setup(jm, n):
    """UPC with job A at s..s+99 and job B far ahead at s+10000..s+10099, leaving an unused gap between."""
    upc = jm.upc(n)
    a, code = make(jm, n, serial_mode="auto", job_ticket=f"G{n}a")
    assert code == 201, a
    s = a["job"]["start_serial"]
    b, code = make(jm, n, serial_mode="manual", start_serial=s + 10_000, job_ticket=f"G{n}b")
    assert code == 201, b
    return upc, s, a, b


def test_start_inside_an_unused_gap_below_the_last_serial_is_allowed(jm):
    upc, s, a, b = gap_setup(jm, 20)
    res, code = make(jm, 20, serial_mode="manual", start_serial=s + 500, job_ticket="G20c")
    assert code == 201, res
    assert res["job"]["start_serial"] == s + 500
    spans = sorted((r["start"], r["end"]) for r in jm.R.read_records(upc))
    assert (s + 500, s + 599) in spans
    mir = [r for r in mirror(jm, upc) if r["job_number"] == res["job_number"]]
    assert [(r["start"], r["end"], r["registry_state"]) for r in mir] == [(s + 500, s + 599, 1)]


def test_automatic_after_a_gap_fill_still_continues_from_the_highest_serial(jm):
    upc, s, a, b = gap_setup(jm, 21)
    make(jm, 21, serial_mode="manual", start_serial=s + 500, job_ticket="G21c")
    nxt, code = make(jm, 21, serial_mode="auto", job_ticket="G21d")
    assert code == 201, nxt
    assert nxt["job"]["start_serial"] == s + 10_100


def test_start_inside_a_used_range_is_refused_and_names_the_job(jm):
    upc, s, a, b = gap_setup(jm, 22)
    before = len(mirror(jm, upc))
    res, code = make(jm, 22, serial_mode="manual", start_serial=s + 50, job_ticket="G22c")
    assert code == 400
    assert a["job_number"] in res["error"]
    assert len(mirror(jm, upc)) == before
    assert len(jm.R.read_records(upc)) == before


def test_range_that_runs_into_a_later_used_range_is_refused(jm):
    upc, s, a, b = gap_setup(jm, 23)
    res, code = make(jm, 23, serial_mode="manual", start_serial=s + 10_000 - 50, job_ticket="G23c")
    assert code == 400
    assert b["job_number"] in res["error"]


def test_range_ending_just_before_a_used_range_is_allowed(jm):
    upc, s, a, b = gap_setup(jm, 24)
    res, code = make(jm, 24, serial_mode="manual", start_serial=s + 10_000 - 100, job_ticket="G24c")
    assert code == 201, res


def test_overlap_is_still_caught_when_the_registry_file_is_lost(jm):
    upc, s, a, b = gap_setup(jm, 25)
    os.remove(os.path.join(jm.config.SERIAL_REGISTRY_DIR, f"{upc}.txt"))
    res, code = make(jm, 25, serial_mode="manual", start_serial=s + 10, job_ticket="G25c")
    assert code == 400, res


def test_range_check_endpoint_reports_conflicts_and_free_ranges(jm):
    upc, s, a, b = gap_setup(jm, 26)
    c = jm.app.app.test_client()
    bad = c.get(f"/api/upc/{upc}/range?start={s + 50}&qty=100").get_json()
    assert bad["ok"] is False and [x["job_number"] for x in bad["conflicts"]] == [a["job_number"]]
    good = c.get(f"/api/upc/{upc}/range?start={s + 500}&qty=100").get_json()
    assert good["ok"] is True and good["conflicts"] == []
