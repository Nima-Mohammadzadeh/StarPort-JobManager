"""
integrity.py — read-only check that the serial records and the database add up
================================================================================
Ported in spirit from the earlier RFID Encoding Manager (its V1.7 "database-backed serial
overlap validation" and V1.8.2/1.9 integrity scans). It never changes anything: it reports.
Something that can't be checked (no start serial, no recorded range) is reported, never
counted as fine.

Checks
  serial    every standard job: its rolls start at the job's start serial, run one after another
            with no gaps or repeats, add up to quantity + overage, and carry the right EPCs; the
            job's serial range is recorded (serial_allocations) and covers every roll.
  overlap   no two recorded ranges for one UPC overlap (any job, reservation or history line),
            and no two jobs' own ranges overlap; the same for each data-sheet series (CDO).
  database  SQLite quick_check, foreign keys, and rolls / notes that point at no job.

run(conn) returns {"ok", "checked_at", "jobs_checked", "ranges_checked", "problems": [...],
"counts"}; each problem is {"kind", "level": bad|warn, "job_id", "job_number", "detail"}.
"""
from datetime import datetime

from generators import generate_epc, production_qty


def _p(out, kind, level, detail, job=None, job_number=None):
    out.append({"kind": kind, "level": level, "detail": detail,
                "job_id": (job or {}).get("id"), "job_number": (job or {}).get("job_number") or job_number})


def _overlaps(ranges):
    """[(start, end, owner), ...] -> pairs of different owners whose ranges overlap."""
    found, active = [], []
    for s, e, who in sorted(ranges):
        active = [a for a in active if a[1] >= s]
        for a in active:
            if a[2] != who:
                found.append((a, (s, e, who)))
        active.append((s, e, who))
    return found


def check_serials(conn, out):
    jobs = [dict(r) for r in conn.execute(
        """SELECT id, job_number, job_ticket, customer, upc, quantity, overage_pct, start_serial, lpr, job_type,
                  status, archived_at, data_sheet FROM jobs WHERE status != 'Cancelled'""")]
    rolls = {}
    for r in conn.execute("""SELECT job_id, roll_number, quantity, serial_start, serial_end, epc_start, epc_end
                             FROM job_rolls ORDER BY job_id, roll_number"""):
        rolls.setdefault(r["job_id"], []).append(dict(r))
    alloc = {}
    for a in conn.execute('SELECT upc, job_number, start, "end", qty, registry_state FROM serial_allocations'):
        alloc.setdefault((a["upc"], a["job_number"]), []).append(dict(a))
    checked = 0
    for j in jobs:
        if not j["upc"]:
            continue                                     # special jobs: checked as a data-sheet series below
        checked += 1
        label = j["job_ticket"] or j["job_number"]
        if not j["start_serial"] or not j["quantity"]:
            _p(out, "serial", "warn", f"{label}: no start serial or quantity recorded, so its serials can't be checked", j)
            continue
        total = production_qty(j["quantity"], j["overage_pct"] or 0)
        start, end = j["start_serial"], j["start_serial"] + total - 1
        mine = alloc.get((j["upc"], j["job_number"]), [])
        if not mine:
            _p(out, "serial", "warn", f"{label}: no serial range is recorded for this job (UPC {j['upc']}, "
                                      f"{start:,}–{end:,})", j)
        elif not any(a["start"] <= start and a["end"] >= end for a in mine):
            got = ", ".join(f"{a['start']:,}–{a['end']:,}" for a in mine)
            _p(out, "serial", "bad", f"{label}: uses {start:,}–{end:,} but its recorded range is {got}", j)
        rs = [r for r in rolls.get(j["id"], []) if r["serial_start"] is not None]
        if not rs:
            continue
        expect, problems, serious = start, [], False
        for i, r in enumerate(rs):
            n = r["roll_number"]
            if r["serial_start"] < expect:
                problems.append(f"roll {n} starts at {r['serial_start']:,}, before " +
                                (f"the job's first serial {start:,}" if i == 0 else f"roll {rs[i - 1]['roll_number']} ended (repeats serials)"))
                serious = True
            elif r["serial_start"] > expect:
                problems.append(f"roll {n} starts at {r['serial_start']:,}; serials {expect:,}–{r['serial_start'] - 1:,} are on no roll")
            if r["serial_end"] - r["serial_start"] + 1 != r["quantity"]:
                problems.append(f"roll {n} holds {r['serial_end'] - r['serial_start'] + 1:,} serials but says {r['quantity']:,} labels")
            if (r["epc_start"] and r["epc_start"] != generate_epc(j["upc"], r["serial_start"])) or \
               (r["epc_end"] and r["epc_end"] != generate_epc(j["upc"], r["serial_end"])):
                problems.append(f"roll {n} EPCs don't match its serials")
                serious = True
            expect = max(expect, r["serial_end"] + 1)
        if expect - 1 > end:
            problems.append(f"rolls run to {expect - 1:,}, past the job's last serial {end:,}")
            serious = True
        elif expect - 1 < end:
            problems.append(f"rolls stop at {expect - 1:,}; the job runs to {end:,} ({end - expect + 1:,} labels have no roll)")
        if problems:
            more = f" (and {len(problems) - 4} more)" if len(problems) > 4 else ""
            _p(out, "serial", "bad" if serious else "warn", f"{label}: " + "; ".join(problems[:4]) + more, j)
    return checked


