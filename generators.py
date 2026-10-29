"""
generators.py — everything that turns job numbers into encoding data
====================================================================
  • SGTIN-96 EPC encoding (one copy, used everywhere)
  • UPC-12 validation
  • Roll plan (serial / EPC range per roll)
  • Encoding database files (Data/*.xlsx)
  • Quality Control sheet (job folder root)
"""
import os
import csv
import math
from html import escape

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    XLSX_AVAILABLE = True
except ImportError:          # pragma: no cover
    XLSX_AVAILABLE = False


# ── EPC ───────────────────────────────────────────────────────────────────────
def generate_epc(upc: str, serial: int) -> str:
    """12-digit UPC + serial → 24-char hex SGTIN-96 (filter 1, partition 5)."""
    company = int("0" + upc[:6])
    item    = int(upc[6:11])
    bits = ("00110000" + "001" + "101"
            + format(company, "024b")
            + format(item,    "020b")
            + format(int(serial), "038b"))
    return format(int(bits, 2), "024X")


def decode_epc(epc_hex: str) -> dict:
    bits = format(int(epc_hex, 16), "096b")
    company = str(int(bits[14:38], 2)).zfill(7)
    item    = str(int(bits[38:58], 2)).zfill(5)
    upc11   = company[1:] + item
    return {"upc": upc11 + str(upc_check_digit(upc11)), "serial": int(bits[58:], 2)}


def upc_check_digit(first11: str) -> int:
    d = [int(c) for c in first11]
    return (10 - ((sum(d[0::2]) * 3 + sum(d[1::2])) % 10)) % 10


def validate_upc(upc: str) -> dict:
    """{valid, error, corrected_upc?}. Includes an encode→decode round trip."""
    upc = (upc or "").strip()
    if not upc:
        return {"valid": False, "error": "Enter a UPC"}
    if not upc.isdigit():
        return {"valid": False, "error": "UPC can only contain digits"}
    if len(upc) != 12:
        return {"valid": False, "error": f"UPC needs 12 digits ({len(upc)} entered)"}
    expected = upc_check_digit(upc[:11])
    if expected != int(upc[11]):
        fixed = upc[:11] + str(expected)
        return {"valid": False, "corrected_upc": fixed,
                "error": f"Check digit should be {expected}. Did you mean {fixed}?"}
    if decode_epc(generate_epc(upc, 1))["upc"] != upc:
        return {"valid": False, "error": "This UPC does not survive EPC encoding"}
    return {"valid": True, "error": None}


# ── Quantities ────────────────────────────────────────────────────────────────
def production_qty(quantity: int, overage_pct: float) -> int:
    if overage_pct and overage_pct > 0:
        return int(round(quantity * (1 + overage_pct / 100.0)))
    return int(quantity)


def roll_plan(upc: str, start_serial: int, total_qty: int, lpr: int) -> list:
    """Split total_qty into rolls of lpr. Returns list of dicts."""
    if not (upc and start_serial and total_qty and lpr and lpr > 0):
        return []
    rolls, cur, n, left = [], int(start_serial), 1, int(total_qty)
    while left > 0:
        q = min(lpr, left)
        rolls.append({
            "roll_number":  n,
            "quantity":     q,
            "serial_start": cur,
            "serial_end":   cur + q - 1,
            "epc_start":    generate_epc(upc, cur),
            "epc_end":      generate_epc(upc, cur + q - 1),
        })
        cur += q; left -= q; n += 1
    return rolls


def roll_plan_plain(total_qty: int, lpr: int) -> list:
    """Rolls by quantity only, for special jobs without a UPC / serials."""
    if not (total_qty and lpr and lpr > 0):
        return []
    rolls, n, left = [], 1, int(total_qty)
    while left > 0:
        q = min(lpr, left)
        rolls.append({"roll_number": n, "quantity": q, "serial_start": None, "serial_end": None,
                      "epc_start": None, "epc_end": None})
        left -= q; n += 1
    return rolls


def db_file_split(total_qty: int, qty_db: int | None) -> list:
    """Row counts per DB file. Blank/zero qty_db = one file with everything."""
    if not qty_db or qty_db <= 0 or qty_db >= total_qty:
        return [total_qty]
    return [min(qty_db, total_qty - i * qty_db) for i in range(math.ceil(total_qty / qty_db))]


