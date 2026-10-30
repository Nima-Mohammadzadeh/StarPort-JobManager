"""
floor_ops.py — what the encoding floor needs between "start a roll" and "job done"
==================================================================================
Logic only (no routes): app.py calls these. Ported ideas from the earlier RFID Encoding Manager
(protected BarTender handoff, RFID roll verification, reprint history, shift handoff), shaped to
Job Manager's rolls, printers and record numbers.

  BarTender print requests  A roll (or one part of it, when it spans two database files) is sent to
                            BarTender as a BTXML script file in the trigger folder: the job's template,
                            the record range, the Windows printer. A BarTender Integration (Enterprise)
                            watches that folder, prints, and writes its reply into the acknowledgement
                            folder. Request states: sent -> picked_up (the file was taken) -> printed |
                            failed. One unfinished request per roll at a time.
  Tag checks                Each scan made against an open job is recorded. The roll's first and last
                            tag passing marks the roll verified.
  Reprints                  Labels reprinted or spoiled on a roll, with a reason.
  Pace                      Minutes per roll and an estimated finish, from roll start / done times.
  Shift summary             What happened between two times, for the hand-off.
"""
import os
import re
import statistics
from datetime import datetime, timedelta
from html import escape
from xml.sax.saxutils import escape as xml_escape

REPRINT_REASONS = ["Bad encode", "Print defect", "Jam / torn", "Setup / test", "Wrong data", "Other"]
BT_EXT = ".btxml"                    # what the BarTender Integration's file trigger should watch for
BT_STUCK_MINUTES = 3                 # a request nobody picked up for this long needs a look
PACE_MIN_ROLLS = 2                   # rolls with real start/finish times needed before estimating


def init_schema(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS roll_checks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL, roll_number INTEGER,
        epc TEXT, serial INTEGER, position TEXT, result TEXT NOT NULL, message TEXT,
        source TEXT, who TEXT, ts TEXT NOT NULL)""")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_checks_job ON roll_checks(job_id, roll_number)")
    cur.execute("""CREATE TABLE IF NOT EXISTS reprints (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL, roll_number INTEGER NOT NULL,
        qty INTEGER NOT NULL, serial_from INTEGER, serial_to INTEGER, reason TEXT NOT NULL, note TEXT,
        who TEXT, ts TEXT NOT NULL)""")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_reprints_job ON reprints(job_id)")
    cur.execute("""CREATE TABLE IF NOT EXISTS print_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, req_id TEXT NOT NULL UNIQUE, job_id INTEGER NOT NULL,
        roll_number INTEGER NOT NULL, part INTEGER NOT NULL DEFAULT 1, parts INTEGER NOT NULL DEFAULT 1,
        template TEXT, database TEXT, rec_from INTEGER, rec_to INTEGER, printer TEXT, bt_printer TEXT,
        trigger_path TEXT, state TEXT NOT NULL, message TEXT, who TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_print_job ON print_requests(job_id, roll_number)")
    cols = [r[1] for r in cur.execute("PRAGMA table_info(job_rolls)")]
    for col in ("first_ok_at", "last_ok_at", "verified_at", "verified_by"):
        if col not in cols:
            cur.execute(f"ALTER TABLE job_rolls ADD COLUMN {col} TEXT")


