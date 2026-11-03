"""
restore_backup.py — put a backup back (emergencies only)
========================================================
Usage (stop Job Manager first):

    python restore_backup.py "<backup zip>"                 preview only, nothing changes
    python restore_backup.py "<backup zip>" --apply         restore (asks if needed)
    python restore_backup.py "<zip>" --apply --keep-new-work   keep jobs made/changed since the backup
    python restore_backup.py "<zip>" --apply --drop-new-work   take the backup's jobs exactly

Or drag the zip onto restore_backup.bat.

Rules it never breaks
  • Serial numbers never go backwards. The registry is MERGED with the backup's (every line
    kept), and every serial range in the current database is carried into the restored one.
    After a restore, the next serial for every UPC is the same or higher than before.
  • Job numbers never go backwards, so a job number is never used twice.
  • Before changing anything it copies the current database, registry, previews and
    config.ini into a restore_safety_<date> folder next to the app.
  • It refuses while Job Manager is running, and refuses a backup that fails its check
    (unless --allow-unverified).

When it asks you something
  Only when the current database holds work the backup doesn't: jobs created after the
  backup, or jobs whose rolls, notes or details changed since. You choose to keep that
  work (recommended) or drop it. Dropped jobs are listed in the restore log; their serials
  stay used and their folders on the share are not touched.
"""
import os
import sys
import json
import shutil
import socket
import sqlite3
import zipfile
import argparse
import tempfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config                                  # noqa: E402
import serial_registry as R                    # noqa: E402
import serial_store as SS                      # noqa: E402
import backup                                  # noqa: E402
from import_old import merge_registry          # noqa: E402

JOB_TABLES = ("job_rolls", "job_notes")


class RestoreError(Exception):
    pass


# ── checks ────────────────────────────────────────────────────────────────────
def app_is_running():
    try:
        with socket.create_connection(("127.0.0.1", config.PORT), timeout=1):
            return True
    except OSError:
        return False


def _db_readable(path):
    if not os.path.isfile(path):
        return False
    try:
        c = sqlite3.connect(path)
        ok = c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        c.execute("SELECT COUNT(*) FROM jobs")
        c.close()
        return ok
    except sqlite3.Error:
        return False


def _cols(conn, table):
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def _all_upcs(*conns):
    upcs = set(f[:-4] for f in os.listdir(config.SERIAL_REGISTRY_DIR) if f.endswith(".txt")) \
        if os.path.isdir(config.SERIAL_REGISTRY_DIR) else set()
    for c in conns:
        if c is None:
            continue
        try:
            upcs |= {r[0] for r in c.execute("SELECT DISTINCT upc FROM serial_allocations")}
            upcs |= {r[0] for r in c.execute("SELECT DISTINCT upc FROM jobs WHERE upc IS NOT NULL AND upc != ''")}
        except sqlite3.Error:
            pass
    return upcs


def next_starts(upcs):
    """Next free serial per UPC from every record the app reads right now. With a missing
    or damaged database only the registry can be read (and nothing is created)."""
    SS._health["val"] = None                      # don't trust a cached registry check
    if _db_readable(config.DB_PATH):
        return {u: SS.next_start(u) for u in sorted(upcs)}
    out = {}
    for u in sorted(upcs):
        last = R._read_last_end(u)
        out[u] = SS.DEFAULT_START if last is None else last + 1
    return out


# ── comparing the current database with the backup's ─────────────────────────
def _job_snapshot(conn, job_number):
    """Everything about one job that a person could have changed, for comparison."""
    j = conn.execute("SELECT * FROM jobs WHERE job_number=?", (job_number,)).fetchone()
    if not j:
        return None
    j = dict(j)
    jid = j.pop("id")
    rolls = [tuple(dict(r).get(k) for k in ("roll_number", "status", "completed_at", "completed_by",
                                             "started_at", "paused_at"))
             for r in conn.execute("SELECT * FROM job_rolls WHERE job_id=? ORDER BY roll_number", (jid,))]
    notes = [(r["roll_number"], r["body"], r["created_at"])
             for r in conn.execute("SELECT * FROM job_notes WHERE job_id=? ORDER BY id", (jid,))]
    return {"job": j, "rolls": rolls, "notes": notes}


