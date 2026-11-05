"""Add data to an existing job: new serials, rolls and data files; serial safety nets know about them."""
import os
import threading

import pytest
from openpyxl import load_workbook


def rows(path):
    wb = load_workbook(path, read_only=True)
    try:
        return list(wb.active.iter_rows(min_row=2, values_only=True))
    finally:
        wb.close()


def data_files(job_dir):
    d = os.path.join(job_dir, "Data")
    return sorted(f for f in os.listdir(d) if f.endswith((".xlsx", ".csv")))


def db(jm):
    return jm.app.get_db()


def add_row(jm, job_id, seq, qty, s, e, first_roll, last_roll):
    c = db(jm)
    c.execute("INSERT INTO job_additions (job_id, seq, quantity, serial_start, serial_end, first_roll, last_roll,"
              " created_at) VALUES (?,?,?,?,?,?,?,'2026-01-01 00:00:00')", (job_id, seq, qty, s, e, first_roll, last_roll))
    c.commit(); c.close()


# ── Task 1: schema, helpers, safety nets ──────────────────────────────────────
def test_schema_exists(jm):
    c = db(jm)
    cols = [r[1] for r in c.execute("PRAGMA table_info(job_additions)")]
    assert {"job_id", "seq", "quantity", "serial_start", "serial_end", "first_roll", "last_roll",
            "files", "reason", "added_by", "created_at"} <= set(cols)
    assert "addition_id" in [r[1] for r in c.execute("PRAGMA table_info(job_rolls)")]
    c.close()


def test_serial_store_safety_nets_include_additions(jm):
    res = jm.job(1)
    upc = res["job"]["upc"]
    s = res["job"]["start_serial"] + 5_000_000
    add_row(jm, res["job"]["id"], 1, 1000, s, s + 999, 4, 23)
    c = jm.SS._db()
    assert jm.SS._jobs_last_end(c, upc) == s + 999
    assert {"job_number": res["job_number"], "start": s, "end": s + 999} in jm.SS._jobs_ranges(c, upc)
    c.close()
    assert jm.SS.next_start(upc) == s + 1000
    assert not jm.SS.range_conflicts(upc, s + 500, 10)["ok"]


def test_total_and_ranges_helpers(jm):
    res = jm.job(2)                                  # 100 + 2% = 102 labels
    job = res["job"]
    add_row(jm, job["id"], 1, 1000, 9_000_000, 9_000_999, 4, 23)
    c = db(jm)
    j = dict(c.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone())
    assert jm.app.job_total_qty(c, j) == 1102
    rg = jm.app.job_serial_ranges(c, j)
    c.close()
    assert [(r["start"], r["end"]) for r in rg] == [(job["start_serial"], job["start_serial"] + 101), (9_000_000, 9_000_999)]


# ── Task 2: standard (UPC) jobs ───────────────────────────────────────────────
def add(jm, job_id, qty=1000, **kw):
    return jm.app.add_data(job_id, {"quantity": qty, "reason": "short run", "added_by": "Sam", **kw})


def rolls_of(jm, job_id):
    c = db(jm)
    try:
        return [dict(r) for r in c.execute("SELECT * FROM job_rolls WHERE job_id=? ORDER BY roll_number", (job_id,))]
    finally:
        c.close()


def test_standard_addition_contiguous(jm):
    res = jm.job(10)                                 # 102 labels, 3 rolls of 50
    job = res["job"]
    before = data_files(res["folder_path"])
    out, code = add(jm, job["id"], 1000)
    assert code == 201, out
    a = out["addition"]
    assert a["serial_start"] == job["start_serial"] + 102 and a["serial_end"] == a["serial_start"] + 999
    rl = rolls_of(jm, job["id"])
    assert [r["roll_number"] for r in rl] == list(range(1, 24))      # 3 original + 20 new
    new = [r for r in rl if r["addition_id"] == a["id"]]
    assert (new[0]["roll_number"], new[-1]["roll_number"]) == (4, 23)
    assert new[0]["serial_start"] == a["serial_start"] and new[-1]["serial_end"] == a["serial_end"]
    assert all(r["status"] == "pending" for r in new)
    assert (a["first_roll"], a["last_roll"]) == (4, 23)
    names = data_files(res["folder_path"])
    assert set(before) <= set(names) and len(names) == len(before) + 1
    added = [n for n in names if "_ADD1" in n]
    assert len(added) == 1
    got = rows(os.path.join(res["folder_path"], "Data", added[0]))
    assert len(got) == 1000 and got[0][0] == a["serial_start"] and got[-1][0] == a["serial_end"]
    assert got[0][2] == __import__("sys").modules["generators"].generate_epc(job["upc"], a["serial_start"])


