"""
import_old.py — bring data over from the old Job Manager (db-app)
=================================================================
Usage (stop Job Manager first):

    python import_old.py "C:\\Users\\kcyencode5\\Desktop\\db-app"              # serials + templates + all jobs
    python import_old.py "C:\\...\\db-app" --jobs open                         # only jobs not archived
    python import_old.py "C:\\...\\db-app" --jobs none                         # serials + templates only
    python import_old.py "C:\\...\\db-app" --dry-run                           # show what would happen

Or drag the old app's folder onto import_old.bat.

What it does
  • Serial registry: copies every <UPC>.txt. If the UPC already exists here, the two
    files are MERGED (every allocation line kept, sorted), so the next serial is always
    past everything either app ever handed out. Nothing is ever rolled back.
  • Templates: adds the old template list and preview images (matched by file path).
  • Jobs: copies jobs (all / open / none) with their rolls; old roll notes become notes.
    Job numbers already present here are skipped. Job numbering continues after the
    highest old number either way.
  • Makes a copy of this app's database before changing anything.
Run it again any time — it skips what is already there.
"""
import os
import sys
import shutil
import sqlite3
import argparse
import configparser
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def old_paths(folder):
    cfg = configparser.ConfigParser()
    cfg.read(os.path.join(folder, "config.ini"), encoding="utf-8")
    g = lambda k: cfg.get("paths", k, fallback="").strip()
    return {
        "db":       g("database") or os.path.join(folder, "local.db"),
        "registry": g("serial_registry") or os.path.join(folder, "serial_registry"),
        "thumbs":   os.path.join(folder, "template_thumbnails"),
    }


def read_registry(path):
    header, rows = [], []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if not line.strip():
                continue
            if line.startswith("#"):
                header.append(line)
                continue
            parts = [p.strip() for p in line.split("|")]
            try:
                rows.append((int(parts[2]), int(parts[3]), line))
            except (IndexError, ValueError):
                rows.append((0, 0, line))
    return header, rows


def merge_registry(src, dst, dry):
    """Returns 'copied' | 'merged' | 'same'."""
    if not os.path.exists(dst):
        if not dry:
            shutil.copy2(src, dst)
        return "copied"
    h_src, r_src = read_registry(src)
    h_dst, r_dst = read_registry(dst)
    lines = {r[2] for r in r_dst}
    extra = [r for r in r_src if r[2] not in lines]
    if not extra:
        return "same"
    if not dry:
        allrows = sorted(r_dst + extra, key=lambda r: (r[0], r[1]))
        with open(dst, "w", encoding="utf-8", newline="\n") as fh:
            for h in (h_dst or h_src):
                fh.write(h + "\n")
            for r in allrows:
                fh.write(r[2] + "\n")
    return "merged"