def compare_jobs(cur, old):
    """(new_jobs, changed_jobs): work in the current database the backup doesn't have."""
    cur.row_factory = old.row_factory = sqlite3.Row
    have_old = {r["job_number"] for r in old.execute("SELECT job_number FROM jobs")}
    new, changed = [], []
    for r in cur.execute("SELECT job_number, job_ticket, customer, title, created_at, folder_path "
                         "FROM jobs ORDER BY id"):
        info = dict(r)
        if r["job_number"] not in have_old:
            new.append(info)
        elif _job_snapshot(cur, r["job_number"]) != _job_snapshot(old, r["job_number"]):
            changed.append(info)
    return new, changed


# ── preview ───────────────────────────────────────────────────────────────────
def plan_restore(zip_path):
    if not os.path.isfile(zip_path):
        raise RestoreError(f"Backup not found: {zip_path}")
    check = backup.verify_backup(zip_path)
    with zipfile.ZipFile(zip_path) as z:
        names = set(z.namelist())
        manifest = json.loads(z.read("manifest.json")) if "manifest.json" in names else {}
        zreg, _ = backup.read_zip_registry(z)
        fd, tmp = tempfile.mkstemp(suffix=".db"); os.close(fd)
        with z.open("local.db") as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst)
    old = sqlite3.connect(tmp)
    cur_ok = _db_readable(config.DB_PATH)
    cur = sqlite3.connect(config.DB_PATH) if cur_ok else None
    try:
        new, changed = compare_jobs(cur, old) if cur else ([], [])
        upcs = _all_upcs(cur, old) | set(zreg)
        before = next_starts(upcs)
        zip_high = {u: max(e for _, _, e, _ in r) for u, r in zreg.items() if r}
        live = {u: {(x["job_number"], x["start"]) for x in R.read_records(u)} for u in zreg}
        lines_added = sum(1 for u, r in zreg.items() for j, s, _, _ in r if (j, s) not in live.get(u, set()))
        count = lambda c, t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        return {
            "zip": os.path.abspath(zip_path), "check": check,
            "backup_made": manifest.get("created_at") or datetime.fromtimestamp(os.path.getmtime(zip_path)).isoformat(timespec="seconds"),
            "backup_jobs": count(old, "jobs"),
            "current_readable": cur_ok, "current_exists": os.path.isfile(config.DB_PATH),
            "current_jobs": count(cur, "jobs") if cur else None,
            "new_jobs": new, "changed_jobs": changed, "conflict": bool(new or changed),
            "upcs": len(upcs), "registry_lines_added": lines_added,
            "next_before": before,
            "next_after_expected": {u: max(before.get(u, SS.DEFAULT_START), zip_high.get(u, 0) + 1) for u in upcs},
        }
    finally:
        old.close()
        if cur:
            cur.close()
        os.remove(tmp)


def print_plan(p):
    c = p["check"]
    print("=" * 66)
    print("  Restore preview (nothing has been changed)")
    print("=" * 66)
    print(f"  Backup          : {p['zip']}")
    print(f"  Made            : {p['backup_made']}")
    print(f"  Backup check    : {'PASSED' if c['ok'] else 'FAILED: ' + str(c.get('error'))}"
          + (f" ({c['upcs_checked']} UPCs)" if c.get("upcs_checked") else ""))
    print(f"  Jobs in backup  : {p['backup_jobs']:,}")
    if not p["current_exists"]:
        print("  Current database: none (fresh install)")
    elif not p["current_readable"]:
        print("  Current database: DAMAGED or unreadable; it will be replaced")
    else:
        print(f"  Jobs now        : {p['current_jobs']:,}")
    print(f"  Serial registry : {p['upcs']:,} UPCs; {p['registry_lines_added']:,} line(s) from the backup are missing "
          "here and will be added. Nothing is removed.")
    if p["conflict"]:
        print("-" * 66)
        print(f"  Work since the backup: {len(p['new_jobs'])} new job(s), {len(p['changed_jobs'])} changed job(s)")
        for j in (p["new_jobs"] + p["changed_jobs"])[:15]:
            print(f"    {j['job_number']:<10} ticket {j['job_ticket'] or '-':<10} {j['customer'] or '':<20} {j['created_at']}")
        if len(p["new_jobs"]) + len(p["changed_jobs"]) > 15:
            print("    ...")
        print("  You will be asked whether to KEEP this work (recommended) or DROP it.")
    print("=" * 66)