def test_non_contiguous_addition(jm):
    upc = jm.upc(11)
    a = jm.job(11, upc=upc, ticket="NA")
    b = jm.job(12, upc=upc, ticket="NB")             # takes the numbers right after A
    out, code = add(jm, a["job"]["id"], 500)
    assert code == 201, out
    s, e = out["addition"]["serial_start"], out["addition"]["serial_end"]
    assert s == b["job"]["start_serial"] + 102
    assert jm.SS.range_conflicts(upc, s, 500)["ok"] is False         # now taken
    nxt = jm.job(13, upc=upc, ticket="NC")
    assert nxt["job"]["start_serial"] == e + 1
    c = db(jm)
    j = dict(c.execute("SELECT * FROM jobs WHERE id=?", (a["job"]["id"],)).fetchone())
    ranges = jm.app.job_serial_ranges(c, j); c.close()
    assert len(ranges) == 2 and ranges[1]["start"] == s


def test_addition_recorded_in_registry_mirror_journal(jm):
    res = jm.job(14)
    out, code = add(jm, res["job"]["id"], 300)
    a = out["addition"]; upc = res["job"]["upc"]
    assert (a["serial_start"], a["serial_end"]) in [(r["start"], r["end"]) for r in jm.R.read_records(upc)]
    c = jm.SS._db()
    mir = [(r["start"], r["end"], r["registry_state"]) for r in c.execute(
        'SELECT start, "end", registry_state FROM serial_allocations WHERE upc=?', (upc,))]
    c.close()
    assert (a["serial_start"], a["serial_end"], 1) in mir
    with open(jm.SS.journal_path(), encoding="utf-8") as fh:
        assert any(str(a["serial_start"]) in ln for ln in fh)


def test_preview_reserves_nothing(jm):
    res = jm.job(15)
    upc, jid = res["job"]["upc"], res["job"]["id"]
    nxt = jm.SS.next_start(upc); files = data_files(res["folder_path"])
    out, code = add(jm, jid, 700, preview=True)
    assert code == 200 and out["preview"] is True
    assert out["serial_start"] == nxt and out["serial_end"] == nxt + 699
    assert out["first_roll"] == 4 and out["new_total"] == 102 + 700
    assert jm.SS.next_start(upc) == nxt and data_files(res["folder_path"]) == files
    assert len(rolls_of(jm, jid)) == 3
    c = db(jm); assert c.execute("SELECT COUNT(*) FROM job_additions").fetchone()[0] == 0; c.close()


def test_completed_job_reopens(jm):
    res = jm.job(16); jid = res["job"]["id"]
    c = db(jm)
    c.execute("UPDATE job_rolls SET status='complete' WHERE job_id=?", (jid,))
    c.execute("UPDATE jobs SET status='Complete', completed_at='2026-01-01 00:00:00' WHERE id=?", (jid,))
    c.commit(); c.close()
    out, code = add(jm, jid, 100)
    assert code == 201, out
    c = db(jm); j = c.execute("SELECT status, completed_at FROM jobs WHERE id=?", (jid,)).fetchone()
    act = [r["message"] for r in c.execute("SELECT message FROM activity WHERE job_id=? AND kind='data_added'", (jid,))]
    c.close()
    assert j["status"] == "In Progress" and j["completed_at"] is None
    st = [r["status"] for r in rolls_of(jm, jid)]
    assert st[:3] == ["complete"] * 3 and set(st[3:]) == {"pending"}
    assert len(act) == 1 and "100" in act[0] and "short run" in act[0]


def test_archived_refused_409(jm):
    res = jm.job(17); jid = res["job"]["id"]
    c = db(jm); c.execute("UPDATE jobs SET archived_at='2026-01-01 00:00:00' WHERE id=?", (jid,)); c.commit(); c.close()
    out, code = add(jm, jid, 100)
    assert code == 409 and "error" in out
    assert len(rolls_of(jm, jid)) == 3


@pytest.mark.parametrize("bad", [0, -5, "abc", 1_000_001, None, "", 12.5e9])
def test_bad_quantity_400(jm, bad):
    res = jm.job(18)
    out, code = add(jm, res["job"]["id"], bad)
    assert code == 400 and "error" in out


