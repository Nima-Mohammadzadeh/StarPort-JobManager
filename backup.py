"""
backup.py — snapshot zips and how they are checked
==================================================
A backup is one zip holding every data source the app owns:

    local.db                  jobs, rolls, notes, activity, settings, copy of every serial range
    serial_registry/<UPC>.txt the primary record of every serial range
    serial_journal.csv        copy of the append-only journal from the backup folder
    template_thumbnails/      preview images
    config.ini                paths and port
    manifest.json             what was in the zip and the highest serial per UPC

Serial numbers only ever move forward, so the one thing a backup must never get wrong
is the serial history. write_snapshot() copies everything while serial allocation is
paused (a fraction of a second), records the highest serial per UPC it saw in the live
records, and verify_backup() opens the finished zip and refuses it unless the registry
inside covers every one of those serials and every range in the zip's own database.

Job folders and Master Templates live on the share, which has its own backup; the
manifest only lists how many job folders there were.
"""
import os
import csv
import io
import json
import shutil
import socket
import sqlite3
import zipfile
import tempfile
from datetime import datetime

import serial_registry as R

FORMAT = 2


# ── reading serial records ────────────────────────────────────────────────────
def parse_registry_text(text):
    """(ranges, bad_lines) from the text of one registry file.
    ranges: list of (job_number, start, end, qty). bad_lines: lines that can't be read."""
    ranges, bad = [], []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        try:
            job, start, end, qty = parts[1], int(parts[2]), int(parts[3]), int(parts[4])
            if end < start:
                raise ValueError
            ranges.append((job, start, end, qty))
        except (IndexError, ValueError):
            bad.append(line)
    return ranges, bad


def registry_highs(registry_dir):
    """{upc: highest end} for every registry file in a folder."""
    out = {}
    try:
        names = os.listdir(registry_dir)
    except OSError:
        return out
    for f in names:
        if not f.endswith(".txt"):
            continue
        try:
            with open(os.path.join(registry_dir, f), "r", encoding="utf-8") as fh:
                ranges, _ = parse_registry_text(fh.read())
        except OSError:
            continue
        if ranges:
            out[f[:-4]] = max(r[2] for r in ranges)
    return out


def mirror_highs(conn):
    """{upc: highest end} of ranges the database says are in the registry (state 1)."""
    try:
        rows = conn.execute('SELECT upc, MAX("end") FROM serial_allocations '
                            'WHERE registry_state=1 GROUP BY upc').fetchall()
    except sqlite3.Error:
        return {}
    return {u: e for u, e in rows if e is not None}


def journal_highs(text):
    """{upc: highest end} from journal rows written to the registry. Rows made while the
    registry was offline are left out: they are not in the registry until synced."""
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        try:
            if (row.get("status") or "registry") != "registry":
                continue
            u, e = row["upc"], int(row["end"])
            out[u] = max(out.get(u, e), e)
        except (KeyError, ValueError, TypeError):
            pass
    return out


def _merge_max(*maps):
    out = {}
    for m in maps:
        for k, v in m.items():
            out[k] = max(out.get(k, v), v)
    return out


# ── writing ───────────────────────────────────────────────────────────────────
def _copy_registry_file(upc, src, z):
    """Copy one registry file under that UPC's locks, so no half-written line is copied."""
    with R._thread_lock_for(upc):
        lock = R._acquire_file_lock(upc)
        try:
            with open(src, "rb") as fh:
                z.writestr(f"serial_registry/{upc}.txt", fh.read())
        finally:
            R._release_file_lock(lock)


def _count_job_folders(base):
    try:
        n = 0
        for cust in os.scandir(base):
            if cust.is_dir():
                for size in os.scandir(cust.path):
                    if size.is_dir():
                        n += sum(1 for j in os.scandir(size.path) if j.is_dir())
        return n
    except OSError:
        return None


