"""
Adding a CDO or GO2 job, checked from every side: what a client can send, what lands on disk,
what is recorded, and what a second user or a failure can do to it. Nothing here may leave a
half-made job, a reused serial, or a file outside the job folders.
"""
import os
import re
import threading

import pytest
from openpyxl import load_workbook

FIRST = 3_768_677_565
SHEETS = [("CDO", "cdo_seq"), ("GO2", "go2_rand")]


def body(customer, sheet, n, qty=50, **kw):
    return {"customer": customer, "title": f"Tag {n}", "job_ticket": f"S{n}", "job_type": "special",
            "data_sheet": sheet, "quantity": qty, "lpr": 25, **kw}


def post(jm, payload, path="/api/jobs"):
    return jm.app.app.test_client().post(path, json=payload)


def job_rows(jm):
    c = jm.app.get_db()
    try:
        return c.execute("SELECT job_number, folder_path FROM jobs").fetchall()
    finally:
        c.close()


def folders_on_disk(jm):
    """Job folders left behind (empty customer / label-size parents are harmless and not counted)."""
    return [d for d, ns, _ in os.walk(jm.customers) if "Data" in ns]


def inside(base, path):
    base, path = os.path.realpath(base), os.path.realpath(path)
    return os.path.commonpath([base, path]) == base


# ── 1. What a client can send ────────────────────────────────────────────────
@pytest.mark.parametrize("customer,sheet", SHEETS)
def test_created_over_http_returns_the_job_and_its_sheet(jm, customer, sheet):
    r = post(jm, body(customer, sheet, 1))
    assert r.status_code == 201, r.get_json()
    j = r.get_json()
    assert j["job"]["customer"] == customer and j["job"]["data_sheet"] == sheet
    assert j["job"]["upc"] is None and j["rolls"] == 2
    assert os.path.isfile(os.path.join(j["folder_path"], "Data", os.listdir(os.path.join(j["folder_path"], "Data"))[0]))


@pytest.mark.parametrize("customer,sheet", SHEETS)
@pytest.mark.parametrize("qty", [float("inf"), "1e999", "inf", "nan", "abc", [], {}, 0, -5, "-1"])
def test_unusable_quantity_is_refused_not_a_crash(jm, customer, sheet, qty):
    r = post(jm, body(customer, sheet, 2, qty=qty))
    assert r.status_code == 400, (qty, r.status_code, r.get_data(as_text=True)[:200])
    assert job_rows(jm) == [] and folders_on_disk(jm) == []
    assert jm.SHEETS.next_start("cdo_seq") == FIRST


@pytest.mark.parametrize("field,val", [("overage_pct", "nan"), ("overage_pct", "inf"), ("overage_pct", 101),
                                       ("overage_pct", -1), ("lpr", 0), ("lpr", "x"), ("qty_db", -3)])
@pytest.mark.parametrize("customer,sheet", SHEETS)
def test_unusable_numbers_are_refused(jm, customer, sheet, field, val):
    r = post(jm, body(customer, sheet, 3, **{field: val}))
    assert r.status_code == 400, (field, val, r.get_json())
    assert job_rows(jm) == [] and jm.SHEETS.next_start("cdo_seq") == FIRST


@pytest.mark.parametrize("payload", [None, [], "text", 7])
def test_non_object_body_is_refused(jm, payload):
    r = jm.app.app.test_client().post("/api/jobs", json=payload)
    assert r.status_code in (400, 415), r.status_code
    assert job_rows(jm) == []


def test_non_json_body_is_refused(jm):
    r = jm.app.app.test_client().post("/api/jobs", data="customer=CDO", content_type="text/plain")
    assert r.status_code == 400 and job_rows(jm) == []


@pytest.mark.parametrize("sheet", ["CDO_SEQ", "../cdo_seq", "cdo_seq; DROP TABLE jobs", 5])
def test_unknown_sheet_name_is_refused(jm, sheet):
    r = post(jm, body("CDO", sheet, 4))
    assert r.status_code == 400 and job_rows(jm) == []
    assert jm.SHEETS.next_start("cdo_seq") == FIRST


