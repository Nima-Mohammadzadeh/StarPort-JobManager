"""Pins down what creating a job and adding data to a job do, on every path: the answer sent back,
and everything left behind (job rows, job number counter, folders and files, serial records, sheet
numbers, rolls, activity). The expected results are in tests/golden/job_build.json and were recorded
from the code before create_job / add_data were reorganised into steps, so any difference is a change
in behaviour.

To record again after an INTENDED change:  JM_UPDATE_GOLDEN=1 python -m pytest tests/test_job_build_behaviour.py
"""
import json
import os
import re
from datetime import datetime

import pytest

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden", "job_build.json")
UPDATE = os.environ.get("JM_UPDATE_GOLDEN") == "1"


# ── what is left behind ───────────────────────────────────────────────────────
def _norm(v, jm):
    if isinstance(v, dict):
        return {k: _norm(x, jm) for k, x in sorted(v.items())}
    if isinstance(v, (list, tuple)):
        return [_norm(x, jm) for x in v]
    if isinstance(v, str):
        v = v.replace(jm.root, "<ROOT>").replace("\\", "/")
        v = v.replace(f"{datetime.now():%m.%d.%y}", "<DATE>")
        v = re.sub(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", "<TS>", v)
        v = re.sub(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?", "<TS>", v)
    return v


def _rows(conn, sql):
    return [dict(r) for r in conn.execute(sql)]


def _state(jm):
    with jm.db.connect() as c:
        s = {
            "jobs": _rows(c, "SELECT * FROM jobs ORDER BY id"),
            "job_number_last": [r[0] for r in c.execute("SELECT value FROM app_settings WHERE key='job_number_last'")],
            "rolls": _rows(c, "SELECT job_id, roll_number, quantity, serial_start, serial_end, epc_start, epc_end, "
                              "status, addition_id FROM job_rolls ORDER BY job_id, roll_number"),
            "additions": _rows(c, "SELECT * FROM job_additions ORDER BY id"),
            "serial_allocations": _rows(c, 'SELECT upc, job_number, start, "end", qty, note, created_by, registry_state '
                                           "FROM serial_allocations ORDER BY id"),
            "sheet_serials": _rows(c, 'SELECT sheet, job_number, start, "end", qty, note, created_by '
                                      "FROM sheet_serials ORDER BY id"),
            "activity": _rows(c, "SELECT kind, actor, job_id, message FROM activity ORDER BY id"),
        }
    tree = []
    for base, _dirs, files in os.walk(jm.customers):
        rel = os.path.relpath(base, jm.customers)
        tree.append(rel + "/")
        tree += [os.path.join(rel, f) for f in files]
    s["folders"] = sorted(tree)
    reg = {}
    for base, _dirs, files in os.walk(jm.config.SERIAL_REGISTRY_DIR):
        for f in files:
            if f.endswith(".txt"):
                with open(os.path.join(base, f), encoding="utf-8") as fh:
                    reg[os.path.relpath(os.path.join(base, f), jm.config.SERIAL_REGISTRY_DIR)] = \
                        [ln.rstrip("\r\n") for ln in fh if ln.strip() and not ln.startswith("#")]
    s["registry_files"] = reg
    return s


# ── ways to make a step fail ──────────────────────────────────────────────────
def _boom(exc):
    def f(*a, **k):
        raise exc
    return f


STD = {"customer": "Acme", "title": "Blue shirt", "job_ticket": "T100", "quantity": 120, "lpr": 50,
       "overage_pct": 5, "label_size": "2 x 1", "po_number": "PO7", "part_number": "P-1", "inlay_type": "M730",
       "due_date": "2026-12-01", "notes": "rush", "created_by": "Dana"}


def _std(jm, **over):
    return {**STD, "upc": jm.upc(1), **over}


CDO = {"customer": "cdo", "title": "CDO tags", "job_ticket": "C200", "job_type": "special", "data_sheet": "cdo_seq",
       "quantity": 120, "lpr": 50, "qty_db": 100, "created_by": "Dana"}
GO2 = {"customer": "Go2", "title": "GO2 tags", "job_ticket": "G300", "job_type": "special", "data_sheet": "go2_rand",
       "quantity": 120, "lpr": 50, "created_by": "Dana"}
PLAIN = {"customer": "Acme", "title": "Hang tags", "job_ticket": "S400", "job_type": "special",
         "quantity": 120, "lpr": 50, "created_by": "Dana"}


def _made(jm, payload):
    body, code = jm.app.create_job(payload)
    assert code == 201, body
    return body["job"]["id"]


def _offline(jm):
    return jm.SS.SerialsOffline("The serial registry can't be reached.", {"next_start": 5000, "registry_message": "gone"})


def _flagged(jm, **flags):
    real = jm.SS.reserve
    return lambda *a, **k: {**real(*a, **k), **flags}


# ── new job ───────────────────────────────────────────────────────────────────
def new_standard(jm, mp):           return jm.app.create_job(_std(jm))
def new_standard_split_files(jm, mp): return jm.app.create_job(_std(jm, qty_db=50))
def new_standard_manual_start(jm, mp): return jm.app.create_job(_std(jm, serial_mode="manual", start_serial=70000))
def new_standard_no_ticket_no_size(jm, mp): return jm.app.create_job(_std(jm, job_ticket="", label_size="", po_number="n/a"))
def new_cdo(jm, mp):                return jm.app.create_job(CDO)
def new_go2(jm, mp):                return jm.app.create_job(GO2)
def new_special_plain(jm, mp):      return jm.app.create_job(PLAIN)
def new_special_no_quantity(jm, mp): return jm.app.create_job({"customer": "Acme", "title": "Art only", "job_type": "special"})


def new_second_job_same_day_same_ticket_folder(jm, mp):
    _made(jm, _std(jm, job_ticket=""))
    return jm.app.create_job(_std(jm, job_ticket="", title="Other", upc=jm.upc(2)))


def new_refused_no_customer(jm, mp):     return jm.app.create_job(_std(jm, customer=""))
def new_refused_bad_upc(jm, mp):         return jm.app.create_job(_std(jm, upc="12345"))
def new_refused_duplicate_ticket(jm, mp):
    _made(jm, _std(jm))
    return jm.app.create_job(_std(jm, title="Again", upc=jm.upc(2)))


def new_refused_overlapping_manual_start(jm, mp):
    _made(jm, _std(jm))
    return jm.app.create_job(_std(jm, job_ticket="T101", serial_mode="manual", start_serial=jm.R.SERIAL_START_DEFAULT + 5))


def new_refused_folders_unreachable(jm, mp):
    mp.setattr(jm.app, "Z_DRIVE_BASE", os.path.join(jm.root, "not_there"))
    return jm.app.create_job(_std(jm))


def new_fails_making_folder(jm, mp):
    mp.setattr(jm.app.os, "makedirs", _boom(OSError("share went away")))
    return jm.app.create_job(_std(jm))


def new_fails_serials_offline(jm, mp):
    mp.setattr(jm.SS, "reserve", _boom(_offline(jm)))
    return jm.app.create_job(_std(jm))


def new_fails_serials_refused(jm, mp):
    mp.setattr(jm.SS, "reserve", _boom(ValueError("That start serial is taken.")))
    return jm.app.create_job(_std(jm))


def new_fails_serials_unexpected(jm, mp):
    mp.setattr(jm.SS, "reserve", _boom(RuntimeError("registry file locked")))
    return jm.app.create_job(_std(jm))


def new_fails_cdo_numbers_refused(jm, mp):
    mp.setattr(jm.SHEETS, "reserve", _boom(jm.SHEETS.SheetSerialError("The CDO series is full.")))
    return jm.app.create_job(CDO)


def new_fails_cdo_numbers_unexpected(jm, mp):
    mp.setattr(jm.SHEETS, "reserve", _boom(RuntimeError("log locked")))
    return jm.app.create_job(CDO)


def new_fails_cdo_sheet_write(jm, mp):
    mp.setattr(jm.app, "generate_cdo_files", _boom(RuntimeError("disk full")))
    return jm.app.create_job(CDO)


def new_fails_go2_sheet_write(jm, mp):
    mp.setattr(jm.app, "generate_go2_files", _boom(RuntimeError("disk full")))
    return jm.app.create_job(GO2)


def new_after_failed_cdo_sheet_next_job(jm, mp):
    mp.setattr(jm.app, "generate_cdo_files", _boom(RuntimeError("disk full")))
    jm.app.create_job(CDO)
    mp.undo()
    return jm.app.create_job({**CDO, "job_ticket": "C201"})


def new_warns_encoding_database_not_written(jm, mp):
    mp.setattr(jm.app, "generate_db_files", _boom(RuntimeError("disk full")))
    return jm.app.create_job(_std(jm))


def new_warns_checklist_and_qc_not_written(jm, mp):
    mp.setattr(jm.app, "generate_checklist_html", lambda *a, **k: {"success": False, "error": "no room"})
    mp.setattr(jm.app, "generate_qc_sheet", lambda *a, **k: {"success": False, "error": "no room either"})
    return jm.app.create_job(_std(jm))


def new_warns_template_not_copied(jm, mp):
    return jm.app.create_job(_std(jm, template_id=999))


def new_warns_registry_offline_and_journal_unwritten(jm, mp):
    mp.setattr(jm.SS, "reserve", _flagged(jm, offline=True, journaled=False))
    return jm.app.create_job(_std(jm))


def new_fails_final_save_standard(jm, mp):
    mp.setattr(jm.app, "_insert_rolls", _boom(RuntimeError("db locked")))
    return jm.app.create_job(_std(jm))


def new_fails_final_save_cdo(jm, mp):
    mp.setattr(jm.app, "_insert_rolls", _boom(RuntimeError("db locked")))
    return jm.app.create_job(CDO)


def new_after_failed_folder_next_job_reuses_number(jm, mp):
    mp.setattr(jm.app.os, "makedirs", _boom(OSError("share went away")))
    jm.app.create_job(_std(jm))
    mp.undo()
    return jm.app.create_job(_std(jm))


# ── add data ──────────────────────────────────────────────────────────────────
ADD = {"quantity": 75, "reason": "customer wants more", "added_by": "Lee"}


def add_standard(jm, mp):        return jm.app.add_data(_made(jm, _std(jm)), ADD)
def add_standard_split(jm, mp):  return jm.app.add_data(_made(jm, _std(jm, qty_db=50)), ADD)
def add_cdo(jm, mp):             return jm.app.add_data(_made(jm, CDO), ADD)
def add_go2(jm, mp):             return jm.app.add_data(_made(jm, GO2), ADD)
def add_preview_standard(jm, mp): return jm.app.add_data(_made(jm, _std(jm)), {**ADD, "preview": True})
def add_preview_cdo(jm, mp):     return jm.app.add_data(_made(jm, CDO), {**ADD, "preview": True})
def add_preview_go2(jm, mp):     return jm.app.add_data(_made(jm, GO2), {**ADD, "preview": True})
def add_second_addition(jm, mp):
    j = _made(jm, _std(jm)); jm.app.add_data(j, ADD)
    return jm.app.add_data(j, {"quantity": 10})


def add_no_labels_per_roll(jm, mp):  return jm.app.add_data(_made(jm, _std(jm, lpr="")), ADD)


def add_reopens_complete_job(jm, mp):
    j = _made(jm, _std(jm))
    with jm.db.connect() as c:
        c.execute("UPDATE jobs SET status='Complete', completed_at='2026-01-01 00:00:00' WHERE id=?", (j,)); c.commit()
    return jm.app.add_data(j, ADD)


def add_refused_bad_quantity(jm, mp):   return jm.app.add_data(_made(jm, _std(jm)), {"quantity": 0})
def add_refused_too_many(jm, mp):       return jm.app.add_data(_made(jm, _std(jm)), {"quantity": 1_000_001})
def add_refused_unknown_job(jm, mp):    return jm.app.add_data(4242, ADD)
def add_refused_plain_special(jm, mp):  return jm.app.add_data(_made(jm, PLAIN), ADD)
def add_refused_archived(jm, mp):
    j = _made(jm, _std(jm))
    with jm.db.connect() as c:
        c.execute("UPDATE jobs SET archived_at='2026-01-01 00:00:00' WHERE id=?", (j,)); c.commit()
    return jm.app.add_data(j, ADD)


def add_refused_folder_gone(jm, mp):
    j = _made(jm, _std(jm))
    with jm.db.connect() as c:
        c.execute("UPDATE jobs SET folder_path=? WHERE id=?", (os.path.join(jm.root, "gone"), j)); c.commit()
    return jm.app.add_data(j, ADD)


def add_fails_serials_offline(jm, mp):
    j = _made(jm, _std(jm)); mp.setattr(jm.SS, "reserve", _boom(_offline(jm)))
    return jm.app.add_data(j, ADD)


def add_fails_serials_refused(jm, mp):
    j = _made(jm, _std(jm)); mp.setattr(jm.SS, "reserve", _boom(ValueError("No room left.")))
    return jm.app.add_data(j, ADD)


def add_fails_cdo_numbers_refused(jm, mp):
    j = _made(jm, CDO); mp.setattr(jm.SHEETS, "reserve", _boom(jm.SHEETS.SheetSerialError("The CDO series is full.")))
    return jm.app.add_data(j, ADD)


def add_fails_serials_unexpected(jm, mp):
    j = _made(jm, _std(jm)); mp.setattr(jm.SS, "reserve", _boom(RuntimeError("registry file locked")))
    return jm.app.add_data(j, ADD)


def add_warns_registry_offline(jm, mp):
    j = _made(jm, _std(jm)); mp.setattr(jm.SS, "reserve", _flagged(jm, offline=True))
    return jm.app.add_data(j, ADD)


def add_fails_files_standard(jm, mp):
    j = _made(jm, _std(jm)); mp.setattr(jm.app, "generate_db_files", _boom(RuntimeError("disk full")))
    return jm.app.add_data(j, ADD)


def add_fails_files_cdo(jm, mp):
    j = _made(jm, CDO); mp.setattr(jm.app, "generate_cdo_files", _boom(RuntimeError("disk full")))
    return jm.app.add_data(j, ADD)


def add_fails_files_go2(jm, mp):
    j = _made(jm, GO2); mp.setattr(jm.app, "generate_go2_files", _boom(RuntimeError("disk full")))
    return jm.app.add_data(j, ADD)


def add_fails_save_standard(jm, mp):
    j = _made(jm, _std(jm)); mp.setattr(jm.app, "_insert_rolls", _boom(RuntimeError("db locked")))
    return jm.app.add_data(j, ADD)


def add_fails_save_cdo(jm, mp):
    j = _made(jm, CDO); mp.setattr(jm.app, "_insert_rolls", _boom(RuntimeError("db locked")))
    return jm.app.add_data(j, ADD)


def add_fails_save_go2(jm, mp):
    j = _made(jm, GO2); mp.setattr(jm.app, "_insert_rolls", _boom(RuntimeError("db locked")))
    return jm.app.add_data(j, ADD)


def add_warns_paperwork_not_rewritten(jm, mp):
    j = _made(jm, _std(jm)); mp.setattr(jm.app, "regenerate_sheets", lambda job_id: ["QC sheet locked"])
    return jm.app.add_data(j, ADD)


def add_warns_paperwork_crashed(jm, mp):
    j = _made(jm, _std(jm)); mp.setattr(jm.app, "regenerate_sheets", _boom(RuntimeError("printer on fire")))
    return jm.app.add_data(j, ADD)


SCENARIOS = {n: f for n, f in list(globals().items()) if n.startswith(("new_", "add_")) and callable(f)}


def _golden():
    if not os.path.isfile(GOLDEN):
        return {}
    with open(GOLDEN, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_behaviour_is_unchanged(name, jm, monkeypatch):
    body, code = SCENARIOS[name](jm, monkeypatch)
    monkeypatch.undo()
    got = json.loads(json.dumps(_norm({"status": code, "answer": body, "left_behind": _state(jm)}, jm)))
    golden = _golden()
    if UPDATE:
        golden[name] = got
        with open(GOLDEN, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(golden, fh, indent=1, sort_keys=True, ensure_ascii=False)
            fh.write("\n")
        return
    assert name in golden, f"No recorded result for {name}. See the note at the top of this file."
    assert got == golden[name]


def test_every_recorded_result_still_has_a_scenario():
    assert sorted(_golden()) == sorted(SCENARIOS)