def _add_extra_sources(z, extra_sources):
    """Copy other folders the app owns into the zip (proofing records). extra_sources is a
    list of (folder, name in the zip, keep(file name) -> bool). Returns files added."""
    n = 0
    for folder, arc, keep in extra_sources or ():
        if not folder or not os.path.isdir(folder):
            continue
        for dirpath, _dirs, files in os.walk(folder):
            for f in files:
                if f.startswith(".") or not keep(f):
                    continue
                fp = os.path.join(dirpath, f)
                rel = os.path.relpath(fp, folder).replace("\\", "/")
                try:
                    z.write(fp, f"{arc}/{rel}")
                    n += 1
                except OSError:
                    pass                  # a file removed while the backup runs is not an error
    return n


def write_snapshot(part_path, *, db_path, registry_dir, thumbnail_dir, config_ini, journal_path,
                   job_folder_base, app_version, serial_lock, extra_sources=None):
    """Write the zip to part_path. serial_lock is held while the serial records and the
    database are copied, so they describe one moment. Returns the manifest.
    extra_sources: other folders to include (see _add_extra_sources); outside the lock."""
    fd, tmp_db = tempfile.mkstemp(suffix=".db"); os.close(fd)
    try:
        with zipfile.ZipFile(part_path, "w", zipfile.ZIP_DEFLATED) as z:
            with serial_lock:
                live = sqlite3.connect(db_path, timeout=15)
                try:
                    high = _merge_max(registry_highs(registry_dir), mirror_highs(live))
                    snap = sqlite3.connect(tmp_db)
                    live.backup(snap); snap.close()
                finally:
                    live.close()
                reg_files = 0
                if os.path.isdir(registry_dir):
                    for f in sorted(os.listdir(registry_dir)):
                        fp = os.path.join(registry_dir, f)
                        if f.endswith(".txt") and os.path.isfile(fp):
                            _copy_registry_file(f[:-4], fp, z)
                            reg_files += 1
                journal_rows = None
                if journal_path and os.path.isfile(journal_path):
                    with open(journal_path, "r", encoding="utf-8", newline="") as fh:
                        jtext = fh.read()
                    z.writestr("serial_journal.csv", jtext)
                    high = _merge_max(high, journal_highs(jtext))
                    journal_rows = max(0, len(jtext.splitlines()) - 1)
            z.write(tmp_db, "local.db")
            if os.path.isdir(thumbnail_dir):
                for f in os.listdir(thumbnail_dir):
                    fp = os.path.join(thumbnail_dir, f)
                    if os.path.isfile(fp):
                        z.write(fp, f"template_thumbnails/{f}")
            if config_ini and os.path.isfile(config_ini):
                z.write(config_ini, "config.ini")
            extra_files = _add_extra_sources(z, extra_sources)
            c = sqlite3.connect(tmp_db)
            try:
                count = lambda sql: c.execute(sql).fetchone()[0]
                counts = {"jobs": count("SELECT COUNT(*) FROM jobs"),
                          "rolls": count("SELECT COUNT(*) FROM job_rolls"),
                          "serial_ranges": count("SELECT COUNT(*) FROM serial_allocations"),
                          "registry_files": reg_files, "journal_rows": journal_rows,
                          "job_folders": _count_job_folders(job_folder_base), "upcs": len(high),
                          "proofing_files": extra_files}
            finally:
                c.close()
            manifest = {"format": FORMAT, "created_at": datetime.now().isoformat(timespec="seconds"),
                        "app_version": app_version, "host": socket.gethostname(),
                        "paths": {"database": db_path, "serial_registry": registry_dir,
                                  "journal": journal_path or None, "job_folders": job_folder_base},
                        "counts": counts, "serial_high": high}
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
        return manifest
    finally:
        try:
            os.remove(tmp_db)
        except OSError:
            pass


# ── checking ──────────────────────────────────────────────────────────────────
def read_zip_registry(z):
    """({upc: [ranges]}, {upc: [bad lines]}) from the registry files inside a zip."""
    ranges, bad = {}, {}
    for n in z.namelist():
        if n.startswith("serial_registry/") and n.endswith(".txt"):
            upc = n[len("serial_registry/"):-4]
            r, b = parse_registry_text(z.read(n).decode("utf-8", errors="replace"))
            ranges[upc] = r
            if b:
                bad[upc] = b
    return ranges, bad