def test_sheet_without_a_quantity_is_refused(jm):
    r = post(jm, {**body("GO2", "go2_rand", 5), "quantity": None})
    assert r.status_code == 400 and job_rows(jm) == []


# ── 2. Size limits: one request must not be able to hang or fill the PC ──────
@pytest.mark.parametrize("customer,sheet", SHEETS)
def test_absurd_quantity_is_refused_before_anything_is_made(jm, monkeypatch, customer, sheet):
    def must_not_run(*a, **k):
        raise AssertionError("sheet generation started for an absurd quantity")
    monkeypatch.setattr(jm.app, "generate_go2_files", must_not_run)
    monkeypatch.setattr(jm.app, "generate_cdo_files", must_not_run)
    r = post(jm, body(customer, sheet, 6, qty=10 ** 9))
    assert r.status_code == 400, r.get_json()
    assert job_rows(jm) == [] and folders_on_disk(jm) == []
    assert jm.SHEETS.next_start("cdo_seq") == FIRST


@pytest.mark.parametrize("customer,sheet", SHEETS)
def test_one_file_cannot_pass_excels_row_limit(jm, monkeypatch, customer, sheet):
    """Excel stops at 1,048,576 rows (one is the header). A bigger single file can't be opened."""
    def must_not_run(*a, **k):
        raise AssertionError("sheet generation started for a file Excel can't open")
    monkeypatch.setattr(jm.app, "generate_go2_files", must_not_run)
    monkeypatch.setattr(jm.app, "generate_cdo_files", must_not_run)
    r = post(jm, body(customer, sheet, 7, qty=1_048_576))
    assert r.status_code == 400 and "Qty per DB file" in r.get_json()["error"]
    assert job_rows(jm) == [] and jm.SHEETS.next_start("cdo_seq") == FIRST


@pytest.mark.parametrize("customer,sheet", SHEETS)
def test_file_split_that_stays_under_excels_limit_is_accepted(jm, monkeypatch, customer, sheet):
    calls = []
    monkeypatch.setattr(jm.app, "generate_go2_files", lambda *a, **k: calls.append(a) or {})
    monkeypatch.setattr(jm.app, "generate_cdo_files", lambda *a, **k: calls.append(a) or {})
    r = post(jm, body(customer, sheet, 8, qty=1_048_576, qty_db=500_000))
    assert r.status_code == 201, r.get_json()
    assert len(calls) == 1


# ── 3. Nothing leaves the job folders ────────────────────────────────────────
@pytest.mark.parametrize("bad", ["..", ".", "...", "../..", "..\\..", "CON", "nul", "aux.txt", "x.", " . "])
@pytest.mark.parametrize("customer,sheet", SHEETS)
def test_label_size_cannot_walk_out_of_the_job_folders(jm, customer, sheet, bad):
    """A label size is a folder name: it must stay one folder, directly inside the customer's folder."""
    before = set(os.listdir(os.path.dirname(jm.customers)))
    r = post(jm, body(customer, sheet, 9, label_size=bad))
    assert set(os.listdir(os.path.dirname(jm.customers))) == before, "something was written outside the job folders"
    assert r.status_code == 201, r.get_json()
    folder = r.get_json()["folder_path"]
    assert inside(os.path.join(jm.customers, customer), folder), folder
    rel = os.path.relpath(os.path.realpath(folder), os.path.realpath(os.path.join(jm.customers, customer)))
    assert len(rel.split(os.sep)) == 2, rel          # <label size>\<date - ticket>


@pytest.mark.parametrize("bad", ["..", "../x", "CON", "a" * 400])
@pytest.mark.parametrize("customer,sheet", SHEETS)
def test_ticket_cannot_escape_or_leave_a_half_made_job(jm, customer, sheet, bad):
    before = set(os.listdir(os.path.dirname(jm.customers)))
    r = post(jm, {**body(customer, sheet, 10), "job_ticket": bad})
    assert set(os.listdir(os.path.dirname(jm.customers))) == before
    if r.status_code == 201:
        j = r.get_json()
        assert inside(jm.customers, j["folder_path"])
        assert all(inside(jm.customers, os.path.join(j["folder_path"], "Data", f))
                   for f in os.listdir(os.path.join(j["folder_path"], "Data")))
    else:
        assert job_rows(jm) == [] and folders_on_disk(jm) == []


