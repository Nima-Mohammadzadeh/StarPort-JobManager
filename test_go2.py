"""GO2 data sheet on special (no UPC) jobs: random E2004 EPCs, customer GO2 only."""
import os
import re

import pytest
from openpyxl import load_workbook, Workbook


def _rows(path):
    wb = load_workbook(path, read_only=True)
    try:
        return list(wb.active.iter_rows(min_row=2, values_only=True))
    finally:
        wb.close()


def _data_files(res):
    d = os.path.join(res["folder_path"], "Data")
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".xlsx"))


def _go2(jm, n, sheet, qty, **kw):
    body = {"customer": "GO2", "title": f"Tag {n}", "job_ticket": f"G{n}", "job_type": "special",
            "data_sheet": sheet, "quantity": qty, "lpr": 500, **kw}
    return jm.app.create_job(body)


def test_random_sheet_prefix_never_drifts(jm):
    """The old sheet counted E2004, E2005... after row 100,000. Go well past that."""
    res, code = _go2(jm, 1, "go2_rand", 100_050)
    assert code == 201, res
    (path,) = _data_files(res)
    rows = _rows(path)
    assert len(rows) == 100_050
    assert {r[0] for r in rows} == {"E2004"}
    assert all(r[2] == r[0] + r[1] and len(r[2]) == 24 and re.fullmatch(r"E2004[0-9A-F]{19}", r[2]) for r in rows)
    assert len({r[2] for r in rows}) == len(rows)


def test_random_sheet_is_values_not_formulas(jm):
    res, code = _go2(jm, 2, "go2_rand", 50)
    assert code == 201, res
    wb = load_workbook(_data_files(res)[0])           # not data_only: would show formulas
    cells = [c.value for row in wb.active.iter_rows(min_row=2) for c in row]
    assert not any(isinstance(v, str) and v.startswith("=") for v in cells)


def test_random_sheet_covers_every_hex_digit_evenly(jm):
    res, _ = _go2(jm, 3, "go2_rand", 20_000)
    digits = "".join(r[1] for r in _rows(_data_files(res)[0]))
    for ch in "0123456789ABCDEF":
        assert abs(digits.count(ch) / len(digits) - 1 / 16) < 0.005, ch


def test_random_sheet_splits_with_overage(jm):
    res, code = _go2(jm, 4, "go2_rand", 1000, overage_pct=2, qty_db=400)
    assert code == 201, res
    files = _data_files(res)
    assert [len(_rows(f)) for f in files] == [400, 400, 220]
    allepc = [r[2] for f in files for r in _rows(f)]
    assert len(set(allepc)) == 1020 and all(e.startswith("E2004") for e in allepc)


@pytest.mark.parametrize("name", ["GO2", "go2", "Go2", "gO2", "  go2 "])
def test_any_capitalization_of_go2_is_accepted(jm, name):
    res, code = _go2(jm, 20, "go2_rand", 10, customer=name)
    assert code == 201 and res["job"]["data_sheet"] == "go2_rand", res
    assert res["job"]["customer"] == "GO2"                  # one customer, one folder
    assert os.path.basename(os.path.dirname(res["folder_path"])) == "GO2"


@pytest.mark.parametrize("name", ["Acme", "GO23", "GO 2", ""])
def test_other_customers_cannot_take_the_go2_sheet(jm, name):
    res, code = _go2(jm, 21, "go2_rand", 10, customer=name)
    assert code == 400


def test_data_sheet_rules(jm):
    res, code = jm.app.create_job({"customer": "GO2", "title": "x", "job_ticket": "S1", "upc": jm.upc(1),
                                   "quantity": 10, "data_sheet": "go2_rand"})
    assert code == 400                                   # standard jobs cannot take one
    res, code = _go2(jm, 11, "go2_rand", None)
    assert code == 400 and "Quantity" in res["error"]
    res, code = _go2(jm, 12, "go2_seq", 10)              # the sequential sheet is gone
    assert code == 400


def test_special_job_without_sheet_unchanged(jm):
    res, code = jm.app.create_job({"customer": "Acme", "title": "x", "job_ticket": "P1", "job_type": "special"})
    assert code == 201 and res["job"]["data_sheet"] is None
    assert not [f for f in os.listdir(os.path.join(res["folder_path"], "Data")) if f.endswith(".xlsx")]


def test_verifier_rejects_a_drifting_prefix(tmp_path):
    import generators as g
    wb = Workbook()
    ws = wb.active
    ws.append(["Prefix", "Random Gen", "EPC"])
    ws.append(["E2004", "0" * 19, "E2004" + "0" * 19])
    ws.append(["E2005", "1" * 19, "E2005" + "1" * 19])        # the old fill-down bug
    p = str(tmp_path / "bad.xlsx"); wb.save(p)
    with pytest.raises(ValueError, match="bad prefix"):
        g.verify_go2_file(p, 2)