# ── Encoding database files ───────────────────────────────────────────────────
def generate_db_files(job_dir: str, upc: str, start_serial: int,
                      total_qty: int, qty_db: int | None) -> dict:
    data_dir = os.path.join(job_dir, "Data")
    os.makedirs(data_dir, exist_ok=True)
    split   = db_file_split(total_qty, qty_db)
    serial  = int(start_serial)
    files   = []
    for idx, rows in enumerate(split, start=1):
        records = []
        for _ in range(rows):
            records.append((serial, upc, generate_epc(upc, serial)))
            serial += 1
        suffix = f"_DB{idx}" if len(split) > 1 else ""
        if XLSX_AVAILABLE:
            path = os.path.join(data_dir, f"{upc}{suffix}.xlsx")
            _write_xlsx(path, records, f"DB{idx}" if len(split) > 1 else "Database")
        else:
            path = os.path.join(data_dir, f"{upc}{suffix}.csv")
            with open(path, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh); w.writerow(["Serial #", "UPC", "EPC"]); w.writerows(records)
        files.append(path)
    return {"files": files, "file_count": len(files), "split": split,
            "start_serial": int(start_serial), "end_serial": serial - 1}


def _write_xlsx(path, records, sheet_title):
    wb = Workbook(write_only=False)
    ws = wb.active
    ws.title = sheet_title
    thin   = Side(style="thin", color="CBD5E0")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for col, (head, width) in enumerate(zip(["Serial #", "UPC", "EPC"], [14, 16, 28]), start=1):
        c = ws.cell(row=1, column=col, value=head)
        c.font = Font(bold=True, color="FFFFFF"); c.border = border
        c.fill = PatternFill("solid", fgColor="1F4E79")
        c.alignment = Alignment(horizontal="center")
        ws.column_dimensions[c.column_letter].width = width
    mono = Font(name="Courier New", size=10)
    for r, (serial, upc, epc) in enumerate(records, start=2):
        ws.cell(row=r, column=1, value=serial)
        ws.cell(row=r, column=2, value=upc)
        ws.cell(row=r, column=3, value=epc).font = mono
    ws.freeze_panes = "A2"
    wb.save(path)


# ── Quality Control sheet ─────────────────────────────────────────────────────
# Same layout as the original QC sheet the floor already uses. The only addition:
# a blank write-in area under each roll for the rewinders. If that space would push
# the printout past QC_MAX_PAGES sheets, the sheet is printed without it.
QC_MAX_PAGES = 4

# Rolls per letter sheet, measured from Chrome's print output (0.5in margins).
# With write-in space: 4 on the first sheet (title + job info), 8 per sheet after.
_QC_FIT = {True: (4, 8), False: (13, 26)}


def _safe_name(s: str) -> str:
    for ch in '\\/:*?"<>|':
        s = s.replace(ch, "-")
    return s.strip()