def check_overlaps(conn, out):
    # Open conflicts are already listed (Needs attention, Tools) and reviewed ones were accepted by a
    # person: neither is reported again here.
    by_upc, handled = {}, set()
    for a in conn.execute('SELECT upc, job_number, start, "end", registry_state, note FROM serial_allocations WHERE "end" >= start'):
        if a["registry_state"] == -1 or "conflict reviewed by" in (a["note"] or ""):
            handled.add((a["upc"], a["job_number"]))
            continue
        by_upc.setdefault(a["upc"], []).append((a["start"], a["end"], a["job_number"]))
    jobs = {r["job_number"]: dict(r) for r in conn.execute("SELECT id, job_number, job_ticket FROM jobs")}
    seen, n = set(), 0
    for upc, ranges in by_upc.items():
        n += len(ranges)
        for (s1, e1, w1), (s2, e2, w2) in _overlaps(ranges):
            key = (upc, *sorted((w1, w2)))
            if key in seen:
                continue
            seen.add(key)
            lo, hi = max(s1, s2), min(e1, e2)
            _p(out, "overlap", "bad", f"UPC {upc}: {w1} ({s1:,}–{e1:,}) and {w2} ({s2:,}–{e2:,}) share serials "
                                      f"{lo:,}–{hi:,}", jobs.get(w2) or jobs.get(w1), w2)
    # The jobs' own ranges, in case a recorded range is missing or wrong.
    own = {}
    for j in conn.execute("""SELECT id, job_number, upc, quantity, overage_pct, start_serial FROM jobs
                             WHERE status != 'Cancelled' AND upc IS NOT NULL AND upc != ''
                               AND start_serial IS NOT NULL AND quantity > 0"""):
        own.setdefault(j["upc"], []).append((j["start_serial"], j["start_serial"] + production_qty(j["quantity"], j["overage_pct"] or 0) - 1,
                                             j["job_number"]))
    for upc, ranges in own.items():
        for (s1, e1, w1), (s2, e2, w2) in _overlaps(ranges):
            key = (upc, *sorted((w1, w2)))
            if key in seen or (upc, w1) in handled or (upc, w2) in handled:
                continue
            seen.add(key)
            _p(out, "overlap", "bad", f"UPC {upc}: jobs {w1} ({s1:,}–{e1:,}) and {w2} ({s2:,}–{e2:,}) use the same serials",
               jobs.get(w2), w2)
    # Data-sheet series (CDO): one running series, never two jobs on one number.
    try:
        series = {}
        for r in conn.execute('SELECT sheet, job_number, start, "end" FROM sheet_serials'):
            series.setdefault(r["sheet"], []).append((r["start"], r["end"], r["job_number"]))
        for sheet, ranges in series.items():
            n += len(ranges)
            for (s1, e1, w1), (s2, e2, w2) in _overlaps(ranges):
                _p(out, "overlap", "bad", f"{sheet} series: {w1} ({s1:,}–{e1:,}) and {w2} ({s2:,}–{e2:,}) share numbers",
                   jobs.get(w2), w2)
    except Exception:
        pass                                             # older database without the table
    return n


def check_database(conn, out):
    qc = conn.execute("PRAGMA quick_check").fetchone()[0]
    if qc != "ok":
        _p(out, "database", "bad", f"SQLite quick_check: {qc}")
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        tables = sorted({r[0] for r in fk})
        _p(out, "database", "warn", f"{len(fk)} row(s) point at records that no longer exist ({', '.join(tables)})")
    for table, what in (("job_rolls", "rolls"), ("job_notes", "notes")):
        n = conn.execute(f"SELECT COUNT(*) FROM {table} t WHERE NOT EXISTS (SELECT 1 FROM jobs j WHERE j.id = t.job_id)").fetchone()[0]
        if n:
            _p(out, "database", "warn", f"{n:,} {what} belong to no job")


def run(conn) -> dict:
    out = []
    jobs_checked = check_serials(conn, out)
    ranges_checked = check_overlaps(conn, out)
    check_database(conn, out)
    out.sort(key=lambda p: (p["level"] != "bad", p["kind"] != "overlap", p["job_number"] or ""))
    counts = {}
    for p in out:
        counts[p["level"]] = counts.get(p["level"], 0) + 1
    return {"ok": not out, "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "jobs_checked": jobs_checked, "ranges_checked": ranges_checked, "problems": out, "counts": counts}