# ── applying ──────────────────────────────────────────────────────────────────
def _safety_copy():
    base = os.path.join(HERE, f"restore_safety_{datetime.now():%Y%m%d-%H%M%S}")
    dest, n = base, 2
    while os.path.exists(dest):
        dest, n = f"{base}_{n}", n + 1
    os.makedirs(dest)
    if os.path.isfile(config.DB_PATH):
        try:
            src, dst = sqlite3.connect(config.DB_PATH), sqlite3.connect(os.path.join(dest, "local.db"))
            src.backup(dst); dst.close(); src.close()
        except sqlite3.Error:                      # damaged: copy the raw files instead
            for ext in ("", "-wal", "-shm"):
                if os.path.isfile(config.DB_PATH + ext):
                    shutil.copy2(config.DB_PATH + ext, os.path.join(dest, "local.db" + ext))
    if os.path.isdir(config.SERIAL_REGISTRY_DIR):
        shutil.copytree(config.SERIAL_REGISTRY_DIR, os.path.join(dest, "serial_registry"),
                        ignore=shutil.ignore_patterns("*.lock"))
    if os.path.isdir(config.THUMBNAIL_DIR):
        shutil.copytree(config.THUMBNAIL_DIR, os.path.join(dest, "template_thumbnails"))
    if os.path.isfile(config.CONFIG_INI):
        shutil.copy2(config.CONFIG_INI, os.path.join(dest, "config.ini"))
    return dest


def _merge_registry_from_zip(z):
    os.makedirs(config.SERIAL_REGISTRY_DIR, exist_ok=True)
    tmpdir = tempfile.mkdtemp()
    stats = {"copied": 0, "merged": 0, "same": 0}
    try:
        for n in z.namelist():
            if not (n.startswith("serial_registry/") and n.endswith(".txt")):
                continue
            upc = n[len("serial_registry/"):-4]
            src = os.path.join(tmpdir, f"{upc}.txt")
            with open(src, "wb") as fh:
                fh.write(z.read(n))
            with R._thread_lock_for(upc):
                lock = R._acquire_file_lock(upc)
                try:
                    stats[merge_registry(src, os.path.join(config.SERIAL_REGISTRY_DIR, f"{upc}.txt"), False)] += 1
                finally:
                    R._release_file_lock(lock)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return stats


def _copy_job(cur, new, job_number, job_cols):
    """Copy one job with its rolls and notes from cur into new. A job already in new keeps
    its id (so its activity lines still point at it) and gets cur's details."""
    row = cur.execute("SELECT * FROM jobs WHERE job_number=?", (job_number,)).fetchone()
    cols = [c for c in job_cols if c != "id"]
    have = new.execute("SELECT id FROM jobs WHERE job_number=?", (job_number,)).fetchone()
    if have:
        new_id = have[0]
        new.execute(f"UPDATE jobs SET {', '.join(c + '=?' for c in cols)} WHERE id=?",
                    [row[c] for c in cols] + [new_id])
        for t in JOB_TABLES:
            new.execute(f"DELETE FROM {t} WHERE job_id=?", (new_id,))
    else:
        new_id = new.execute(f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                             [row[c] for c in cols]).lastrowid
    for t in JOB_TABLES:
        tcols = [c for c in _cols(new, t) if c in _cols(cur, t) and c not in ("id", "job_id")]
        for r in cur.execute(f"SELECT * FROM {t} WHERE job_id=?", (row["id"],)):
            new.execute(f"INSERT INTO {t} (job_id, {', '.join(tcols)}) VALUES (?, {', '.join('?' * len(tcols))})",
                        [new_id] + [r[c] for c in tcols])
    return new_id


