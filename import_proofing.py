"""
import_proofing.py — bring saved proofs over from the standalone RFID Proofing Utility
======================================================================================
Usage (stop Job Manager first):

    python import_proofing.py "C:\\Users\\...\\Desktop\\Proofing Test"            # import + link to jobs
    python import_proofing.py "C:\\...\\Proofing Test" --dry-run                  # show what would happen
    python import_proofing.py "C:\\...\\Proofing Test" --no-link                  # import only

Or drag the old Proofing Test folder onto import_proofing.bat.

Copies into the proofing data folder (config.ini [proofing] data_dir):
  • saved proofs: jobs\\, jobs\\approved\\, jobs\\archive\\ and the recovery snapshots in
    jobs\\history\\. A proof file that is already here is never overwritten.
  • the inlay library (config\\inlays.json + inlay_images), merged by inlay name
  • template profiles (config\\template_profiles.json), merged by template path
  • settings (config\\settings.json) only if there are none here yet
  • generated PDFs / packages from proofs\\ (skipped if already here) and proof_history.csv

Previews: the standalone app wrote every BarTender preview to the same file
(%LOCALAPPDATA%\\RFID_Proofing_Utility\\cache\\bartender\\preview_1.png), so an older proof
points at whatever was rendered last. Imported proofs that point into that cache lose the
link; regenerate the preview before generating a new PDF. Their existing PDFs are kept.

Linking: a proof whose Job # is the ticket of exactly one Job Manager job (same customer
when several share the ticket) is linked to it. Run it again any time; it skips what is
already here.
"""
import argparse
import csv
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import config                                    # noqa: E402

PATH_FIELDS = ("preview_path", "last_generated_output_path", "inlay_image_path")
CACHE_MARK = "rfid_proofing_utility"             # %LOCALAPPDATA%\RFID_Proofing_Utility\...


def app_is_running():
    try:
        with socket.create_connection(("127.0.0.1", config.PORT), timeout=0.5):
            return True
    except OSError:
        return False


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_json(path, data, dry):
    if dry:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _copy(src, dest, dry):
    if dry:
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy2(src, dest)


def _tail_after(path_value, markers=("proofs", "jobs", "config")):
    """'C:\\...\\Proofing Test\\proofs\\x.pdf' -> 'proofs/x.pdf' (from the last known folder)."""
    parts = [p for p in str(path_value or "").replace("\\", "/").split("/") if p]
    for i in range(len(parts) - 1, -1, -1):
        if parts[i].lower() in markers:
            return "/".join(parts[i:])
    return None


def fix_paths(job, src_root, dest_root):
    """Point an imported proof's file paths at the new data folder; drop shared-cache previews.
    Returns notes."""
    notes = []
    pv = str(job.get("preview_path") or "")
    if pv and CACHE_MARK in pv.replace("\\", "/").lower():
        job["preview_path"] = ""
        job["preview_fingerprint"] = ""
        job["preview_source"] = ""
        notes.append("preview link dropped (shared cache file): regenerate the preview before a new PDF")
    for key in ("last_generated_output_path", "inlay_image_path"):
        val = str(job.get(key) or "")
        if not val or not re.match(r"^([A-Za-z]:[\\/]|[\\/])", val):    # relative paths already resolve
            continue
        tail = _tail_after(val)
        if tail and os.path.isfile(os.path.join(src_root, *tail.split("/"))):
            job[key] = os.path.join(dest_root, *tail.split("/"))
    return notes