def test_job_file_download_cannot_leave_the_job_folder(jm):
    j = post(jm, body("CDO", "cdo_seq", 11)).get_json()
    secret = os.path.join(os.path.dirname(jm.customers), "secret.txt")
    open(secret, "w").write("nope")
    c = jm.app.app.test_client()
    for rel in ["../../../secret.txt", "..%2F..%2F..%2Fsecret.txt", "Data/../../../../secret.txt",
                "%2e%2e/%2e%2e/%2e%2e/secret.txt", "C:/Windows/win.ini", "..\\..\\..\\secret.txt"]:
        assert c.get(f"/api/jobs/{j['job']['id']}/file/{rel}").status_code == 404, rel
    name = os.listdir(os.path.join(j["folder_path"], "Data"))[0]
    assert c.get(f"/api/jobs/{j['job']['id']}/file/Data/{name}").status_code == 200


# ── 4. Hostile text is data, never markup or SQL ─────────────────────────────
@pytest.mark.parametrize("customer,sheet", SHEETS)
def test_markup_in_text_fields_is_escaped_in_every_printout(jm, customer, sheet):
    evil = '<img src=x onerror=alert(1)>'
    r = post(jm, body(customer, sheet, 12, part_number=evil, po_number=evil, inlay_type=evil, notes=evil,
                      title=evil, label_size="4x2"))
    assert r.status_code == 201, r.get_json()
    j = r.get_json()
    folder = j["folder_path"]
    pages = [open(os.path.join(folder, f), encoding="utf-8").read() for f in os.listdir(folder) if f.endswith(".html")]
    pages.append(jm.app.app.test_client().get(f"/jobs/{j['job']['id']}/roll-sheet").get_data(as_text=True))
    assert pages
    for html in pages:
        assert "<img src=x" not in html, "unescaped markup reached a printable page"


@pytest.mark.parametrize("customer,sheet", SHEETS)
def test_sql_text_is_stored_as_text(jm, customer, sheet):
    nasty = "x'); DROP TABLE jobs;--"
    r = post(jm, {**body(customer, sheet, 13), "title": nasty, "job_ticket": nasty, "notes": nasty})
    assert r.status_code == 201, r.get_json()
    assert jm.app.app.test_client().get("/api/jobs").status_code == 200
    assert r.get_json()["job"]["title"] == nasty
    assert jm.app.app.test_client().get("/api/ticket-check", query_string={"ticket": nasty}).get_json()["exists"]


# ── 5. A second person, a double click, a crash ──────────────────────────────
@pytest.mark.parametrize("customer,sheet", SHEETS)
def test_same_ticket_entered_at_the_same_moment_makes_one_job(jm, customer, sheet):
    results, gate = [], threading.Barrier(8)

    def go():
        c = jm.app.app.test_client()
        gate.wait()
        results.append(c.post("/api/jobs", json=body(customer, sheet, 14)).status_code)
    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(results) == [201] + [409] * 7, results
    assert len(job_rows(jm)) == 1 and len(folders_on_disk(jm)) >= 1
    if sheet == "cdo_seq":
        assert len(jm.SHEETS.history("cdo_seq")) == 1          # one set of numbers, not eight