def verify_backup(path):
    """Open a backup the way a restore would and check that nothing is missing.
    ok is True only if the zip is sound, the database passes SQLite's integrity check,
    every registry line can be read, and the registry inside the zip covers every
    serial the manifest and the zip's own database know about."""
    out = {"file": os.path.basename(path), "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "ok": False, "format": 1, "serial_problems": [], "upcs_checked": 0, "notes": []}
    tmp = conn = None
    problems = out["serial_problems"]
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            out["zip_ok"] = z.testzip() is None
            out["has_config"] = "config.ini" in names
            out["has_journal"] = "serial_journal.csv" in names
            manifest = json.loads(z.read("manifest.json")) if "manifest.json" in names else {}
            out["format"] = int(manifest.get("format", 1))
            reg, bad = read_zip_registry(z)
            out["registry_files"] = len(reg)
            if "local.db" not in names:
                out["error"] = "local.db is missing from the backup"
                return out
            fd, tmp = tempfile.mkstemp(suffix=".db"); os.close(fd)
            with z.open("local.db") as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
            if out["has_journal"]:
                try:
                    list(csv.DictReader(io.StringIO(z.read("serial_journal.csv").decode("utf-8"))))
                except (csv.Error, UnicodeDecodeError) as e:
                    out["notes"].append(f"The journal copy could not be read: {e}")
        conn = sqlite3.connect(tmp)
        out["integrity"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
        out["jobs"] = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        try:
            mirror = conn.execute('SELECT upc, job_number, start, "end" FROM serial_allocations '
                                  'WHERE registry_state=1').fetchall()
            out["serial_ranges"] = conn.execute("SELECT COUNT(*) FROM serial_allocations").fetchone()[0]
        except sqlite3.Error:
            mirror, out["serial_ranges"] = [], None

        reg_high = {u: max(e for _, _, e, _ in r) for u, r in reg.items() if r}
        reg_keys = {u: {(j, s) for j, s, _, _ in r} for u, r in reg.items()}

        for upc, lines in sorted(bad.items()):
            problems.append({"upc": upc, "problem": f"{len(lines)} line(s) in the registry file can't be read"})
        # (b) the registry covers the highest serial seen in the live records at backup time
        for upc, need in sorted(manifest.get("serial_high", {}).items()):
            have = reg_high.get(upc)
            if have is None:
                problems.append({"upc": upc, "problem": f"registry file missing; serials up to {need:,} were in use"})
            elif have < need:
                problems.append({"upc": upc, "problem": f"registry ends at {have:,} but serials up to {need:,} were in use"})
        # (c) every range the zip's database records as written is in the zip's registry
        seen = {p["upc"] for p in problems}
        for upc, job, start, end in mirror:
            if upc in seen:
                continue
            if (job, start) not in reg_keys.get(upc, set()):
                problems.append({"upc": upc, "problem": f"range {start:,}-{end:,} ({job}) is in the database "
                                                         "but not in the registry file"})
                seen.add(upc)
        # (e) counts match the manifest
        mc = manifest.get("counts", {})
        if mc and mc.get("registry_files") not in (None, len(reg)):
            problems.append({"upc": "", "problem": f"manifest lists {mc['registry_files']} registry files, "
                                                   f"zip has {len(reg)}"})
        if mc and mc.get("jobs") not in (None, out["jobs"]):
            out["notes"].append(f"manifest lists {mc['jobs']} jobs, database has {out['jobs']}")
        if out["format"] < 2:
            out["notes"].append("Older backup format: checked against its own database only.")

        out["upcs_checked"] = len(set(reg) | set(manifest.get("serial_high", {})))
        out["ok"] = bool(out["zip_ok"]) and out["integrity"] == "ok" and not problems
        if problems and "error" not in out:
            first = problems[0]
            more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
            out["error"] = f"Serial history incomplete: UPC {first['upc']} {first['problem']}{more}".replace("UPC  ", "")
        elif out["integrity"] != "ok":
            out["error"] = f"Database check failed: {out['integrity']}"
    except Exception as e:
        out["error"] = str(e)
    finally:
        if conn:
            conn.close()
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return out