def _build_db(tmp, keep, plan):
    """Turn the backup's database (at tmp) into the one to install."""
    new = sqlite3.connect(tmp); new.row_factory = sqlite3.Row
    SS.init_schema(new)                           # very old backups may lack the serial table
    cur = None
    if plan["current_readable"]:
        cur = sqlite3.connect(config.DB_PATH); cur.row_factory = sqlite3.Row
    try:
        kept = []
        if cur:
            # Serial ranges: every one in the current database survives, whatever is chosen.
            scols = [c for c in _cols(new, "serial_allocations") if c in _cols(cur, "serial_allocations") and c != "id"]
            if scols:
                for r in cur.execute("SELECT * FROM serial_allocations"):
                    new.execute(f"INSERT OR IGNORE INTO serial_allocations ({', '.join(scols)}) "
                                f"VALUES ({', '.join('?' * len(scols))})", [r[c] for c in scols])
            if keep:
                job_cols = [c for c in _cols(new, "jobs") if c in _cols(cur, "jobs")]
                # Changed jobs first: a job cancelled since the backup must be cancelled here before a
                # newer job that reused its ticket comes in, or the one-job-per-ticket rule refuses it.
                for j in plan["changed_jobs"] + plan["new_jobs"]:
                    _copy_job(cur, new, j["job_number"], job_cols)
                    kept.append(j["job_number"])
                # Activity since the backup, pointed at the right job in the restored database.
                by_number = {r["job_number"]: r["id"] for r in new.execute("SELECT id, job_number FROM jobs")}
                id_map = {r["id"]: by_number.get(r["job_number"]) for r in cur.execute("SELECT id, job_number FROM jobs")}
                last = new.execute("SELECT MAX(ts) FROM activity").fetchone()[0] or ""
                for a in cur.execute("SELECT * FROM activity WHERE ts > ? ORDER BY id", (last,)):
                    new.execute("INSERT INTO activity (ts, job_id, kind, actor, message) VALUES (?,?,?,?,?)",
                                (a["ts"], id_map.get(a["job_id"]), a["kind"], a["actor"], a["message"]))
            # Settings: the current ones win (folder paths, schedule), job numbers never go back.
            for r in cur.execute("SELECT key, value FROM app_settings"):
                new.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (r["key"], r["value"]))
        top = 0
        for (num,) in list(new.execute("SELECT job_number FROM jobs")) + \
                (list(cur.execute("SELECT job_number FROM jobs")) if cur else []):
            try:
                top = max(top, int(str(num).split("-")[1]))
            except (IndexError, ValueError):
                pass
        for key in ("job_number_last", "job_number_floor"):
            for c in (new, cur):
                if c is None:
                    continue
                v = c.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
                if v and str(v[0]).isdigit():
                    top = max(top, int(v[0]))
        new.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('job_number_last', ?)", (str(top),))
        new.commit()
        return kept
    finally:
        new.close()
        if cur:
            cur.close()


def _install_db(tmp, readable):
    if readable:
        src, dst = sqlite3.connect(tmp), sqlite3.connect(config.DB_PATH)
        src.backup(dst); dst.close(); src.close()
        return
    for ext in ("", "-wal", "-shm"):
        if os.path.isfile(config.DB_PATH + ext):
            os.remove(config.DB_PATH + ext)
    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
    shutil.copy2(tmp, config.DB_PATH)


