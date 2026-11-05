"""
Encoding Checklist Generator — single-page printable checklist saved in the job folder.
"""
import os, math, json
from html import escape as _esc
from datetime import datetime


def generate_checklist_html(job_dir, job_data):
    result = {"success": False, "path": None, "error": None}
    try:
        job_number   = _esc(str(job_data.get("job_number",  "")))
        customer     = _esc(str(job_data.get("customer",    "")))
        job_ticket   = _esc(str(job_data.get("job_ticket",  "") or ""))
        po_number    = _esc(str(job_data.get("po_number",   "") or ""))
        part_number  = _esc(str(job_data.get("part_number", "") or ""))
        inlay_type   = _esc(str(job_data.get("inlay_type",  "") or ""))
        label_size   = _esc(str(job_data.get("label_size",  "") or ""))
        title        = _esc(str(job_data.get("title",       "") or ""))
        upc          = _esc(str(job_data.get("upc",         "") or ""))
        quantity     = int(job_data.get("quantity")     or 0)
        lpr          = int(job_data.get("lpr")          or 0)
        start_serial = int(job_data.get("start_serial") or 0)
        overage_pct  = float(job_data.get("overage_pct") or 0)
        due_date     = _esc(str(job_data.get("due_date", "") or ""))

        # Production quantity includes overage (previously read two boolean
        # flags the app never sent, so overage was always shown as "None").
        prod_qty    = int(round(quantity * (1 + overage_pct / 100.0))) if overage_pct > 0 else quantity
        qty_db       = int(job_data.get("qty_db") or prod_qty or 0)
        ov_str      = f"+{overage_pct:g}%" if overage_pct > 0 else "None"
        num_rolls   = math.ceil(prod_qty / lpr) if lpr > 0 else 0
        stop_serial = (start_serial + prod_qty - 1) if (start_serial and prod_qty) else 0
        num_dbs     = math.ceil(prod_qty / qty_db) if qty_db > 0 else 0
        # Data added after the job was made: the original block is unchanged; each addition is listed.
        added = job_data.get("additions") or []
        if added:
            prod_qty += sum(int(a.get("quantity") or 0) for a in added)
            num_rolls = int(job_data.get("roll_count") or num_rolls)
            num_dbs += sum(len(json.loads(a["files"])) if a.get("files") else 0 for a in added)
        added_html = "".join(
            f'<div class="serial-cell"><div class="serial-lbl">Added #{int(a["seq"])} ({int(a["quantity"]):,})</div>'
            f'<div class="serial-val">' + (f'{int(a["serial_start"]):,} – {int(a["serial_end"]):,}' if a.get("serial_start") else "—")
            + '</div></div>' for a in added)
        date_str    = datetime.now().strftime("%B %d, %Y")

        # ── helpers ────────────────────────────────────────────────────────────
        def row(label, value, mono=False):
            cls = ' mono' if mono else ''
            v   = str(value) if value else ""
            return (f'<div class="r"><div class="rl">{label}</div>'
                    f'<div class="rv{cls}">{v}</div></div>')

        def chk(text):
            return (f'<div class="chk">'
                    f'<div class="chk-box"></div><span>{text}</span></div>')

        def wr(label):
            return (f'<div class="wline"><span class="wl">{label}</span>'
                    f'<div class="wl-rule"></div></div>')

        def stat(label, value, mono=False):
            """A single stat cell in the encoding data block."""
            cls = ' font-family:\'Courier New\',monospace;letter-spacing:.5px;' if mono else ''
            return (f'<div class="stat">'
                    f'<div class="stat-l">{label}</div>'
                    f'<div class="stat-v" style="{cls}">{value if value else "—"}</div>'
                    f'</div>')

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Encoding Checklist — {customer}</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{
  font-family:'Inter',-apple-system,Arial,sans-serif;
  font-size:9pt;color:#0f172a;background:#fff;
  padding:.4in .45in .35in;line-height:1.4;
}}

