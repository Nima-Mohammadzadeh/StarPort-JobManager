"""Venture Research data sheet: Prefix | Serial | EPC, one running series that continues from the last sheet we ran."""
import os

from openpyxl import load_workbook

FIRST = 380_021                     # the last sheet ended at 0000380020
PREFIX = "34083328448A2E"


def _rows(path):
    wb = load_workbook(path, read_only=True)
    try:
        return list(wb.active.iter_rows(min_row=1, values_only=True))
    finally:
        wb.close()


def _files(res):
    d = os.path.join(res["folder_path"], "Data")
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".xlsx"))


def _v(jm, n, qty, **kw):
    return jm.app.create_job({"customer": "venture research", "title": f"Tag {n}", "job_ticket": f"V{n}",
                              "job_type": "special", "data_sheet": "venture_seq", "quantity": qty, "lpr": 500, **kw})


def test_formats_match_the_old_sheet():
    import generators as g
    assert g.venture_serial(380020) == "0000380020"
    assert g.venture_epc(360021) == PREFIX + "0000360021"      # first row of the old sheet
    assert len(g.venture_epc(1)) == 24


def test_first_job_continues_after_the_last_sheet(jm):
    res, code = _v(jm, 1, 1000)
    assert code == 201, res
    rows = _rows(_files(res)[0])
    assert rows[0] == ("Prefix", "Serial", "EPC")
    body = rows[1:]
    assert len(body) == 1000
    assert body[0] == (PREFIX, "0000380021", PREFIX + "0000380021")
    assert [int(r[1]) for r in body] == list(range(FIRST, FIRST + 1000))
    assert all(r[0] == PREFIX and r[2] == r[0] + r[1] for r in body)
    assert res["job"]["start_serial"] == FIRST and res["job"]["customer"] == "Venture Research"


def test_second_job_and_addition_never_repeat(jm):
    a, _ = _v(jm, 2, 300)
    b, code = _v(jm, 3, 100)
    assert code == 201, b
    sa = [r[1] for r in _rows(_files(a)[0])[1:]]
    sb = [r[1] for r in _rows(_files(b)[0])[1:]]
    assert int(sa[-1]) + 1 == int(sb[0]) and not set(sa) & set(sb)


def test_only_for_its_customer(jm):
    res, code = jm.app.create_job({"customer": "CDO", "title": "x", "job_ticket": "V9", "job_type": "special",
                                   "data_sheet": "venture_seq", "quantity": 10, "lpr": 5})
    assert code == 400


def test_meta_reports_next_numbers(jm):
    import sheet_serials as S
    assert S.next_start("venture_seq") == FIRST
    assert S.next_start("cdo_seq") == 3_768_677_565
