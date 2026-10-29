"""CDO data sheet: sequential serial + 8-hex EPC, one running series, never repeats."""
import os
import threading

import pytest
from openpyxl import load_workbook

FIRST = 3_768_677_565


def _rows(path):
    wb = load_workbook(path, read_only=True)
    try:
        return list(wb.active.iter_rows(min_row=2, values_only=True))
    finally:
        wb.close()


def _files(res):
    d = os.path.join(res["folder_path"], "Data")
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".xlsx"))


def _cdo(jm, n, qty, **kw):
    return jm.app.create_job({"customer": "CDO", "title": f"Tag {n}", "job_ticket": f"C{n}", "job_type": "special",
                              "data_sheet": "cdo_seq", "quantity": qty, "lpr": 500, **kw})


def test_epc_is_dec2hex_8():
    import generators as g
    assert g.cdo_epc(3768662565) == "E0A13A25"          # first row of the sample sheet
    assert g.cdo_epc(3768677564) == "E0A174BC"          # last row
    assert g.cdo_epc(255) == "000000FF"


def test_first_job_continues_after_the_sample_sheet(jm):
    res, code = _cdo(jm, 1, 1000)
    assert code == 201, res
    rows = _rows(_files(res)[0])
    assert len(rows) == 1000
    assert rows[0] == (FIRST, format(FIRST, "08X"))
    assert [r[0] for r in rows] == list(range(FIRST, FIRST + 1000))
    assert all(r[1] == format(r[0], "08X") for r in rows)
    assert res["job"]["start_serial"] == FIRST and res["job"]["upc"] is None


def test_values_not_formulas(jm):
    res, _ = _cdo(jm, 2, 20)
    wb = load_workbook(_files(res)[0])
    assert not any(isinstance(c.value, str) and c.value.startswith("=") for row in wb.active.iter_rows(min_row=2) for c in row)


def test_second_job_never_repeats_numbers(jm):
    a, _ = _cdo(jm, 3, 300, overage_pct=2)                       # 306 numbers
    b, code = _cdo(jm, 4, 100)
    assert code == 201, b
    sa = [r[0] for r in _rows(_files(a)[0])]
    sb = [r[0] for r in _rows(_files(b)[0])]
    assert sa[-1] + 1 == sb[0] and not set(sa) & set(sb) and len(sa) == 306


def test_split_files_stay_in_one_series(jm):
    res, code = _cdo(jm, 5, 1000, qty_db=400)
    assert code == 201, res
    allr = [r for f in _files(res) for r in _rows(f)]
    assert [len(_rows(f)) for f in _files(res)] == [400, 400, 200]
    assert [r[0] for r in allr] == list(range(FIRST, FIRST + 1000))


def test_rolls_carry_serial_and_epc_ranges(jm):
    res, _ = _cdo(jm, 6, 1000, lpr=400)
    conn = jm.app.get_db()
    rolls = [dict(r) for r in conn.execute("SELECT * FROM job_rolls WHERE job_id=? ORDER BY roll_number", (res["job"]["id"],))]
    conn.close()
    assert [(r["serial_start"], r["serial_end"], r["epc_start"]) for r in rolls][0] == (FIRST, FIRST + 399, format(FIRST, "08X"))
    assert rolls[-1]["serial_end"] == FIRST + 999


def test_threads_never_get_the_same_number(jm):
    import sheet_serials as S
    got, errs = [], []
    def worker(i):
        try:
            got.append(S.reserve("cdo_seq", f"T{i}", 50))
        except Exception as e:                          # pragma: no cover
            errs.append(e)
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errs
    nums = [n for a in got for n in range(a["start"], a["end"] + 1)]
    assert len(nums) == len(set(nums)) == 600


def test_series_survives_losing_one_record(jm):
    """The next number is past the highest end in ANY of database, log, journal."""
    a, _ = _cdo(jm, 7, 100)
    end = FIRST + 99
    conn = jm.app.get_db(); conn.execute("DELETE FROM sheet_serials"); conn.commit(); conn.close()   # database rolled back
    assert jm.SHEETS.next_start("cdo_seq") == end + 1
    os.remove(jm.SHEETS._log_path("cdo_seq"))                                                       # log gone too
    assert jm.SHEETS.next_start("cdo_seq") == end + 1                                               # journal remembers


def test_failed_sheet_leaves_a_gap_never_a_repeat(jm, monkeypatch):
    import generators as g
    def boom(*a, **k): raise ValueError("disk full")
    monkeypatch.setattr(g, "verify_cdo_file", boom)
    monkeypatch.setattr(jm.app, "generate_cdo_files", g.generate_cdo_files)
    res, code = _cdo(jm, 8, 100)
    assert code == 500 and "nothing was created" in res["error"]
    monkeypatch.undo()
    ok, code = _cdo(jm, 9, 10)
    assert code == 201
    assert _rows(_files(ok)[0])[0][0] == FIRST + 100        # the 100 burned numbers are skipped, not reused
    hist = jm.SHEETS.history("cdo_seq")
    assert hist[0]["note"].startswith("VOID")


def test_running_out_of_hex_room(jm):
    import sheet_serials as S
    S.reserve("cdo_seq", "BIG", 0xFFFFFFFF - FIRST - 5)         # leaves 6 numbers
    res, code = _cdo(jm, 10, 10)
    assert code == 400 and "largest number" in res["error"]


@pytest.mark.parametrize("name", ["CDO", "cdo", "Cdo", " cDo "])
def test_any_capitalization_is_one_customer(jm, name):
    res, code = _cdo(jm, 11, 10, customer=name)
    assert code == 201 and res["job"]["customer"] == "CDO"


@pytest.mark.parametrize("sheet,customer", [("cdo_seq", "GO2"), ("go2_rand", "CDO"), ("cdo_seq", "Acme"), ("cdo_seq", "CDO2")])
def test_sheets_belong_to_their_customer(jm, sheet, customer):
    res, code = jm.app.create_job({"customer": customer, "title": "x", "job_ticket": "X1", "job_type": "special",
                                   "data_sheet": sheet, "quantity": 10})
    assert code == 400
    assert jm.SHEETS.next_start("cdo_seq") == FIRST          # a refused job takes no numbers


def test_cdo_standard_job_cannot_take_it(jm):
    res, code = jm.app.create_job({"customer": "CDO", "title": "x", "job_ticket": "X2", "upc": jm.upc(3),
                                   "quantity": 10, "data_sheet": "cdo_seq"})
    assert code == 400


def test_meta_lists_both_sheets(jm):
    r = jm.app.app.test_client().get("/api/meta").get_json()
    assert {d["key"] for d in r["data_sheets"]} == {"go2_rand", "cdo_seq"} and r["cdo_next"] == FIRST
