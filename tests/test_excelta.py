"""Excelta Corporation data sheet: leading | filler | serial | barcode hr | rfid & barcode, one never-repeating series."""
import os
import threading

from openpyxl import load_workbook

FIRST = 15_035                      # their proof used 00015034


def _rows(path):
    wb = load_workbook(path, read_only=True)
    try:
        return list(wb.active.iter_rows(min_row=1, values_only=True))
    finally:
        wb.close()


def _files(res):
    d = os.path.join(res["folder_path"], "Data")
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".xlsx"))


def _x(jm, n, qty, **kw):
    return jm.app.create_job({"customer": "excelta corporation", "title": f"Tag {n}", "job_ticket": f"X{n}",
                              "job_type": "special", "data_sheet": "excelta_seq", "quantity": qty, "lpr": 500, **kw})


def test_row_matches_their_proof():
    import generators as g
    assert g.excelta_row(15034) == [2853, "-0000-0000-0000-", "00015034",
                                    "2853-0000-0000-0000-0001-5034", "285300000000000000015034"]


def test_job_continues_after_the_proof_and_never_repeats(jm):
    a, code = _x(jm, 1, 1000)
    assert code == 201, a
    rows = _rows(_files(a)[0])
    assert rows[0] == ("leading", "filler", "serial", "barcode hr", "rfid & barcode")
    assert rows[1][2] == "00015035" and rows[-1][2] == "00016034" and len(rows) == 1001
    assert all(len(r[4]) == 24 and r[4] == f"2853{'0' * 12}{r[2]}" for r in rows[1:])
    b, code = _x(jm, 2, 100)
    assert code == 201, b
    sa = {r[2] for r in rows[1:]}
    sb = [r[2] for r in _rows(_files(b)[0])[1:]]
    assert sb[0] == "00016035" and not sa & set(sb)


def test_concurrent_jobs_get_distinct_ranges(jm):
    out = []
    def go(i): out.append(_x(jm, 10 + i, 50))
    ts = [threading.Thread(target=go, args=(i,)) for i in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert all(c == 201 for _, c in out), out
    serials = [r[2] for res, _ in out for r in _rows(_files(res)[0])[1:]]
    assert len(serials) == len(set(serials)) == 200


def test_runs_out_at_eight_digits(jm):
    import sheet_serials as S
    S.reserve("excelta_seq", "X-FILL", 99_999_990 - FIRST + 1)
    res, code = _x(jm, 3, 100)
    assert code == 400 and "99,999,999" in res["error"], res


def test_only_for_its_customer(jm):
    res, code = jm.app.create_job({"customer": "CDO", "title": "x", "job_ticket": "X9", "job_type": "special",
                                   "data_sheet": "excelta_seq", "quantity": 10, "lpr": 5})
    assert code == 400
