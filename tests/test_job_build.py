"""job_build.Build: remembers what a job has taken so far and can undo it in one place."""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JB = importlib.import_module("job_build")
sys.path.pop(0)


def test_stop_undoes_everything_newest_first_and_carries_the_answer():
    log, b = [], JB.Build()
    b.took("job number", undo=lambda why: log.append("number"))
    b.took("folder", undo=lambda why: log.append("folder"))
    b.took("sheet numbers", undo=lambda why: log.append(f"sheet:{why}"))
    stop = b.stop("The sheet could not be written", 500, because="sheet not written", detail=7)
    assert isinstance(stop, JB.Stop)
    assert stop.answer == ({"error": "The sheet could not be written", "detail": 7}, 500)
    assert log == ["sheet:sheet not written", "folder", "number"]


def test_things_with_no_undo_are_remembered_but_left_alone():
    b = JB.Build()
    b.took("serials")
    assert b.has("serials") and not b.has("folder")
    assert b.rollback() == []


def test_keep_means_nothing_taken_so_far_is_undone_later():
    log, b = [], JB.Build()
    b.took("job number", undo=lambda why: log.append("number"))
    b.keep()
    b.took("data files", undo=lambda why: log.append("files"))
    assert b.rollback() == ["data files"]
    assert log == ["files"] and b.has("job number")


def test_rollback_runs_once():
    log, b = [], JB.Build()
    b.took("folder", undo=lambda why: log.append("folder"))
    b.rollback(); b.rollback()
    assert log == ["folder"]


def test_a_failing_undo_does_not_stop_the_others_or_hide_the_error():
    log, b = [], JB.Build()
    b.took("job number", undo=lambda why: log.append("number"))
    b.took("folder", undo=lambda why: 1 / 0)
    stop = b.stop("Serials could not be taken", 500)
    assert log == ["number"]
    assert stop.answer[0]["error"] == "Serials could not be taken"
    assert [what for what, _ in b.undo_errors] == ["folder"]


def test_refuse_is_a_stop_that_undoes_nothing():
    stop = JB.refuse("Customer is required", 400)
    assert stop.answer == ({"error": "Customer is required"}, 400)
    with pytest.raises(JB.Stop):
        raise stop


def test_warnings_collect_in_order():
    b = JB.Build()
    b.warn("first"); b.warn("second")
    assert b.warnings == ["first", "second"]


# ── the undo path, through the real job functions ─────────────────────────────
def _boom(*a, **k):
    raise RuntimeError("unexpected crash")


CDO = {"customer": "CDO", "title": "CDO tags", "job_ticket": "C1", "job_type": "special", "data_sheet": "cdo_seq",
       "quantity": 100, "lpr": 50}


def _counts(jm):
    with jm.db.connect() as c:
        return {"jobs": c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                "serials": c.execute("SELECT COUNT(*) FROM serial_allocations").fetchone()[0],
                "sheet_notes": [r[0] for r in c.execute("SELECT note FROM sheet_serials ORDER BY id")],
                "last": c.execute("SELECT value FROM app_settings WHERE key='job_number_last'").fetchone()[0]}


def test_unexpected_crash_before_serials_leaves_nothing_behind(jm, monkeypatch):
    monkeypatch.setattr(jm.app, "roll_plan_seq", _boom)          # after the CDO numbers are taken
    with pytest.raises(RuntimeError, match="unexpected crash"):
        jm.app.create_job(CDO)
    got = _counts(jm)
    assert got["jobs"] == 0 and got["last"] == "0"
    assert got["sheet_notes"] == ["VOID: job creation failed unexpectedly"]        # a gap, never a repeat
    assert not any(fs for _b, _d, fs in os.walk(jm.customers))                     # no files left anywhere


def test_unexpected_crash_after_upc_serials_keeps_the_job_number_they_are_recorded_under(jm, monkeypatch):
    monkeypatch.setattr(jm.app, "roll_plan", _boom)              # after the UPC serials are taken
    with pytest.raises(RuntimeError, match="unexpected crash"):
        jm.app.create_job({"customer": "Acme", "title": "Shirt", "job_ticket": "T1", "upc": jm.upc(1),
                           "quantity": 100, "lpr": 50})
    got = _counts(jm)
    assert got["serials"] == 1 and got["jobs"] == 1 and got["last"] == "1"
    monkeypatch.undo()
    nxt, code = jm.app.create_job({"customer": "Acme", "title": "Shirt 2", "job_ticket": "T2", "upc": jm.upc(1),
                                   "quantity": 100, "lpr": 50})
    assert code == 201 and nxt["job_number"] == "JOB-0002"       # the number is not handed out again
    assert nxt["serial_alloc"]["start"] > jm.R.SERIAL_START_DEFAULT + 99


def test_unexpected_crash_while_adding_data_voids_the_new_cdo_numbers(jm, monkeypatch):
    body, code = jm.app.create_job(CDO); assert code == 201
    monkeypatch.setattr(jm.app, "roll_plan_seq", _boom)
    with pytest.raises(RuntimeError, match="unexpected crash"):
        jm.app.add_data(body["job"]["id"], {"quantity": 40})
    got = _counts(jm)
    assert got["sheet_notes"] == ["base", "VOID: addition failed unexpectedly"]
    with jm.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM job_additions").fetchone()[0] == 0