def test_different_cdo_jobs_at_the_same_moment_never_share_a_number(jm):
    got, gate = [], threading.Barrier(6)

    def go(i):
        c = jm.app.app.test_client()
        gate.wait()
        r = c.post("/api/jobs", json=body("CDO", "cdo_seq", 100 + i, qty=40, qty_db=15))
        got.append((r.status_code, r.get_json()))
    ts = [threading.Thread(target=go, args=(i,)) for i in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert [c for c, _ in got] == [201] * 6, got
    serials = []
    for _, j in got:
        d = os.path.join(j["folder_path"], "Data")
        for f in os.listdir(d):
            wb = load_workbook(os.path.join(d, f), read_only=True)
            serials += [r[0] for r in wb.active.iter_rows(min_row=2, values_only=True)]
            wb.close()
    assert len(serials) == 240 and len(set(serials)) == 240
    assert min(serials) == FIRST and max(serials) == FIRST + 239          # one unbroken series


@pytest.mark.parametrize("customer,sheet", SHEETS)
def test_failed_sheet_leaves_no_job_folder_or_files(jm, monkeypatch, customer, sheet):
    import generators as g
    monkeypatch.setattr(g, "verify_go2_file", lambda *a: (_ for _ in ()).throw(ValueError("disk full")))
    monkeypatch.setattr(g, "verify_cdo_file", lambda *a: (_ for _ in ()).throw(ValueError("disk full")))
    monkeypatch.setattr(jm.app, "generate_go2_files", g.generate_go2_files)
    monkeypatch.setattr(jm.app, "generate_cdo_files", g.generate_cdo_files)
    r = post(jm, body(customer, sheet, 15))
    assert r.status_code == 500 and "nothing was created" in r.get_json()["error"]
    assert job_rows(jm) == [] and folders_on_disk(jm) == []
    monkeypatch.undo()
    ok = post(jm, body(customer, sheet, 15))                  # same ticket works again, same job number
    assert ok.status_code == 201 and ok.get_json()["job_number"] == "JOB-0001"


def test_failed_job_save_after_sheet_still_keeps_numbers_used(jm, monkeypatch):
    """If the database write at the very end fails, the numbers stay used and the error says so."""
    real = jm.app._insert_rolls
    monkeypatch.setattr(jm.app, "_insert_rolls", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db locked")))
    r = post(jm, body("CDO", "cdo_seq", 16, qty=30))
    assert r.status_code == 500
    monkeypatch.setattr(jm.app, "_insert_rolls", real)
    nxt = post(jm, body("CDO", "cdo_seq", 17, qty=5)).get_json()
    assert nxt["serial_alloc"]["start"] == FIRST + 30


def test_deleting_a_job_never_frees_its_cdo_numbers(jm):
    a = post(jm, body("CDO", "cdo_seq", 18, qty=20)).get_json()
    assert jm.app.app.test_client().delete(f"/api/jobs/{a['job']['id']}").status_code == 200
    b = post(jm, body("CDO", "cdo_seq", 19, qty=5)).get_json()
    assert b["serial_alloc"]["start"] == FIRST + 20


def test_deleting_a_job_never_frees_its_job_number(jm):
    a = post(jm, body("GO2", "go2_rand", 20)).get_json()
    jm.app.app.test_client().delete(f"/api/jobs/{a['job']['id']}")
    assert post(jm, body("GO2", "go2_rand", 21)).get_json()["job_number"] != a["job_number"]


# ── 6. Batch entry (many jobs in one request) ────────────────────────────────
def test_batch_of_cdo_jobs_takes_one_unbroken_series(jm):
    rows = [body("CDO", "cdo_seq", 200 + i, qty=10) for i in range(5)]
    r = jm.app.app.test_client().post("/api/jobs/batch", json={"jobs": rows}).get_json()
    assert r["created"] == 5 and r["failed"] == 0
    assert [(h["start"], h["end"]) for h in jm.SHEETS.history("cdo_seq")] == \
        [(FIRST + 10 * i, FIRST + 10 * i + 9) for i in range(5)]


@pytest.mark.parametrize("junk", ["text", 5, None, [], [1, 2], True])
def test_batch_row_that_is_not_an_object_fails_alone(jm, junk):
    rows = [body("CDO", "cdo_seq", 210, qty=10), junk, body("GO2", "go2_rand", 211, qty=10)]
    res = jm.app.app.test_client().post("/api/jobs/batch", json={"jobs": rows})
    assert res.status_code == 200, res.get_data(as_text=True)[:200]
    out = res.get_json()
    assert [x["ok"] for x in out["results"]] == [True, False, True]


def test_batch_limits(jm):
    c = jm.app.app.test_client()
    assert c.post("/api/jobs/batch", json={"jobs": []}).status_code == 400
    assert c.post("/api/jobs/batch", json={"jobs": "x"}).status_code == 400
    assert c.post("/api/jobs/batch", json={"jobs": [{}] * 101}).status_code == 400
    assert job_rows(jm) == []


def test_batch_bad_row_takes_no_cdo_numbers(jm):
    rows = [body("CDO", "cdo_seq", 220, qty=10), {**body("CDO", "cdo_seq", 221, qty=10), "quantity": "x"},
            body("CDO", "cdo_seq", 222, qty=10)]
    out = jm.app.app.test_client().post("/api/jobs/batch", json={"jobs": rows}).get_json()
    assert [x["ok"] for x in out["results"]] == [True, False, True]
    assert [h["start"] for h in jm.SHEETS.history("cdo_seq")] == [FIRST, FIRST + 10]


# ── 7. What was written is what was recorded ─────────────────────────────────
def test_cdo_sheet_matches_its_rolls_and_its_records(jm):
    j = post(jm, body("CDO", "cdo_seq", 30, qty=100, overage_pct=2, qty_db=60)).get_json()
    total = 102
    assert j["serial_alloc"] == {"sheet": "cdo_seq", "start": FIRST, "end": FIRST + total - 1, "qty": total,
                                 "job_number": j["job_number"]}
    d = os.path.join(j["folder_path"], "Data")
    rows = []
    for f in sorted(os.listdir(d)):
        wb = load_workbook(os.path.join(d, f), read_only=True)
        rows += list(wb.active.iter_rows(min_row=2, values_only=True)); wb.close()
    assert [r[0] for r in rows] == list(range(FIRST, FIRST + total))
    assert all(r[1] == format(r[0], "08X") for r in rows)
    c = jm.app.get_db()
    rolls = c.execute("SELECT * FROM job_rolls WHERE job_id=? ORDER BY roll_number", (j["job"]["id"],)).fetchall()
    c.close()
    assert sum(r["quantity"] for r in rolls) == total
    assert rolls[0]["serial_start"] == FIRST and rolls[-1]["serial_end"] == FIRST + total - 1
    assert all(a["serial_end"] + 1 == b["serial_start"] for a, b in zip(rolls, rolls[1:]))
    log = open(jm.SHEETS._log_path("cdo_seq"), encoding="utf-8").read()
    assert f"{j['job_number']} | {FIRST} | {FIRST + total - 1} | {total}" in log
    assert j["job"]["start_serial"] == FIRST


def test_go2_job_takes_no_upc_serials_and_no_cdo_numbers(jm):
    j = post(jm, body("GO2", "go2_rand", 31, qty=60)).get_json()
    assert j["serial_alloc"] is None and j["job"]["start_serial"] is None
    assert jm.SHEETS.next_start("cdo_seq") == FIRST and jm.SHEETS.history("cdo_seq") == []
    c = jm.app.get_db()
    rolls = c.execute("SELECT serial_start, epc_start FROM job_rolls WHERE job_id=?", (j["job"]["id"],)).fetchall()
    c.close()
    assert len(rolls) == 3 and all(r["serial_start"] is None and r["epc_start"] is None for r in rolls)


def test_go2_epcs_are_unique_across_every_file_of_a_job(jm):
    j = post(jm, body("GO2", "go2_rand", 32, qty=3000, qty_db=700)).get_json()
    d = os.path.join(j["folder_path"], "Data")
    epcs = []
    for f in sorted(os.listdir(d)):
        wb = load_workbook(os.path.join(d, f), read_only=True)
        epcs += [r[2] for r in wb.active.iter_rows(min_row=2, values_only=True)]; wb.close()
    assert len(epcs) == 3000 == len(set(epcs))
    assert all(re.fullmatch(r"E2004[0-9A-F]{19}", e) for e in epcs)


def test_regenerate_never_changes_a_sheet_job_data_files(jm):
    j = post(jm, body("CDO", "cdo_seq", 33, qty=40)).get_json()
    d = os.path.join(j["folder_path"], "Data")
    before = {f: open(os.path.join(d, f), "rb").read() for f in os.listdir(d)}
    assert jm.app.app.test_client().post(f"/api/jobs/{j['job']['id']}/regenerate").status_code == 200
    assert {f: open(os.path.join(d, f), "rb").read() for f in os.listdir(d)} == before
    assert jm.SHEETS.history("cdo_seq")[0]["qty"] == 40