def import_proofs(src, dry=False, link=True):
    src = os.path.abspath(src)
    dest = os.path.abspath(config.PROOFING_DATA_DIR)
    if not (os.path.isdir(os.path.join(src, "jobs")) or os.path.isfile(os.path.join(src, "config", "settings.json"))):
        raise SystemExit(f"{src} doesn't look like an RFID Proofing Utility folder (no jobs\\ or config\\settings.json).")
    if os.path.normcase(src) == os.path.normcase(dest):
        raise SystemExit("The source is the proofing data folder itself; nothing to import.")
    report = {"proofs": 0, "proofs_skipped": 0, "snapshots": 0, "outputs": 0, "inlays_added": 0,
              "images": 0, "profiles_added": 0, "settings": "kept the current ones", "history_rows": 0,
              "previews_dropped": 0, "linked": 0, "ambiguous": 0, "no_match": 0}
    print(f"  From : {src}\n  To   : {dest}{'   (dry run: nothing is written)' if dry else ''}\n")

    # settings: only when there are none yet
    s_src, s_dest = os.path.join(src, "config", "settings.json"), os.path.join(dest, "config", "settings.json")
    if os.path.isfile(s_src) and not os.path.isfile(s_dest):
        data = _read_json(s_src, {})
        data["recent_job_files"] = []
        _write_json(s_dest, data, dry)
        report["settings"] = "copied"

    # inlay library, merged by name; images copied if missing
    img_src, img_dest = os.path.join(src, "config", "inlay_images"), os.path.join(dest, "config", "inlay_images")
    if os.path.isdir(img_src):
        for f in os.listdir(img_src):
            if os.path.isfile(os.path.join(img_src, f)) and not os.path.exists(os.path.join(img_dest, f)):
                _copy(os.path.join(img_src, f), os.path.join(img_dest, f), dry)
                report["images"] += 1
    mine = _read_json(os.path.join(dest, "config", "inlays.json"), [])
    names = {str(i.get("name", "")).strip().lower() for i in mine}
    for item in _read_json(os.path.join(src, "config", "inlays.json"), []):
        if isinstance(item, dict) and str(item.get("name", "")).strip().lower() not in names:
            mine.append(item)
            names.add(str(item.get("name", "")).strip().lower())
            report["inlays_added"] += 1
    if report["inlays_added"]:
        _write_json(os.path.join(dest, "config", "inlays.json"), mine, dry)

    # template profiles, merged by template path
    mine = _read_json(os.path.join(dest, "config", "template_profiles.json"), [])
    have = {str(p.get("template_path", "")).lower() for p in mine}
    for prof in _read_json(os.path.join(src, "config", "template_profiles.json"), []):
        if isinstance(prof, dict) and str(prof.get("template_path", "")).lower() not in have:
            mine.append(prof)
            have.add(str(prof.get("template_path", "")).lower())
            report["profiles_added"] += 1
    if report["profiles_added"]:
        _write_json(os.path.join(dest, "config", "template_profiles.json"), mine, dry)

    # generated PDFs / packages (proofs\ and proofs\archive\)
    for dirpath, _dirs, files in os.walk(os.path.join(src, "proofs")):
        for f in files:
            if f.lower().endswith((".pdf", ".zip")):
                rel = os.path.relpath(os.path.join(dirpath, f), src)
                if not os.path.exists(os.path.join(dest, rel)):
                    _copy(os.path.join(dirpath, f), os.path.join(dest, rel), dry)
                    report["outputs"] += 1

    # saved proofs and snapshots
    for sub in ("", "approved", "archive"):
        folder = os.path.join(src, "jobs", sub)
        if not os.path.isdir(folder):
            continue
        for f in sorted(os.listdir(folder)):
            if not f.endswith(".rfidproof.json"):
                continue
            target = os.path.join(dest, "jobs", sub, f)
            if os.path.exists(target):
                report["proofs_skipped"] += 1
                continue
            payload = _read_json(os.path.join(folder, f), None)
            if not isinstance(payload, dict):
                print(f"  [SKIP] {f}: not a readable proof file")
                continue
            job = payload.get("job", payload)
            notes = fix_paths(job, src, dest)
            report["previews_dropped"] += bool(notes)
            _write_json(target, payload, dry)
            report["proofs"] += 1
            print(f"  [PROOF] {os.path.join('jobs', sub, f)}" + (f"  ({'; '.join(notes)})" if notes else ""))
    for dirpath, _dirs, files in os.walk(os.path.join(src, "jobs", "history")):
        for f in files:
            if f.endswith(".snapshot.json"):
                rel = os.path.relpath(os.path.join(dirpath, f), src)
                if not os.path.exists(os.path.join(dest, rel)):
                    _copy(os.path.join(dirpath, f), os.path.join(dest, rel), dry)
                    report["snapshots"] += 1

    # proof history: rows not already here
    h_src, h_dest = os.path.join(src, "logs", "proof_history.csv"), os.path.join(dest, "logs", "proof_history.csv")
    if os.path.isfile(h_src):
        with open(h_src, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        existing, fields = [], (rows[0].keys() if rows else [])
        if os.path.isfile(h_dest):
            with open(h_dest, newline="", encoding="utf-8-sig") as fh:
                r = csv.DictReader(fh)
                existing, fields = list(r), r.fieldnames or fields
        seen = {json.dumps(x, sort_keys=True) for x in existing}
        new = [x for x in rows if json.dumps(x, sort_keys=True) not in seen]
        report["history_rows"] = len(new)
        if new and not dry:
            os.makedirs(os.path.dirname(h_dest), exist_ok=True)
            with open(h_dest, "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=list(fields), extrasaction="ignore")
                w.writeheader()
                w.writerows(existing + new)

    if link and not dry:
        report.update({k: v for k, v in link_to_jobs().items() if k in ("linked", "ambiguous", "no_match")})
    return report


def link_to_jobs():
    """Link proofs to jobs by ticket, through the same code the Proofing page uses."""
    import proofing
    from proofing import routes

    def get_db():
        c = sqlite3.connect(config.DB_PATH, timeout=15)
        c.row_factory = sqlite3.Row
        return c

    def log_activity(conn, kind, job_id, message, who=None):
        conn.execute("INSERT INTO activity (ts, job_id, kind, actor, message) VALUES (?,?,?,?,?)",
                     (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), job_id, kind, who or "Import", message))

    conn = get_db()
    proofing.init_schema(conn.cursor()); conn.commit(); conn.close()
    routes.HOOKS.update(get_db=get_db, log_activity=log_activity,
                        job_label=lambda j: f"{j.get('customer') or ''} {j.get('job_ticket') or j.get('job_number')}".strip(),
                        now_str=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return routes.rebuild_index(match_tickets=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Import saved proofs from the standalone RFID Proofing Utility")
    ap.add_argument("folder", help="the old proofing folder (the one with START_HERE.bat)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-link", action="store_true", help="don't link proofs to Job Manager jobs")
    a = ap.parse_args(argv)
    if app_is_running() and not a.dry_run:
        print(f"  [STOP] Job Manager is running (port {config.PORT}). Close its window first, then run this again.")
        return 1
    if not os.path.isfile(config.DB_PATH) and not a.no_link:
        print(f"  [NOTE] No Job Manager database at {config.DB_PATH}: proofs are imported but not linked.")
        a.no_link = True
    r = import_proofs(a.folder, dry=a.dry_run, link=not a.no_link)
    print(f"""
  Proofs imported      : {r['proofs']}  (already here, skipped: {r['proofs_skipped']})
  Recovery snapshots   : {r['snapshots']}
  PDFs / packages      : {r['outputs']}
  Inlays added         : {r['inlays_added']}  (images copied: {r['images']})
  Template profiles    : {r['profiles_added']} added
  Settings             : {r['settings']}
  Proof history rows   : {r['history_rows']}
  Previews to redo     : {r['previews_dropped']}""")
    if not a.dry_run and not a.no_link:
        print(f"  Linked to jobs       : {r['linked']}  (more than one match: {r['ambiguous']}, no job: {r['no_match']})")
    print("\n  " + ("Dry run: nothing was written." if a.dry_run else "Done. Start Job Manager with run.bat and open Proofing."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