def test_unknown_job_404(jm):
    assert add(jm, 9999, 10)[1] == 404


def test_last_original_roll_not_topped_up(jm):
    res = jm.job(19, qty=75)                         # 76 labels incl 2%: rolls 50 + 26
    jid = res["job"]["id"]
    before = rolls_of(jm, jid)
    assert before[-1]["quantity"] == 26
    add(jm, jid, 100)
    after = rolls_of(jm, jid)
    assert after[:2] == before
    assert [r["quantity"] for r in after[2:]] == [50, 50]


def test_failed_file_write_keeps_serials_used(jm, monkeypatch):
    res = jm.job(20); jid = res["job"]["id"]; upc = res["job"]["upc"]
    nxt = jm.SS.next_start(upc)
    def boom(*a, **k): raise RuntimeError("disk full")
    monkeypatch.setattr(jm.app, "generate_db_files", boom)
    out, code = add(jm, jid, 400)
    assert code == 500 and "error" in out
    c = db(jm); assert c.execute("SELECT COUNT(*) FROM job_additions").fetchone()[0] == 0; c.close()
    assert len(rolls_of(jm, jid)) == 3
    assert not [n for n in data_files(res["folder_path"]) if "_ADD" in n]
    assert jm.SS.next_start(upc) == nxt + 400        # serials stay taken, never handed out again


def test_unreachable_folder_503(jm):
    res = jm.job(21); jid = res["job"]["id"]; upc = res["job"]["upc"]
    nxt = jm.SS.next_start(upc)
    c = db(jm); c.execute("UPDATE jobs SET folder_path=? WHERE id=?", (os.path.join(jm.root, "nowhere"), jid)); c.commit(); c.close()
    out, code = add(jm, jid, 100)
    assert code == 503 and jm.SS.next_start(upc) == nxt


def test_split_into_db_files(jm):
    res, code = jm.app.create_job({"customer": "Acme", "title": "Split", "job_ticket": "SP1", "upc": jm.upc(22),
                                   "quantity": 100, "lpr": 50, "qty_db": 60, "overage_pct": 0})
    assert code == 201
    out, code = add(jm, res["job"]["id"], 130)
    assert code == 201 and len(out["files"]) == 3 and all("_ADD1_DB" in f for f in out["files"])


# ── Task 3: CDO and GO2 jobs ──────────────────────────────────────────────────
def special(jm, sheet, customer, n, qty, **kw):
    res, code = jm.app.create_job({"customer": customer, "title": f"Tag {n}", "job_ticket": f"S{n}", "job_type": "special",
                                   "data_sheet": sheet, "quantity": qty, "lpr": 500, **kw})
    assert code == 201, res
    return res


def sheet_rows(res):
    d = os.path.join(res["folder_path"], "Data")
    out = []
    for f in sorted(os.listdir(d)):
        if f.endswith(".xlsx"):
            out += rows(os.path.join(d, f))
    return out


def test_cdo_addition_continues_series(jm):
    a = special(jm, "cdo_seq", "CDO", 1, 1000)
    out, code = add(jm, a["job"]["id"], 200)
    assert code == 201, out
    ad = out["addition"]
    assert ad["serial_start"] == a["job"]["start_serial"] + 1000 and ad["serial_end"] == ad["serial_start"] + 199
    b = special(jm, "cdo_seq", "CDO", 2, 100)
    assert b["job"]["start_serial"] == ad["serial_end"] + 1
    got = [r for r in sheet_rows(a) if r[0] >= ad["serial_start"]]
    assert [r[0] for r in got] == list(range(ad["serial_start"], ad["serial_end"] + 1))
    assert all(r[1] == format(r[0], "08X") for r in got)
    rl = rolls_of(jm, a["job"]["id"])
    assert [r["roll_number"] for r in rl] == [1, 2, 3] and rl[2]["addition_id"] == ad["id"]
    assert rl[2]["epc_start"] == format(rl[2]["serial_start"], "08X") and rl[1]["addition_id"] is None


def test_cdo_failed_write_voids(jm, monkeypatch):
    a = special(jm, "cdo_seq", "CDO", 3, 500)
    def boom(*x, **k): raise RuntimeError("disk full")
    monkeypatch.setattr(jm.app, "generate_cdo_files", boom)
    out, code = add(jm, a["job"]["id"], 100)
    assert code == 500
    c = db(jm); assert c.execute("SELECT COUNT(*) FROM job_additions").fetchone()[0] == 0; c.close()
    monkeypatch.undo()
    b = special(jm, "cdo_seq", "CDO", 4, 10)
    assert b["job"]["start_serial"] == a["job"]["start_serial"] + 600       # the 100 stay used, never reissued