/* ── HEADER ──────────────────────────────────────────────── */
header{{
  display:flex;justify-content:space-between;align-items:flex-start;
  margin-bottom:16pt;
}}
.brand-name{{font-size:7pt;font-weight:600;letter-spacing:.14em;
  text-transform:uppercase;color:#94a3b8;margin-bottom:2pt}}
.doc-title{{font-size:20pt;font-weight:700;letter-spacing:-.5px;line-height:1}}
.accent-bar{{width:28pt;height:2.5pt;background:#2563eb;border-radius:2pt;margin-top:4pt}}
.header-meta{{text-align:right;display:flex;flex-direction:column;gap:2pt}}
.job-num{{font-size:12pt;font-weight:700;color:#2563eb;margin-bottom:2pt}}
.meta-line{{font-size:8pt;color:#64748b}}
.meta-line span{{font-weight:600;color:#0f172a}}

/* ── DIVIDER LABEL ───────────────────────────────────────── */
.divlabel{{
  font-size:6.5pt;font-weight:700;letter-spacing:.13em;text-transform:uppercase;
  color:#94a3b8;margin-bottom:5pt;margin-top:11pt;
  display:flex;align-items:center;gap:8pt;
}}
.divlabel::after{{content:'';flex:1;height:.5pt;background:#e2e8f0}}

/* ── JOB INFO PANEL (top section) ───────────────────────── */
.info-panel{{
  border:1pt solid #e2e8f0;border-radius:6pt;overflow:hidden;margin-bottom:10pt;
}}
.info-grid{{display:grid;grid-template-columns:1fr 1fr}}
.r{{
  display:flex;align-items:baseline;gap:7pt;
  padding:4.5pt 10pt;border-bottom:.75pt solid #f1f5f9;
  border-right:.75pt solid #f1f5f9;
}}
.r:nth-child(even){{border-right:none}}
.r:last-child,.r:nth-last-child(2){{border-bottom:none}}
.rl{{font-size:7.5pt;font-weight:600;color:#64748b;
  white-space:nowrap;min-width:72pt;flex-shrink:0}}
.rv{{
  font-size:9pt;font-weight:500;color:#0f172a;
  flex:1;border-bottom:.75pt solid #cbd5e1;
  min-height:12pt;padding-bottom:1pt;
}}
.rv.mono{{font-family:'Courier New',monospace;font-size:8.5pt}}

/* ── ENCODING DATA BLOCK ─────────────────────────────────── */
.enc-block{{
  background:#f8fafc;border:1.5pt solid #e2e8f0;
  border-radius:7pt;overflow:hidden;margin-bottom:10pt;
}}
.enc-head{{
  background:#0f172a;color:#fff;padding:5pt 12pt;
  font-size:7pt;font-weight:700;letter-spacing:.13em;text-transform:uppercase;
  display:flex;align-items:center;justify-content:space-between;
}}
.enc-head-sub{{font-weight:400;color:#94a3b8;font-size:7pt;letter-spacing:0}}

/* UPC row */
.upc-row{{
  display:flex;align-items:center;gap:12pt;
  padding:8pt 12pt 7pt;border-bottom:1pt solid #e2e8f0;
}}
.upc-label{{
  font-size:6.5pt;font-weight:700;letter-spacing:.1em;
  text-transform:uppercase;color:#64748b;
  white-space:nowrap;
}}
.upc-val{{
  font-family:'Courier New',monospace;
  font-size:15pt;font-weight:700;color:#0f172a;
  letter-spacing:3px;line-height:1;
}}

/* Serials row */
.serials-row{{
  display:grid;grid-template-columns:1fr 1fr;
  border-bottom:1pt solid #e2e8f0;
}}
.serial-cell{{
  padding:6pt 12pt;
}}
.serial-cell:first-child{{border-right:1pt solid #e2e8f0}}
.serial-lbl{{
  font-size:6.5pt;font-weight:700;letter-spacing:.1em;
  text-transform:uppercase;color:#64748b;margin-bottom:2pt;
}}
.serial-val{{
  font-family:'Courier New',monospace;
  font-size:13pt;font-weight:700;color:#1d4ed8;
  letter-spacing:1px;line-height:1;
}}

/* Stats row — quantities */
.stats-row{{
  display:grid;grid-template-columns:repeat(5,1fr);
}}
.stat{{
  padding:5pt 12pt;border-right:1pt solid #e2e8f0;
}}
.stat:last-child{{border-right:none}}
.stat-l{{
  font-size:6.5pt;font-weight:700;letter-spacing:.09em;
  text-transform:uppercase;color:#94a3b8;margin-bottom:2pt;
}}
.stat-v{{
  font-size:10.5pt;font-weight:700;color:#0f172a;line-height:1;
}}

/* ── LOWER COLUMNS ───────────────────────────────────────── */
.lower{{display:grid;grid-template-columns:1fr 1fr;gap:10pt;margin-bottom:10pt}}

/* Setup write-ins */
.setup-panel{{border:1pt solid #e2e8f0;border-radius:6pt;overflow:hidden}}
.panel-head{{
  background:#f8fafc;border-bottom:1pt solid #e2e8f0;
  padding:3.5pt 10pt;font-size:7pt;font-weight:700;
  letter-spacing:.1em;text-transform:uppercase;color:#64748b;
}}
.setup-body{{padding:5pt 10pt;display:flex;flex-direction:column}}
.wline{{
  display:flex;align-items:center;gap:8pt;
  padding:4pt 0;border-bottom:.75pt solid #f1f5f9;font-size:8.5pt;
}}
.wline:last-child{{border-bottom:none}}
.wl{{font-size:7.5pt;font-weight:600;color:#64748b;
  white-space:nowrap;min-width:52pt;flex-shrink:0}}
.wl-rule{{flex:1;border-bottom:.75pt solid #cbd5e1;min-height:12pt}}
.opts{{display:flex;gap:9pt;align-items:center}}
.opt{{display:flex;align-items:center;gap:4pt;font-size:8.5pt}}
.opt-box{{width:9pt;height:9pt;border:1.25pt solid #94a3b8;
  border-radius:1.5pt;flex-shrink:0}}

/* Checklist */
.chk-panel{{border:1pt solid #e2e8f0;border-radius:6pt;overflow:hidden}}
.chklist{{padding:5pt 10pt;display:flex;flex-direction:column}}
.chk{{
  display:flex;align-items:center;gap:8pt;
  padding:4pt 0;border-bottom:.75pt solid #f1f5f9;
  font-size:8.5pt;color:#0f172a;
}}
.chk:last-child{{border-bottom:none}}
.chk-box{{width:11pt;height:11pt;border:1.25pt solid #94a3b8;
  border-radius:2pt;flex-shrink:0}}

/* ── NOTES ───────────────────────────────────────────────── */
.notes-block{{
  border:1pt solid #e2e8f0;border-radius:6pt;
  overflow:hidden;margin-bottom:10pt;
}}
.notes-lines{{padding:5pt 10pt 4pt;display:flex;flex-direction:column;gap:10pt}}
.note-rule{{border-bottom:.75pt solid #e2e8f0;height:14pt}}

/* ── SIGNATURES ──────────────────────────────────────────── */
.sigs{{display:grid;grid-template-columns:repeat(4,1fr);gap:12pt}}
.sig-rule{{border-bottom:1pt solid #0f172a;height:16pt;margin-bottom:3pt}}
.sig-lbl{{font-size:6.5pt;font-weight:600;letter-spacing:.08em;
  text-transform:uppercase;color:#94a3b8}}

/* print */
.no-print{{display:flex;justify-content:flex-end;margin-bottom:10px}}
@page{{size:letter portrait;margin:.4in .45in .35in}}
@media print{{
  .no-print{{display:none!important}}
  body{{padding:0}}
}}
</style>
</head>
<body>

<div class="no-print">
  <button onclick="window.print()"
    style="padding:5px 20px;background:#0f172a;color:#fff;border:none;
           border-radius:4px;cursor:pointer;font-size:10pt;font-weight:500;
           letter-spacing:.02em">Print</button>
</div>

<!-- HEADER -->
<header>
  <div>
    <div class="brand-name">Starport &mdash; MCC Company</div>
    <div class="doc-title">Encoding Checklist</div>
    <div class="accent-bar"></div>
  </div>
  <div class="header-meta">
    <div class="job-num">{job_number}</div>
    <div class="meta-line">Date &nbsp;<span>{date_str}</span></div>
    {f'<div class="meta-line">Due &nbsp;<span>{due_date}</span></div>' if due_date else ''}
  </div>
</header>

<!-- JOB INFO -->
<div class="divlabel">Job Details</div>
<div class="info-panel">
  <div class="info-grid">
    {row("Customer", customer)}
    {row("Item", title)}
    {row("Job Ticket #", job_ticket)}
    {row("Customer PO #", po_number)}
    {row("Part #", part_number)}
    {row("Inlay Type", inlay_type)}
    {row("Label Size", label_size)}
    {row("Overage", ov_str)}
  </div>
</div>

<!-- ENCODING DATA -->
<div class="divlabel">Encoding Data</div>
<div class="enc-block">

  <div class="enc-head">
    <span>Encoding Parameters</span>
    <span class="enc-head-sub">System-allocated &amp; locked</span>
  </div>

  <!-- UPC -->
  <div class="upc-row">
    <div class="upc-label">UPC</div>
    <div class="upc-val">{upc if upc else "—"}</div>
  </div>

  <!-- Serials -->
  <div class="serials-row">
    <div class="serial-cell">
      <div class="serial-lbl">Start Serial</div>
      <div class="serial-val">{f"{start_serial:,}" if start_serial else "—"}</div>
    </div>
    <div class="serial-cell">
      <div class="serial-lbl">Stop Serial</div>
      <div class="serial-val">{f"{stop_serial:,}" if stop_serial else "—"}</div>
    </div>
    {added_html}
  </div>

  <!-- Quantity stats -->
  <div class="stats-row">
    {stat("Base QTY", f"{quantity:,}" if quantity else "—")}
    {stat("Prod. QTY", f"{prod_qty:,}" if prod_qty else "—")}
    {stat("LPR", f"{lpr:,}" if lpr else "—")}
    {stat("Rolls", f"{num_rolls}" if num_rolls else "—")}
    {stat("DB Files", f"{num_dbs}" if num_dbs else "—")}
  </div>

</div>

<!-- SETUP + CHECKLIST -->
<div class="divlabel">Setup &amp; Verification</div>
<div class="lower">

  <div class="setup-panel">
    <div class="panel-head">Setup</div>
    <div class="setup-body">
      {wr("Printer")}
      <div class="wline">
        <span class="wl">Speed</span><div class="wl-rule"></div>
        <span class="wl" style="min-width:44pt;padding-left:8pt">Darkness</span>
        <div class="wl-rule"></div>
      </div>
      {wr("Ribbon")}
      <div class="wline">
        <span class="wl">Software</span>
        <div class="opts">
          <div class="opt"><div class="opt-box"></div> Bartender</div>
          <div class="opt"><div class="opt-box"></div> Nice Label</div>
          <div class="opt"><div class="opt-box"></div> Other</div>
        </div>
      </div>
      <div class="wline">
        <span class="wl">Format</span>
        <div class="opts">
          <div class="opt"><div class="opt-box"></div> HEX</div>
          <div class="opt"><div class="opt-box"></div> ASCII</div>
          <div class="opt"><div class="opt-box"></div> Other</div>
        </div>
      </div>
    </div>
  </div>

  <div class="chk-panel">
    <div class="panel-head">Verification</div>
    <div class="chklist">
      {chk("Label stock loaded — correct size and type")}
      {chk("Ribbon correct for substrate")}
      {chk("Inlay orientation confirmed")}
      {chk("RFID calibration complete")}
      {chk("DB file loaded — start serial verified")}
      {chk("Test batch encoded and inspected")}
      {chk("Production run complete — all rolls tracked")}
      {chk("Stop serial matches expected")}
      {chk("Start / stop emails sent")}
      {chk("Job status updated in Job Manager")}
    </div>
  </div>

</div>

<!-- NOTES -->
<div class="notes-block">
  <div class="panel-head">Notes</div>
  <div class="notes-lines">
    <div class="note-rule"></div>
    <div class="note-rule"></div>
  </div>
</div>

<!-- SIGNATURES -->
<div class="sigs">
  <div><div class="sig-rule"></div><div class="sig-lbl">Completed by</div></div>
  <div><div class="sig-rule"></div><div class="sig-lbl">Peer Approval</div></div>
  <div><div class="sig-rule"></div><div class="sig-lbl">Supervisor</div></div>
  <div><div class="sig-rule"></div><div class="sig-lbl">Initials</div></div>
</div>

</body>
</html>"""

        os.makedirs(job_dir, exist_ok=True)
        out_path = os.path.join(job_dir, f"Encoding Checklist - {job_number}.html")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        result["success"] = True
        result["path"]    = out_path

    except Exception as e:
        result["error"] = str(e)

    return result