def main():
    ap = argparse.ArgumentParser(description="Import data from the old Job Manager")
    ap.add_argument("old_folder")
    ap.add_argument("--jobs", choices=["all", "open", "none"], default="all")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    dry = a.dry_run

    import config
    old = old_paths(a.old_folder)
    print("=" * 64)
    print("  Import from old Job Manager" + ("   (DRY RUN — no data is copied)" if dry else ""))
    print("=" * 64)
    print(f"  Old database : {old['db']}")
    print(f"  Old registry : {old['registry']}")
    print(f"  New database : {config.DB_PATH}")
    print(f"  New registry : {config.SERIAL_REGISTRY_DIR}")
    if not os.path.isfile(old["db"]):
        sys.exit(f"\n  [STOP] Old database not found: {old['db']}")
    if os.path.abspath(old["db"]) == os.path.abspath(config.DB_PATH):
        sys.exit("\n  [STOP] Old and new database are the same file. Nothing to import.")

    config.validate_and_prepare()
    import app as A
    A.init_db()

    if not dry:
        bk = f"{config.DB_PATH}.before-import-{datetime.now():%Y%m%d-%H%M%S}"
        src, dst = sqlite3.connect(config.DB_PATH), sqlite3.connect(bk)
        src.backup(dst); dst.close(); src.close()
        print(f"\n  Safety copy of the new database: {bk}")

    # ── Serial registry ──────────────────────────────────────────────────────
    stats = {"copied": 0, "merged": 0, "same": 0}
    if os.path.isdir(old["registry"]):
        os.makedirs(config.SERIAL_REGISTRY_DIR, exist_ok=True)
        for f in sorted(os.listdir(old["registry"])):
            if f.endswith(".txt"):
                stats[merge_registry(os.path.join(old["registry"], f),
                                     os.path.join(config.SERIAL_REGISTRY_DIR, f), dry)] += 1
    print(f"\n  Serial registry: {stats['copied']} UPCs copied, {stats['merged']} merged, "
          f"{stats['same']} already up to date")

    oc = sqlite3.connect(old["db"]); oc.row_factory = sqlite3.Row
    nc = A.get_db()
    old_tables = {r[0] for r in oc.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    ts = A.now_str()

    # ── Templates + thumbnails ───────────────────────────────────────────────
    t_added, t_thumbs = 0, 0
    if "btw_templates" in old_tables:
        known = {r["file_path"]: r["id"] for r in nc.execute("SELECT id, file_path FROM btw_templates")}
        for t in oc.execute("SELECT * FROM btw_templates"):
            new_id = known.get(t["file_path"])
            if new_id is None:
                t_added += 1
                if dry:
                    continue
                new_id = nc.execute(
                    "INSERT INTO btw_templates (name, file_path, description, notes, active, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?)", (t["name"], t["file_path"], t["description"], t["notes"],
                                                t["active"], t["created_at"] or ts, ts)).lastrowid
                known[t["file_path"]] = new_id
            # preview image: old files are named <old id>.<ext>
            if os.path.isdir(old["thumbs"]) and not dry and not A._thumb_for(new_id):
                for f in os.listdir(old["thumbs"]):
                    stem, ext = os.path.splitext(f)
                    if stem == str(t["id"]):
                        os.makedirs(config.THUMBNAIL_DIR, exist_ok=True)
                        dst = os.path.join(config.THUMBNAIL_DIR, f"{new_id}{ext}")
                        shutil.copy2(os.path.join(old["thumbs"], f), dst)
                        nc.execute("UPDATE btw_templates SET thumbnail_path=? WHERE id=?", (dst, new_id))
                        t_thumbs += 1
                        break
    print(f"  Templates: {t_added} added, {t_thumbs} preview images copied")

    # ── Jobs ─────────────────────────────────────────────────────────────────
    j_added = j_skipped = r_added = n_added = 0
    max_old = 0
    for (num,) in oc.execute("SELECT job_number FROM jobs"):
        try:
            max_old = max(max_old, int(str(num).split("-")[1]))
        except (IndexError, ValueError):
            pass
    if a.jobs != "none":
        new_cols = [r[1] for r in nc.execute("PRAGMA table_info(jobs)") if r[1] != "id"]
        roll_cols = [r[1] for r in nc.execute("PRAGMA table_info(job_rolls)") if r[1] not in ("id", "job_id")]
        have = {r[0] for r in nc.execute("SELECT job_number FROM jobs")}
        sql = "SELECT * FROM jobs" + (" WHERE archived_at IS NULL" if a.jobs == "open" else "") + " ORDER BY id"
        for j in oc.execute(sql):
            j = dict(j)
            if j["job_number"] in have:
                j_skipped += 1
                continue
            j_added += 1
            if dry:
                continue
            cols = [c for c in new_cols if c in j]
            new_id = nc.execute(f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                [j[c] for c in cols]).lastrowid
            if "job_rolls" in old_tables:
                for r in oc.execute("SELECT * FROM job_rolls WHERE job_id=?", (j["id"],)):
                    r = dict(r)
                    rc = [c for c in roll_cols if c in r and c != "notes"]
                    nc.execute(f"INSERT OR IGNORE INTO job_rolls (job_id, {', '.join(rc)}) "
                               f"VALUES (?, {', '.join('?' * len(rc))})", [new_id] + [r[c] for c in rc])
                    r_added += 1
                    if (r.get("notes") or "").strip():
                        nc.execute("INSERT INTO job_notes (job_id, roll_number, author, body, important, created_at)"
                                   " VALUES (?,?,?,?,0,?)", (new_id, r["roll_number"], r.get("completed_by"),
                                                            r["notes"].strip(), r.get("completed_at") or ts))
                        n_added += 1
    print(f"  Jobs: {j_added} added, {j_skipped} already here; {r_added} rolls, {n_added} roll notes")

    # ── Settings (only ones not already set here) ────────────────────────────
    s_added = []
    if "app_settings" in old_tables:
        have_s = {r["key"] for r in nc.execute("SELECT key FROM app_settings")}
        for r in oc.execute("SELECT key, value FROM app_settings WHERE key IN "
                            "('btw_template_dir','backup_dir','backup_interval_hours','backup_auto')"):
            if r["key"] not in have_s and (r["value"] or "").strip():
                s_added.append(f"{r['key']} = {r['value']}")
                if not dry:
                    nc.execute("INSERT INTO app_settings (key, value) VALUES (?, ?)", (r["key"], r["value"]))
    print(f"  Settings: {', '.join(s_added) if s_added else 'nothing new'}")

    if not dry:
        cur = nc.execute("SELECT value FROM app_settings WHERE key='job_number_floor'").fetchone()
        floor = max(max_old, int(cur["value"]) if cur and str(cur["value"]).isdigit() else 0)
        nc.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('job_number_floor', ?)", (str(floor),))
        A.log_activity(nc, "import", None, f"Imported from the old app: {j_added} jobs, "
                       f"{stats['copied'] + stats['merged']} serial registries, {t_added} templates", "Import")
        A.repair_data(nc)             # imported jobs are in the old shape: completion dates, job type, roll trackers
        nc.commit()
        print(f"  Next new job number: JOB-{floor + 1:04d} or later")
    nc.close(); oc.close()
    print("\n  Done." + (" Run again without --dry-run to apply." if dry else " Start Job Manager with run.bat."))


if __name__ == "__main__":
    main()