def apply_restore(zip_path, keep=None, allow_unverified=False, ask=None):
    """Restore. keep: True / False / None (ask if there is work since the backup).
    ask: function returning True (keep) or False (drop); used when keep is None."""
    if app_is_running():
        raise RestoreError(f"Job Manager is running (port {config.PORT}). Close its window first, then run this again.")
    plan = plan_restore(zip_path)
    if not plan["check"]["ok"] and not allow_unverified:
        raise RestoreError("This backup failed its check: " + str(plan["check"].get("error"))
                           + ". Pick an older backup, or run with --allow-unverified if it is the only one.")
    if plan["conflict"] and keep is None:
        if ask is None:
            raise RestoreError("There is work since this backup. Choose --keep-new-work or --drop-new-work.")
        keep = bool(ask(plan))
    keep = bool(keep) if plan["conflict"] else False
    safety = _safety_copy()

    with zipfile.ZipFile(zip_path) as z:
        reg = _merge_registry_from_zip(z)
        fd, tmp = tempfile.mkstemp(suffix=".db"); os.close(fd)
        with z.open("local.db") as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst)
        try:
            kept = _build_db(tmp, keep, plan)
            _install_db(tmp, plan["current_readable"])
        finally:
            os.remove(tmp)
        thumbs = 0
        for n in z.namelist():
            if n.startswith("template_thumbnails/") and not n.endswith("/"):
                dest = os.path.join(config.THUMBNAIL_DIR, os.path.basename(n))
                if not os.path.exists(dest):
                    os.makedirs(config.THUMBNAIL_DIR, exist_ok=True)
                    with open(dest, "wb") as fh:
                        fh.write(z.read(n))
                    thumbs += 1
        config_note = "unchanged"
        if "config.ini" in z.namelist():
            data = z.read("config.ini")
            if not os.path.isfile(config.CONFIG_INI):
                with open(config.CONFIG_INI, "wb") as fh:
                    fh.write(data)
                config_note = "restored from the backup"
            elif open(config.CONFIG_INI, "rb").read() != data:
                with open(config.CONFIG_INI + ".from-backup", "wb") as fh:
                    fh.write(data)
                config_note = "kept the current one; the backup's is saved as config.ini.from-backup"

    after = next_starts(set(plan["next_before"]) | _all_upcs())
    went_back = {u: (plan["next_before"][u], after.get(u)) for u in plan["next_before"]
                 if after.get(u, 0) < plan["next_before"][u]}
    dropped = [] if keep else plan["new_jobs"] + plan["changed_jobs"]

    conn = sqlite3.connect(config.DB_PATH)
    choice = "no work since the backup" if not plan["conflict"] else ("kept work since the backup" if keep
                                                                       else f"dropped {len(dropped)} job(s) made or changed since the backup")
    conn.execute("INSERT INTO activity (ts, job_id, kind, actor, message) VALUES (?,?,?,?,?)",
                 (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), None, "restore", "Restore",
                  f"Restored from {os.path.basename(zip_path)} ({choice}). Safety copy: {safety}"))
    conn.commit(); conn.close()

    result = {"safety_copy": safety, "registry": reg, "kept_jobs": kept, "dropped_jobs": dropped,
              "thumbnails_added": thumbs, "config": config_note, "serials_went_back": went_back,
              "next_after": after, "plan": plan}
    with open(os.path.join(safety, "restore_log.txt"), "w", encoding="utf-8") as fh:
        fh.write(f"Restored {plan['zip']} at {datetime.now():%Y-%m-%d %H:%M:%S}\n{choice}\n")
        fh.write(f"Registry: {reg}\nconfig.ini: {config_note}\n")
        if dropped:
            fh.write("\nDropped jobs (their serials stay used; folders on the share untouched):\n")
            for j in dropped:
                fh.write(f"  {j['job_number']}  ticket {j['job_ticket']}  {j['customer']}  {j['folder_path']}\n")
        if went_back:
            fh.write(f"\nSERIALS WENT BACKWARDS (should never happen): {went_back}\n")
    if went_back:
        raise RestoreError(f"Serial check failed after restore for {len(went_back)} UPC(s). Do not start the app; "
                           f"copy back the files in {safety} and ask for help.")
    return result


def _ask(plan):
    while True:
        a = input(f"  Keep the {len(plan['new_jobs']) + len(plan['changed_jobs'])} job(s) above (K, recommended), "
                  "drop them (D), or quit (Q)? ").strip().lower()
        if a in ("k", "d"):
            return a == "k"
        if a == "q":
            raise RestoreError("Stopped. Nothing was changed.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Restore Job Manager from a backup zip")
    ap.add_argument("zip")
    ap.add_argument("--apply", action="store_true", help="actually restore (default is preview only)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--keep-new-work", action="store_true")
    g.add_argument("--drop-new-work", action="store_true")
    ap.add_argument("--allow-unverified", action="store_true")
    a = ap.parse_args(argv)
    try:
        if not a.apply:
            print_plan(plan_restore(a.zip))
            print("  Preview only. Run again with --apply to restore.")
            return 0
        print_plan(plan_restore(a.zip))
        keep = True if a.keep_new_work else (False if a.drop_new_work else None)
        r = apply_restore(a.zip, keep=keep, allow_unverified=a.allow_unverified,
                          ask=_ask if sys.stdin.isatty() else None)
    except RestoreError as e:
        print(f"\n  [STOP] {e}")
        return 1
    print("\n  Restored.")
    print(f"  Safety copy of what was there before: {r['safety_copy']}")
    print(f"  Serial registry: {r['registry']['copied']} UPC files added, {r['registry']['merged']} merged, "
          f"{r['registry']['same']} already complete")
    if r["kept_jobs"]:
        print(f"  Kept {len(r['kept_jobs'])} job(s) made or changed since the backup")
    if r["dropped_jobs"]:
        print(f"  Dropped {len(r['dropped_jobs'])} job(s); listed in restore_log.txt in the safety copy")
    print(f"  config.ini: {r['config']}")
    print("  Serial check: no UPC's next serial went backwards.")
    print("  Start Job Manager with run.bat.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