# ── BarTender print requests ──────────────────────────────────────────────────
def printer_map(text) -> dict:
    """'Printer 1 = ZDesigner ZT411-203dpi ZPL' lines -> {'printer 1': 'ZDesigner ...'}."""
    out = {}
    for line in str(text or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            if k.strip() and v.strip():
                out[k.strip().lower()] = v.strip()
    return out


def btxml(req_id, template, rec_from, rec_to, bt_printer=None, database=None, label=""):
    """A BarTender XML script that prints records rec_from..rec_to of the template's database.
    Only tags from BarTender's BTXML reference are used: Format, PrintSetup (UseDatabase, RecordRange,
    Printer) and, for a text (.csv) database, RecordSet/FileName. BTXML can't name an Excel file, so for
    .xlsx the template must already point at the job's database (as when it is opened in BarTender);
    the database path is also written in a comment for an Integration that overrides it."""
    rs = ""
    if database and database.lower().endswith((".csv", ".txt")):
        rs = (f'\n      <RecordSet Name="Text File 1" Type="btTextFile">\n'
              f'        <FileName>{xml_escape(database)}</FileName>\n      </RecordSet>')
    pr = f"\n        <Printer>{xml_escape(bt_printer)}</Printer>" if bt_printer else ""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!-- Job Manager print request {req_id}: {xml_escape(label)} -->
<!-- database: {xml_escape(database or '')} -->
<XMLScript Version="2.0" Name="{xml_escape(req_id)}" ID="{xml_escape(req_id)}">
  <Command Name="Print">
    <Print WaitForJobToComplete="true" ReturnPrintSummary="true">
      <Format CloseAtEndOfJob="true">{xml_escape(template)}</Format>{rs}
      <PrintSetup>
        <UseDatabase>true</UseDatabase>
        <RecordRange>{int(rec_from)}-{int(rec_to)}</RecordRange>{pr}
      </PrintSetup>
    </Print>
  </Command>
</XMLScript>
"""


def write_request(trigger_dir, req_id, text) -> str:
    """Write the request so the Integration only ever sees a complete file: a temporary name first,
    then a rename to <req_id>.btxml."""
    final = os.path.join(trigger_dir, f"{req_id}{BT_EXT}")
    tmp = os.path.join(trigger_dir, f".{req_id}.writing")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, final)
    return final


_ERR = re.compile(r'Severity="(?:Error|Fatal)"[^>]*>(?:\s*<[^>]+>)*\s*([^<]{3,300})|<Status>\s*(Fail[^<]*)</Status>', re.I)


def read_reply(text):
    """(state, message) from what the Integration wrote back. A BTXML response marks problems with
    Severity="Error" (or Fatal) on a Message; anything else in an XML response is a print. A plain-text
    reply (an Integration's own response file) counts as failed when it says error or fail."""
    text = text or ""
    m = _ERR.search(text)
    if m:
        return "failed", " ".join((m.group(1) or m.group(2) or "BarTender reported an error").split())[:300]
    if "<" in text and re.search(r"<(XMLScript|Response|Message)\b", text, re.I):
        return "printed", ""
    bad = [l.strip() for l in text.splitlines()
           if re.search(r"\b(error|failed|failure|fail)\b", l, re.I) and not re.search(r"\b(0|no) errors?\b", l, re.I)]
    if bad:
        return "failed", bad[0][:300]
    return "printed", ""


def find_reply(ack_dir, req_id):
    """The reply file for a request: any file in the acknowledgement folder whose name contains the
    request ID (BarTender names its response after the trigger file). None if not there yet."""
    try:
        for e in os.scandir(ack_dir):
            if e.is_file() and req_id.lower() in e.name.lower():
                return e.path
    except OSError:
        pass
    return None


def segments(rolls, roll_number, qty_db):
    """Record numbers for one roll, split per database file (numbering restarts in each file),
    the same way the roll tracking page shows them. [(file_index, rec_from, rec_to), ...]"""
    total = sum(r["quantity"] for r in rolls)
    qdb = qty_db if qty_db and qty_db < total else 0
    before = 0
    for r in rolls:
        if r["roll_number"] == roll_number:
            a, end, out = before + 1, before + r["quantity"], []
            while a <= end:
                f = (a - 1) // qdb + 1 if qdb else 1
                b = min(end, f * qdb) if qdb else end
                out.append((f, (a - 1) % qdb + 1 if qdb else a, (b - 1) % qdb + 1 if qdb else b))
                a = b + 1
            return out
        before += r["quantity"]
    return []


# ── pace ──────────────────────────────────────────────────────────────────────
def _t(ts):
    try:
        return datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def pace(rolls, now=None):
    """From rolls with real start and finish times (1 min to 8 h; bulk "done through" marks are left
    out): minutes per roll, labels per hour, and an estimated finish for what is left, assuming the
    printers running this job now keep going. None until PACE_MIN_ROLLS rolls have been timed."""
    now = now or datetime.now()
    timed = []
    for r in rolls:
        if r["status"] in ("complete", "verified"):
            a, b = _t(r.get("started_at")), _t(r.get("completed_at"))
            if a and b and 60 <= (b - a).total_seconds() <= 8 * 3600:
                timed.append(((b - a).total_seconds() / 60, r["quantity"]))
    if len(timed) < PACE_MIN_ROLLS:
        return None
    per_roll = statistics.median(m for m, _ in timed)
    per_hour = sum(q for _, q in timed) / (sum(m for m, _ in timed) / 60)
    left = [r for r in rolls if r["status"] not in ("complete", "verified")]
    printers = len({r.get("printer") for r in rolls if r["status"] == "running" and r.get("printer")}) or 1
    eta = None
    if left:
        mins = 0.0
        for r in left:
            if r["status"] == "running" and _t(r.get("started_at")):
                mins += max(0.0, per_roll - (now - _t(r["started_at"])).total_seconds() / 60)
            else:
                mins += per_roll
        eta = (now + timedelta(minutes=mins / printers)).strftime("%Y-%m-%d %H:%M")
    return {"minutes_per_roll": round(per_roll, 1), "labels_per_hour": int(per_hour), "timed_rolls": len(timed),
            "rolls_left": len(left), "printers": printers, "finish_at": eta}


# ── shift summary ─────────────────────────────────────────────────────────────
def shift_window(starts_text, which="current", now=None):
    """(start, end, label) of the current or previous shift (end = when the shift ends, even if that is
    still to come). starts_text: '06:00,14:00,22:00'."""
    now = now or datetime.now()
    starts = []
    for part in str(starts_text or "").replace(";", ",").split(","):
        m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", part)
        if m and int(m[1]) < 24 and int(m[2]) < 60:
            starts.append((int(m[1]), int(m[2])))
    starts = sorted(set(starts)) or [(6, 0), (14, 0), (22, 0)]
    cands = []
    for d in (-2, -1, 0, 1):
        day = (now + timedelta(days=d)).date()
        cands += [datetime.combine(day, datetime.min.time()).replace(hour=h, minute=m) for h, m in starts]
    cands.sort()
    i = max(k for k, c in enumerate(cands) if c <= now)
    if which == "previous":
        i -= 1
    start, end = cands[i], cands[i + 1]
    return start, end, f"{start:%a %b %d %H:%M}–{end:%H:%M}"


def shift_summary(conn, start, end):
    s, e = start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")
    q = lambda sql, *a: [dict(r) for r in conn.execute(sql, a)]
    done = q("""SELECT r.job_id, r.roll_number, r.quantity, r.printer, r.completed_by, r.completed_at,
                       j.customer, j.job_ticket, j.job_number, j.title
                FROM job_rolls r JOIN jobs j ON j.id = r.job_id
                WHERE r.status IN ('complete','verified') AND r.completed_at >= ? AND r.completed_at < ?
                ORDER BY r.completed_at""", s, e)
    by_job, by_printer, by_person = {}, {}, {}
    for r in done:
        k = r["job_id"]
        g = by_job.setdefault(k, {"job_id": k, "label": f"{r['customer']} {r['job_ticket'] or r['job_number']}",
                                  "title": r["title"], "rolls": [], "labels": 0})
        g["rolls"].append(r["roll_number"]); g["labels"] += r["quantity"]
        p = by_printer.setdefault(r["printer"] or "(no printer)", {"rolls": 0, "labels": 0})
        p["rolls"] += 1; p["labels"] += r["quantity"]
        w = by_person.setdefault(r["completed_by"] or "(no name)", {"rolls": 0, "labels": 0})
        w["rolls"] += 1; w["labels"] += r["quantity"]
    open_rolls = q("""SELECT r.job_id, r.roll_number, r.status, r.printer, r.started_by, r.started_at, r.paused_by, r.paused_at,
                             j.customer, j.job_ticket, j.job_number,
                             (SELECT body FROM job_notes n WHERE n.job_id = r.job_id AND n.roll_number = r.roll_number
                               ORDER BY n.id DESC LIMIT 1) AS last_note
                      FROM job_rolls r JOIN jobs j ON j.id = r.job_id
                      WHERE r.status IN ('running','paused') AND j.archived_at IS NULL ORDER BY j.job_ticket, r.roll_number""")
    status = q("""SELECT a.ts, a.actor, a.message, a.job_id FROM activity a
                  WHERE a.kind = 'status' AND a.ts >= ? AND a.ts < ? ORDER BY a.ts""", s, e)
    holds = q("""SELECT id, customer, job_ticket, job_number, hold_reason, hold_note, hold_since, hold_by FROM jobs
                 WHERE status = 'On Hold' AND archived_at IS NULL ORDER BY hold_since""")
    notes = q("""SELECT n.created_at, n.author, n.body, n.roll_number, j.customer, j.job_ticket, j.job_number
                 FROM job_notes n JOIN jobs j ON j.id = n.job_id
                 WHERE n.important = 1 AND n.created_at >= ? AND n.created_at < ? ORDER BY n.created_at""", s, e)
    reprints = q("""SELECT p.*, j.customer, j.job_ticket, j.job_number FROM reprints p JOIN jobs j ON j.id = p.job_id
                    WHERE p.ts >= ? AND p.ts < ? ORDER BY p.ts""", s, e)
    fails = q("""SELECT c.*, j.customer, j.job_ticket, j.job_number FROM roll_checks c JOIN jobs j ON j.id = c.job_id
                 WHERE c.result = 'fail' AND c.ts >= ? AND c.ts < ? ORDER BY c.ts""", s, e)
    verified = conn.execute("SELECT COUNT(*) FROM job_rolls WHERE verified_at >= ? AND verified_at < ?", (s, e)).fetchone()[0]
    prints = q("""SELECT state, COUNT(*) n FROM print_requests WHERE created_at >= ? AND created_at < ? GROUP BY state""", s, e)
    print_fails = q("""SELECT p.*, j.customer, j.job_ticket, j.job_number FROM print_requests p JOIN jobs j ON j.id = p.job_id
                       WHERE p.state = 'failed' AND p.created_at >= ? AND p.created_at < ?""", s, e)
    hours = max(0.25, (min(end, datetime.now()) - start).total_seconds() / 3600)
    return {"start": s, "end": e, "rolls_done": len(done), "labels_done": sum(r["quantity"] for r in done),
            "labels_per_hour": int(sum(r["quantity"] for r in done) / hours),
            "by_job": sorted(by_job.values(), key=lambda g: -g["labels"]),
            "by_printer": dict(sorted(by_printer.items())), "by_person": dict(sorted(by_person.items())),
            "open_rolls": open_rolls, "status_changes": status, "holds": holds, "important_notes": notes,
            "reprints": reprints, "reprinted_labels": sum(r["qty"] for r in reprints), "tag_check_fails": fails,
            "rolls_verified": verified, "print_requests": {r["state"]: r["n"] for r in prints}, "print_failures": print_fails}


def ranges_text(nums):
    nums, out, i = sorted(nums), [], 0
    while i < len(nums):
        k = i
        while k + 1 < len(nums) and nums[k + 1] == nums[k] + 1:
            k += 1
        out.append(f"{nums[i]}–{nums[k]}" if k > i else str(nums[i])); i = k + 1
    return ", ".join(out)


def shift_html(d, label, auto_print=False):
    """The shift summary as a printable page."""
    E = lambda v: escape(str(v if v is not None else ""))
    f = lambda n: f"{int(n):,}"
    t = lambda ts: E(str(ts)[5:16].replace("-", "/")) if ts else ""
    rows = lambda items, cols: "".join("<tr>" + "".join(f"<td>{c(x)}</td>" for c in cols) + "</tr>" for x in items)
    sec = lambda title, body, empty="None.": f"<h2>{title}</h2>" + (body if body else f'<p class="none">{empty}</p>')
    jobs = rows(d["by_job"], [lambda g: f"<b>{E(g['label'])}</b> <span class=m>{E(g['title'])}</span>",
                              lambda g: f"roll{'s' if len(g['rolls']) > 1 else ''} {E(ranges_text(g['rolls']))}", lambda g: f"{f(g['labels'])} labels"])
    printers = rows(d["by_printer"].items(), [lambda kv: E(kv[0]), lambda kv: f"{kv[1]['rolls']} roll{'s' if kv[1]['rolls'] != 1 else ''}", lambda kv: f"{f(kv[1]['labels'])} labels"])
    people = rows(d["by_person"].items(), [lambda kv: E(kv[0]), lambda kv: f"{kv[1]['rolls']} roll{'s' if kv[1]['rolls'] != 1 else ''}", lambda kv: f"{f(kv[1]['labels'])} labels"])
    openr = rows(d["open_rolls"], [lambda r: f"<b>{E(r['customer'])} {E(r['job_ticket'] or r['job_number'])}</b> roll {r['roll_number']}",
                                   lambda r: E(r["printer"] or "no printer"),
                                   lambda r: f"{'left' if r['status'] == 'paused' else 'started'} by {E(r['paused_by'] or r['started_by'] or '?')} {t(r['paused_at'] or r['started_at'])}",
                                   lambda r: E(r["last_note"] or "")])
    holds = rows(d["holds"], [lambda h: f"<b>{E(h['customer'])} {E(h['job_ticket'] or h['job_number'])}</b>",
                              lambda h: E(f"{h['hold_reason']}: {h['hold_note']}" if h["hold_reason"] else "no reason recorded"),
                              lambda h: f"since {t(h['hold_since'])}"])
    status = rows(d["status_changes"], [lambda a: t(a["ts"]), lambda a: E(a["message"]), lambda a: E(a["actor"] or "")])
    notes = rows(d["important_notes"], [lambda n: t(n["created_at"]), lambda n: f"<b>{E(n['customer'])} {E(n['job_ticket'] or n['job_number'])}</b>"
                                        + (f" roll {n['roll_number']}" if n["roll_number"] else ""), lambda n: E(n["body"]), lambda n: E(n["author"] or "")])
    reps = rows(d["reprints"], [lambda p: t(p["ts"]), lambda p: f"<b>{E(p['customer'])} {E(p['job_ticket'] or p['job_number'])}</b> roll {p['roll_number']}",
                                lambda p: f"{f(p['qty'])} labels" + (f" (serials {f(p['serial_from'])}–{f(p['serial_to'])})" if p["serial_from"] else ""),
                                lambda p: E(p["reason"] + (f": {p['note']}" if p["note"] else "")), lambda p: E(p["who"] or "")])
    fails = rows(d["tag_check_fails"], [lambda c: t(c["ts"]), lambda c: f"<b>{E(c['customer'])} {E(c['job_ticket'] or c['job_number'])}</b>"
                                        + (f" roll {c['roll_number']}" if c["roll_number"] else ""), lambda c: E(c["message"]), lambda c: E(c["who"] or "")])
    pf = rows(d["print_failures"], [lambda p: t(p["created_at"]), lambda p: f"<b>{E(p['customer'])} {E(p['job_ticket'] or p['job_number'])}</b> roll {p['roll_number']}",
                                    lambda p: E(p["message"] or "")])
    tbl = lambda body: f"<table>{body}</table>" if body else ""
    pr = d["print_requests"]
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Shift summary {E(label)}</title>
<style>body{{font:13px/1.45 "Segoe UI",Arial,sans-serif;color:#1B2A2E;margin:24px;max-width:1000px}}h1{{font-size:22px;margin:0}}
h2{{font-size:15px;margin:22px 0 6px;border-bottom:1px solid #ccd;padding-bottom:3px}}.sub{{color:#5E6E72;margin:2px 0 14px}}
.tiles{{display:flex;gap:10px;flex-wrap:wrap}}.tile{{border:1px solid #D3DADB;border-radius:8px;padding:8px 12px;min-width:120px}}
.tile b{{display:block;font-size:20px}}table{{border-collapse:collapse;width:100%}}td{{padding:4px 8px;border-bottom:1px solid #EEF1F1;vertical-align:top}}
.m,.none{{color:#5E6E72}}.bad{{color:#B3322B}}@page{{margin:.5in}}@media print{{button{{display:none}}}}</style></head><body>
<h1>Shift summary</h1><div class="sub">{E(label)} · made {datetime.now():%Y-%m-%d %H:%M} <button onclick="print()">Print</button></div>
<div class="tiles"><div class="tile"><b>{d['rolls_done']}</b>rolls done</div><div class="tile"><b>{f(d['labels_done'])}</b>labels</div>
<div class="tile"><b>{f(d['labels_per_hour'])}</b>labels / hour</div><div class="tile"><b>{len(d['open_rolls'])}</b>rolls left in progress</div>
<div class="tile"><b>{d['rolls_verified']}</b>rolls verified</div><div class="tile"><b>{f(d['reprinted_labels'])}</b>labels reprinted</div>
<div class="tile"><b class="{'bad' if d['tag_check_fails'] else ''}">{len(d['tag_check_fails'])}</b>failed tag checks</div>
<div class="tile"><b>{pr.get('printed', 0) + pr.get('taken', 0)}</b>printed by BarTender{f", <span class=bad>{pr['failed']} failed</span>" if pr.get('failed') else ''}</div></div>
{sec("Rolls left in progress (pick these up)", tbl(openr))}
{sec("Jobs on hold", tbl(holds))}
{sec("Important notes this shift", tbl(notes))}
{sec("Rolls done, by job", tbl(jobs))}
{sec("By printer", tbl(printers))}{sec("By person", tbl(people))}
{sec("Job status changes", tbl(status))}
{sec("Reprints and spoiled labels", tbl(reps))}
{sec("Failed tag checks", tbl(fails))}
{sec("BarTender print failures", tbl(pf))}
{'<script>addEventListener("load",()=>setTimeout(()=>print(),300))</script>' if auto_print else ''}</body></html>"""