def test_go2_addition_unique_epcs(jm):
    a = special(jm, "go2_rand", "GO2", 5, 300)
    out, code = add(jm, a["job"]["id"], 200)
    assert code == 201, out
    ad = out["addition"]
    assert ad["serial_start"] is None and ad["serial_end"] is None
    epcs = [r[2] for r in sheet_rows(a)]
    assert len(epcs) == 500 and len(set(epcs)) == 500 and all(e.startswith("E2004") for e in epcs)
    assert any("_ADD1" in f for f in out["files"])
    rl = rolls_of(jm, a["job"]["id"])
    assert rl[-1]["serial_start"] is None and rl[-1]["addition_id"] == ad["id"]


def test_go2_new_epcs_avoid_existing(jm, monkeypatch):
    a = special(jm, "go2_rand", "GO2", 6, 50)
    existing = jm.app.read_go2_epcs(a["folder_path"])
    assert len(existing) == 50
    seen = {}
    real = jm.app.generate_go2_files
    def spy(*args, **kw):
        seen["exclude"] = set(kw.get("exclude") or ()); return real(*args, **kw)
    monkeypatch.setattr(jm.app, "generate_go2_files", spy)
    assert add(jm, a["job"]["id"], 20)[1] == 201
    assert seen["exclude"] == existing


def test_special_job_without_data_refused(jm):
    res, code = jm.app.create_job({"customer": "Acme", "title": "Plain", "job_ticket": "PL1", "job_type": "special",
                                   "quantity": 100, "lpr": 50})
    assert code == 201
    out, code = add(jm, res["job"]["id"], 10)
    assert code == 400 and "nothing to add" in out["error"]


def test_concurrent_adds_get_distinct_ranges(jm):
    res = jm.job(30); jid = res["job"]["id"]
    results = []
    def go(): results.append(add(jm, jid, 120))
    ts = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert [c for _, c in results] == [201] * 4, results
    ads = sorted((o["addition"]["serial_start"], o["addition"]["serial_end"]) for o, _ in results)
    assert all(ads[i][1] < ads[i + 1][0] for i in range(3))
    nums = [r["roll_number"] for r in rolls_of(jm, jid)]
    assert nums == list(range(1, len(nums) + 1))
    assert sorted(o["addition"]["seq"] for o, _ in results) == [1, 2, 3, 4]


# ── Task 4: API ───────────────────────────────────────────────────────────────
def test_route_201_and_detail_lists_addition(jm):
    res = jm.job(40); jid = res["job"]["id"]
    cl = jm.app.app.test_client()
    r = cl.post(f"/api/jobs/{jid}/additions", json={"quantity": 400, "reason": "reprint"}, headers={"X-Operator": "Pat"})
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["addition"]["added_by"] == "Pat"
    d = cl.get(f"/api/jobs/{jid}").get_json()
    assert d["total_qty"] == 502 and len(d["additions"]) == 1 and len(d["rolls"]) == 3 + 8
    assert [x["label"] for x in d["serial_ranges"]] == ["Original", "Added #1"]
    assert d["end_serial"] == d["serial_ranges"][-1]["end"]
    assert d["job"]["quantity"] == 100                           # the original order is untouched


def test_route_preview_and_errors(jm):
    res = jm.job(41); jid = res["job"]["id"]
    cl = jm.app.app.test_client()
    p = cl.post(f"/api/jobs/{jid}/additions", json={"quantity": 50, "preview": True})
    assert p.status_code == 200 and p.get_json()["preview"] is True
    assert cl.get(f"/api/jobs/{jid}").get_json()["additions"] == []
    assert cl.post(f"/api/jobs/{jid}/additions", json={"quantity": 0}).status_code == 400
    assert cl.post("/api/jobs/9999/additions", json={"quantity": 5}).status_code == 404
    assert cl.post(f"/api/jobs/{jid}/additions", data="not json").status_code == 400


def test_detail_without_additions_unchanged(jm):
    res = jm.job(42)
    d = jm.app.app.test_client().get(f"/api/jobs/{res['job']['id']}").get_json()
    assert d["total_qty"] == 102 and d["additions"] == []
    assert d["end_serial"] == res["job"]["start_serial"] + 101
