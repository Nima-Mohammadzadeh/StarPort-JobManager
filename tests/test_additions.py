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
