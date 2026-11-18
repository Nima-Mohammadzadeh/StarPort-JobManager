"""Venture Research data sheet: 34083328448A2E + 10-digit serial, one running series for every
label size, never repeats, and never reuses a number already in a sheet on the share."""
import os
import csv
import threading

import pytest
from openpyxl import Workbook, load_workbook

PREFIX = "34083328448A2E"
FIRST = 380_021                           # 1998128.xlsx (the last sheet on the share) ended at 380020
REAL = r"Z:\3 Encoding and Printing Files\Customers Encoding Files\Venture Research\4 x 2\1998128.xlsx"


def _rows(path):
    wb = load_workbook(path, read_only=True)
    try:
        return list(wb.active.iter_rows(values_only=True))
    finally:
        wb.close()


def _files(res):
    d = os.path.join(res["folder_path"], "Data")
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".xlsx"))


def _data(res):
    return [r for f in _files(res) for r in _rows(f)[1:]]


def _vr(jm, n, qty, **kw):
    body = {"customer": "Venture Research", "title": f"Asset tag {n}", "job_ticket": f"V{n}", "job_type": "special",
            "data_sheet": "venture_seq", "quantity": qty, "lpr": 500, "label_size": "4 x 2", **kw}
    return jm.app.create_job(body)


def _share(jm, *parts):
    p = os.path.join(jm.customers, "Venture Research", *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def _hand_made(path, start, count, epc_only=False, serial_as_number=False):
    """A sheet like the ones made by hand in Excel."""
    wb = Workbook()
    ws = wb.active
    ws.append(["EPC"] if epc_only else ["Prefix", "Serial", "EPC"])
    for n in range(start, start + count):
        s = f"{n:010d}"
        ws.append([PREFIX + s] if epc_only else [PREFIX, n if serial_as_number else s, PREFIX + s])
    wb.save(path)


# ── the sheet itself ──────────────────────────────────────────────────────────
def test_epc_is_prefix_plus_ten_digits():
    import generators as g
    assert g.venture_epc(360021) == "34083328448A2E0000360021"      # first row of 1998128.xlsx
    assert g.venture_epc(380020) == "34083328448A2E0000380020"      # last row
    assert len(g.venture_epc(9_999_999_999)) == 24


def test_reading_a_row_counts_one_tag_once():
    import generators as g
    assert g.venture_serials_in_row((PREFIX, "0000000005", PREFIX + "0000000005")) == [5]
    assert g.venture_serials_in_row((PREFIX, 7.0, None)) == [7]
    assert g.venture_serials_in_row(("E2004", "0000000005", "E20040000000005")) == []


@pytest.mark.skipif(not os.path.isfile(REAL), reason="the Venture Research share isn't reachable")
def test_same_layout_as_the_real_sheet(tmp_path):
    import generators as g
    real = load_workbook(REAL, read_only=True, data_only=True)
    try:
        head = list(real.active.iter_rows(min_row=1, max_row=3, values_only=True))
    finally:
        real.close()
    out = g.generate_venture_files(str(tmp_path), "X", 360021, 2, None)
    mine = _rows(out["files"][0])
    assert mine[0] == head[0] == ("Prefix", "Serial", "EPC")
    assert mine[1:] == [tuple(r[:3]) for r in head[1:]]


def test_first_job_continues_after_the_last_sheet_on_the_share(jm):
    res, code = _vr(jm, 1, 1000)
    assert code == 201, res
    rows = _rows(_files(res)[0])
    assert rows[0] == ("Prefix", "Serial", "EPC")
    assert rows[1] == (PREFIX, "0000380021", PREFIX + "0000380021")
    assert len(rows) == 1001 and rows[-1] == (PREFIX, "0000381020", PREFIX + "0000381020")
    assert res["job"]["start_serial"] == FIRST and res["job"]["upc"] is None
    assert res["job"]["customer"] == "Venture Research"
    assert res["folder_path"].split(os.sep)[-3:-1] == ["Venture Research", "4 x 2"]


def test_values_not_formulas_and_serial_keeps_its_zeros(jm):
    res, _ = _vr(jm, 2, 20)
    wb = load_workbook(_files(res)[0])
    cells = [c.value for row in wb.active.iter_rows(min_row=2) for c in row]
    assert not any(isinstance(v, str) and v.startswith("=") for v in cells)
    assert all(isinstance(r[1], str) and len(r[1]) == 10 for r in _data(res))


def test_second_job_never_repeats_numbers(jm):
    a, _ = _vr(jm, 3, 300, overage_pct=2)                       # 306 numbers
    b, code = _vr(jm, 4, 100, label_size="6 x 2")               # other label sizes share the series
    assert code == 201, b
    sa = [int(r[1]) for r in _data(a)]
    sb = [int(r[1]) for r in _data(b)]
    assert sa[-1] + 1 == sb[0] and not set(sa) & set(sb) and len(sa) == 306


def test_split_files_stay_in_one_series(jm):
    res, code = _vr(jm, 5, 1000, qty_db=400)
    assert code == 201, res
    assert [len(_rows(f)) - 1 for f in _files(res)] == [400, 400, 200]
    assert [int(r[1]) for r in _data(res)] == list(range(FIRST, FIRST + 1000))


def test_rolls_carry_serial_and_epc_ranges(jm):
    res, _ = _vr(jm, 6, 1000, lpr=400)
    conn = jm.app.get_db()
    rolls = [dict(r) for r in conn.execute("SELECT * FROM job_rolls WHERE job_id=? ORDER BY roll_number", (res["job"]["id"],))]
    conn.close()
    assert (rolls[0]["serial_start"], rolls[0]["serial_end"], rolls[0]["epc_start"], rolls[0]["epc_end"]) == \
        (FIRST, FIRST + 399, PREFIX + "0000380021", PREFIX + "0000380420")
    assert rolls[-1]["serial_end"] == FIRST + 999


def test_threads_never_get_the_same_number(jm):
    import sheet_serials as S
    got, errs = [], []

    def worker(i):
        try:
            got.append(S.reserve("venture_seq", f"T{i}", 50))
        except Exception as e:                          # pragma: no cover
            errs.append(e)
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errs
    nums = [n for a in got for n in range(a["start"], a["end"] + 1)]
    assert len(nums) == len(set(nums)) == 600


def test_failed_sheet_leaves_a_gap_never_a_repeat(jm, monkeypatch):
    import generators as g

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(jm.app, "generate_venture_files", boom)
    res, code = _vr(jm, 7, 100)
    assert code == 500
    monkeypatch.setattr(jm.app, "generate_venture_files", g.generate_venture_files)
    res, code = _vr(jm, 8, 10)
    assert code == 201 and res["job"]["start_serial"] == FIRST + 100
    assert jm.SHEETS.history("venture_seq")[0]["note"].startswith("VOID")


def test_add_data_continues_the_series(jm):
    a, _ = _vr(jm, 9, 1000)
    out, code = jm.app.add_data(a["job"]["id"], {"quantity": 200, "reason": "short run", "added_by": "Sam"})
    assert code == 201, out
    ad = out["addition"]
    assert ad["serial_start"] == FIRST + 1000 and ad["serial_end"] == FIRST + 1199
    got = [r for r in _data(a) if int(r[1]) >= ad["serial_start"]]
    assert [r[2] for r in got] == [PREFIX + f"{n:010d}" for n in range(FIRST + 1000, FIRST + 1200)]
    b, _ = _vr(jm, 10, 10)
    assert b["job"]["start_serial"] == FIRST + 1200


# ── sheets made by hand on the share ─────────────────────────────────────────
def test_hand_made_sheet_on_the_share_is_never_overlapped(jm):
    _hand_made(_share(jm, "4 x 3.125", "05.01.26 - 2000001", "Data", "2000001 Database.xlsx"), 500_000, 50)
    res, code = _vr(jm, 11, 10)
    assert code == 201, res
    assert res["job"]["start_serial"] == 500_050


@pytest.mark.parametrize("kind", ["epc_only", "serial_as_number", "csv"])
def test_hand_made_sheets_in_other_shapes(jm, kind):
    if kind == "csv":
        with open(_share(jm, "loose.csv"), "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["Prefix", "Serial", "EPC"])
            w.writerow([PREFIX, "0000600000", PREFIX + "0000600000"])
    else:
        _hand_made(_share(jm, "6 x 2", "x.xlsx"), 599_991, 10, **{kind: True})
    assert jm.SHEETS.next_start("venture_seq") == 600_001


def test_other_customers_numbers_on_the_share_are_ignored(jm):
    wb = Workbook(); ws = wb.active
    ws.append(["Prefix", "Serial", "EPC"]); ws.append(["E2004", "0000900000", "E20040000900000"])
    ws.append(["3408332844FFFF", "0000900000", "3408332844FFFF0000900000"])
    wb.save(_share(jm, "other.xlsx"))
    assert jm.SHEETS.next_start("venture_seq") == FIRST


def test_removing_a_sheet_does_not_lower_the_series(jm):
    p = _share(jm, "old.xlsx")
    _hand_made(p, 700_000, 5)
    assert jm.SHEETS.next_start("venture_seq") == 700_005
    os.remove(p)
    assert jm.SHEETS.next_start("venture_seq") == 700_005


def test_unreadable_file_on_the_share_stops_the_job(jm):
    with open(_share(jm, "broken.xlsx"), "wb") as fh:
        fh.write(b"not really a spreadsheet")
    res, code = _vr(jm, 12, 10)
    assert code == 400 and "broken.xlsx" in res["error"] and "can't be read" in res["error"]
    assert jm.SHEETS.history("venture_seq") == []                  # nothing taken
    os.remove(_share(jm, "broken.xlsx"))
    res, code = _vr(jm, 13, 10)
    assert code == 201 and res["job"]["start_serial"] == FIRST


def test_excel_lock_files_are_skipped(jm):
    with open(_share(jm, "~$1998128.xlsx"), "wb") as fh:
        fh.write(b"owner file Excel leaves while a sheet is open")
    res, code = _vr(jm, 14, 10)
    assert code == 201, res


def test_unchanged_files_are_not_read_again(jm, monkeypatch):
    _hand_made(_share(jm, "a.xlsx"), 400_000, 5)
    S = jm.SHEETS
    assert S.next_start("venture_seq") == 400_005
    calls = []
    real = S._serials_in_file
    monkeypatch.setattr(S, "_serials_in_file", lambda *a: calls.append(a) or real(*a))
    assert S.next_start("venture_seq") == 400_005 and calls == []
    _hand_made(_share(jm, "b.xlsx"), 450_000, 5)
    assert S.next_start("venture_seq") == 450_005 and len(calls) == 1


# ── the customer, however it is typed ────────────────────────────────────────
@pytest.mark.parametrize("name", ["Venture Research", "venture research", "VENTURE", "venture", "Venture Research Inc.",
                                  "Venture Research, LLC", "venture-research", "Ventrue Research", "Ventur Research"])
def test_any_way_of_writing_venture_is_one_customer(jm, name):
    res, code = _vr(jm, 15, 10, customer=name)
    assert code == 201, res
    assert res["job"]["customer"] == "Venture Research"
    assert res["folder_path"].split(os.sep)[-3] == "Venture Research"


@pytest.mark.parametrize("customer", ["Acme", "Vantage ID", "Adventure", "GO2", "CDO"])
def test_the_sheet_belongs_to_venture(jm, customer):
    res, code = _vr(jm, 16, 10, customer=customer)
    assert code == 400
    assert jm.SHEETS.history("venture_seq") == []


def test_customer_match_api(jm):
    c = jm.app.app.test_client()
    r = c.get("/api/customer-match?name=ventrue research inc").get_json()
    assert (r["customer"], r["sheet"], r["how"], r["folder"]) == ("Venture Research", "venture_seq", "spelling", "Venture Research")
    r = c.get("/api/customer-match?name=go 2 partners").get_json()
    assert (r["customer"], r["sheet"], r["folder"]) == ("GO2", "go2_rand", "Go2Partners")
    r = c.get("/api/customer-match?name=G2 ID Source").get_json()
    assert r["customer"] is None and r["sheet"] is None


def test_editing_a_job_keeps_one_spelling(jm):
    res, _ = jm.app.create_job({"customer": "Acme", "title": "x", "job_ticket": "E1", "job_type": "special"})
    c = jm.app.app.test_client()
    r = c.patch(f"/api/jobs/{res['job']['id']}", json={"customer": "venture research inc"})
    assert r.status_code == 200, r.get_json()
    conn = jm.app.get_db()
    assert conn.execute("SELECT customer FROM jobs WHERE id=?", (res["job"]["id"],)).fetchone()[0] == "Venture Research"
    conn.close()


def test_meta_gives_the_next_venture_serial(jm):
    r = jm.app.app.test_client().get("/api/meta").get_json()
    assert r["sheet_next"]["venture_seq"] == FIRST
    d = next(x for x in r["data_sheets"] if x["key"] == "venture_seq")
    assert d["customer"] == "Venture Research" and d["sequential"] and "34083328448A2E" in d["hint"]