def qc_page_estimate(n_rolls: int, write_in: bool) -> int:
    first, later = _QC_FIT[write_in]
    if n_rolls <= first:
        return 1
    return 1 + -(-(n_rolls - first) // later)


def qc_html(job: dict, rolls: list, auto_print: bool = False) -> tuple[str, bool]:
    """Returns (html, has_write_in_space)."""
    base  = int(job.get("quantity") or 0)
    total = production_qty(base, float(job.get("overage_pct") or 0))
    write_in = bool(rolls) and qc_page_estimate(len(rolls), True) <= QC_MAX_PAGES
    e = lambda v: escape(str(v or ""))

    info_items = [
        ("Customer",         job.get("customer")),
        ("Job Ticket #:",    job.get("job_ticket")),
        ("PO #:",            job.get("po_number")),
        ("Part #:",          job.get("part_number")),
        ("Item:",            job.get("title")),
        ("Due Date:",        job.get("due_date")),
        ("UPC Number:",      job.get("upc")),
        ("Inlay Type:",      job.get("inlay_type")),
        ("Label Size:",      job.get("label_size")),
        ("Base Quantity:",   f"{base:,}"),
        ("Total Quantity:",  f"{total:,}"),
        ("Labels Per Roll:", f"{int(job.get('lpr') or 0):,}"),
    ]
    info_html = "".join(
        f'<div class="info-item"><span class="label">{e(k)}</span> <span class="value">{e(v)}</span></div>\n'
        for k, v in info_items)

    rows = ""
    for r in rolls:
        row = f"""
<tr>
  <td class="roll-number">{r['roll_number']}</td>
  <td class="quantity">{r['quantity']:,}</td>
  <td class="serial-range">{f"{r['serial_start']:,} - {r['serial_end']:,}" if r.get('serial_start') else ""}</td>
  <td class="epc">{e((r.get('epc_start') or '')[-5:])}</td>
  <td class="epc">{e((r.get('epc_end') or '')[-5:])}</td>
</tr>"""
        if write_in:
            row += """
<tr class="write-in"><td colspan="5"><div class="lines"><span></span><span></span><span></span></div></td></tr>"""
            rows += f"\n<tbody class=\"roll\">{row}\n</tbody>"
        else:
            rows += row
    body = rows if write_in else f"\n      <tbody>{rows}\n      </tbody>"

    extra_css = ""
    if write_in:
        extra_css = """
/* write-in space under each roll for the rewinders */
.roll-table tr.write-in td { padding: 0.04in 0.12in 0.06in; border-top: none; text-align: left; }
.roll-table tbody.roll tr:first-child td { border-bottom: 1px solid #bbb; }
.write-in .lines span { display: block; height: 0.22in; border-bottom: 1px dotted #aaa; }
.roll-table tbody.roll { break-inside: avoid; page-break-inside: avoid; }"""
    # The original sheet kept the whole table on one page, which pushed longer tables
    # to page 2 and left page 1 mostly blank. Rows still never split across pages.
    section_avoid = "\n    .roll-table tr { break-inside: avoid; page-break-inside: avoid; }"
    table_avoid = ""
    printer = ("\n<script>window.addEventListener('load',()=>setTimeout(()=>window.print(),300));</script>"
               if auto_print else "")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Quality Control Sheet - {e(job.get('customer'))} - {e(job.get('job_ticket') or job.get('job_number'))}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    font-size: 12pt; line-height: 1.4; color: #333; background: white;
}}
.qc-sheet {{ width: 100%; max-width: none; margin: 0 auto; padding: 0.3in; background: white; }}
.header {{
    text-align: center; margin-bottom: 0.1in;
    border-bottom: 3px solid #333; padding-bottom: 0.1in;
}}
.header h1 {{ font-size: 24pt; font-weight: bold; color: #333; margin-bottom: 0.1in; }}
.job-info-section, .roll-tracking-section {{ margin-bottom: 0.3in; }}
h2 {{
    font-size: 16pt; font-weight: bold; color: #333;
    margin-bottom: 0.15in; border-bottom: 1px solid #ccc; padding-bottom: 0.05in;
}}
.info-grid {{
    display: grid; grid-template-columns: 1fr 1fr 1fr;
    gap: 0.1in; margin-bottom: 0.2in;
}}
.info-item {{ display: flex; padding: 0.05in 0; }}
.label {{ font-weight: bold; width: 1.5in; flex-shrink: 0; }}
.value {{ border-bottom: 1px dotted #999; flex-grow: 1; min-height: 1.2em; }}
.roll-table {{
    width: 100%; border-collapse: collapse;
    font-size: 11pt; margin-bottom: 0.2in;
}}
.roll-table th, .roll-table td {{
    border: 1px solid #333; padding: 0.08in;
    text-align: center; vertical-align: middle;
}}
.roll-table th {{ background-color: #f5f5f5; font-weight: bold; font-size: 10pt; }}
.roll-number {{ font-weight: bold; background-color: #fafafa; }}
.serial-range {{ font-family: 'Courier New', monospace; font-size: 10pt; }}
.epc {{ font-family: 'Courier New', monospace; font-size: 9pt; }}{extra_css}
@page {{ size: letter; margin: 0.5in; }}
@media print {{
    body {{ font-size: 11pt; }}
    .qc-sheet {{ max-width: none; margin: 0; padding: 0; }}
    .header h1 {{ font-size: 20pt; }}
    h2 {{ font-size: 14pt; }}
    .roll-table {{ font-size: 10pt;{table_avoid} }}
    .roll-table th {{ font-size: 9pt; }}{section_avoid}
}}
</style>
</head>
<body>
<div class="qc-sheet">

  <div class="header">
    <h1>QUALITY CONTROL</h1>
  </div>

  <div class="job-info-section">
    <h2>Job Information</h2>
    <div class="info-grid">
{info_html}    </div>
  </div>

  <div class="roll-tracking-section">
    <h2>Roll Tracking &amp; Quality Control</h2>
    <table class="roll-table">
      <thead>
        <tr>
          <th>Roll #</th>
          <th>Quantity</th>
          <th>Serial Range</th>
          <th>Start EPC</th>
          <th>End EPC</th>
        </tr>
      </thead>{body}
    </table>
  </div>

</div>{printer}
</body>
</html>"""
    return html, write_in


def qc_file_name(job: dict) -> str:
    parts = [job.get("customer"), job.get("part_number") or job.get("job_ticket"),
             job.get("po_clean") or job.get("job_number")]
    return _safe_name("-".join(str(p) for p in parts if p) + "-QualityControl.html")


def generate_qc_sheet(job_dir: str, job: dict, rolls: list) -> dict:
    """Printable QC / roll tracking sheet saved to the job folder root."""
    try:
        html, write_in = qc_html(job, rolls)
        path = os.path.join(job_dir, qc_file_name(job))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        return {"success": True, "path": path, "write_in": write_in, "error": None}
    except Exception as e:
        return {"success": False, "path": None, "error": str(e)}
