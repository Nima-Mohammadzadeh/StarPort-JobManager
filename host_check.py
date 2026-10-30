"""
host_check.py — can Job Manager do its job on this PC?
=======================================================
The earlier RFID Encoding Manager had a "physical certification": checks run on the real host PC
against the real Z: drive, BarTender and printers, written to a dated pass/fail report. This is
Job Manager's version. Used by:
  * JM_TOOLS.bat / jm_tools.py check   (automatic checks, then a short hands-on list)
  * the Tools page, "Check this PC"    (the automatic checks, from inside the running app)

Nothing is changed: shares get a tiny probe file that is deleted straight away. Every check has a
time limit, so a share that doesn't answer is reported as not answering instead of hanging.
Something that couldn't be checked is never counted as a pass: SKIP on a critical item makes the
overall result INCOMPLETE.
"""
import concurrent.futures
import json
import os
import platform
import shutil
import sqlite3
import time
import urllib.request
from datetime import datetime

import config as C

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(HERE, "check_reports")
CHECK_TIMEOUT = 20          # seconds per check
PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"

# The hands-on part (jm_tools.py check asks these one by one).
MANUAL_CHECKS = [
    ("Create a job", "Create a test job (a Special job is fine). Its folder appears on the Z: drive under the customer."),
    ("Encoding database", "For a standard job, open Data in the job folder: the encoding database opens in Excel with the right first serial."),
    ("Rewind tablet", "On a rewind tablet, open the rewind page (scan the QR sign) and scan one tag from a roll: it finds the roll."),
    ("Another PC", "Open Job Manager from a second PC by its network address; the Jobs list loads."),
    ("BarTender preview", "In Proofing, start a proof for the test job and press BarTender Preview: the label image appears."),
    ("Restart", "Close the Job Manager window and start run.bat again: the jobs are all there and open pages reconnect by themselves."),
]


def _res(name, status, detail, critical=True):
    return {"name": name, "status": status, "critical": critical, "detail": detail}


def _probe_write(folder):
    """Write and delete a tiny file in folder. Returns None if it worked, else the error text."""
    if not os.path.isdir(folder):
        return f"folder not found: {folder}"
    probe = os.path.join(folder, f".jm_check_{os.getpid()}_{int(time.time() * 1000)}")
    try:
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
        return None
    except OSError as e:
        return str(e)


def _ro(path):
    """Read-only connection. A proper file:/// URI, so Windows paths with spaces and backslashes work."""
    from pathlib import Path
    conn = sqlite3.connect(Path(os.path.abspath(path)).as_uri() + "?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _settings(db_path):
    try:
        conn = _ro(db_path)
        try:
            return {k: v for k, v in conn.execute("SELECT key, value FROM app_settings")}
        finally:
            conn.close()
    except sqlite3.Error:
        return {}


# ── the automatic checks ──────────────────────────────────────────────────────
def check_packages():
    need, nice, missing, absent = ("flask", "openpyxl", "waitress"), ("segno", "PIL", "reportlab"), [], []
    import importlib.util
    for m in need:
        if importlib.util.find_spec(m) is None:
            missing.append(m)
    for m in nice:
        if importlib.util.find_spec(m) is None:
            absent.append(m)
    if missing:
        return _res("Python packages", FAIL, f"Missing {', '.join(missing)}. Run run.bat once (it installs them) "
                                             "or: python -m pip install -r requirements.txt")
    if absent:
        return _res("Python packages", WARN, f"Missing {', '.join(absent)} (QR sign / proofing need them). Run run.bat once.",
                    critical=False)
    return _res("Python packages", PASS, f"Python {platform.python_version()}, all packages present")


def check_config():
    if not os.path.isfile(C.CONFIG_INI):
        return _res("config.ini", WARN, f"Not found ({C.CONFIG_INI}); the defaults are in use", critical=False)
    return _res("config.ini", PASS, C.CONFIG_INI)


def check_database():
    p = C.DB_PATH
    if not os.path.isfile(p):
        return _res("App database", FAIL, f"Not found: {p}")
    notes = []
    if "onedrive" in p.lower():
        return _res("App database", FAIL, f"{p} is inside OneDrive. Syncing a live SQLite database can corrupt it: "
                                          "move it to a local folder (config.ini [paths] database) and restore from a backup if in doubt.")
    conn = _ro(p)
    try:
        qc = conn.execute("PRAGMA quick_check").fetchone()[0]
        jm = conn.execute("PRAGMA journal_mode").fetchone()[0]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] if "jobs" in tables else 0
    finally:
        conn.close()
    if qc != "ok":
        return _res("App database", FAIL, f"SQLite quick_check: {qc}. Restore from the newest good backup (restore_backup.bat).")
    missing = {"jobs", "job_rolls", "serial_allocations", "activity", "app_settings"} - tables
    if missing:
        return _res("App database", FAIL, f"Tables missing: {', '.join(sorted(missing))}")
    if jm.lower() != "wal":
        notes.append(f"journal mode {jm}")
    free = shutil.disk_usage(os.path.dirname(p) or ".").free
    if free < 2 * 1024 ** 3:
        return _res("App database", WARN, f"quick_check ok, {jobs:,} jobs, but only {free / 1024 ** 3:.1f} GB free on its drive",
                    critical=False)
    return _res("App database", PASS, f"{p}: quick_check ok, {jobs:,} jobs" + (f" ({', '.join(notes)})" if notes else ""))


def check_job_folders():
    e = _probe_write(C.Z_DRIVE_BASE)
    if e:
        return _res("Job folders (Z:)", FAIL, f"Can't write in {C.Z_DRIVE_BASE}: {e}")
    if C.Z_IS_DEFAULT:
        return _res("Job folders (Z:)", WARN, f"Writable, but {C.Z_DRIVE_BASE} is this PC only: set z_drive_base in config.ini "
                                              "to the shared folder", critical=False)
    return _res("Job folders (Z:)", PASS, f"Read and write OK: {C.Z_DRIVE_BASE}")


def check_registry():
    e = _probe_write(C.SERIAL_REGISTRY_DIR)
    if e:
        return _res("Serial registry", FAIL, f"Can't write in {C.SERIAL_REGISTRY_DIR}: {e}. No serials can be given out.")
    n = sum(1 for f in os.listdir(C.SERIAL_REGISTRY_DIR) if f.endswith(".txt"))
    return _res("Serial registry", PASS, f"Read and write OK, {n:,} UPC files: {C.SERIAL_REGISTRY_DIR}")


def check_backups():
    s = _settings(C.DB_PATH)
    bdir = (s.get("backup_dir") or "").strip() or os.path.join(HERE, "backups")
    e = _probe_write(bdir)
    if e:
        return _res("Backups", FAIL, f"Can't write in the backup folder {bdir}: {e}")
    zips = sorted(f for f in os.listdir(bdir) if f.startswith("backup_") and f.endswith(".zip"))
    if not zips:
        return _res("Backups", WARN, f"Folder writable ({bdir}) but no backup made yet", critical=False)
    age_h = (time.time() - os.path.getmtime(os.path.join(bdir, zips[-1]))) / 3600
    try:
        every = int(float(s.get("backup_interval_hours") or 6))
    except ValueError:
        every = 6
    detail = f"Newest {zips[-1]}, {age_h:.0f} h old, {len(zips)} on hand in {bdir}"
    if not s.get("backup_dir"):
        return _res("Backups", WARN, detail + ". They are on this PC only: set a backup folder on the share "
                                              "(Settings, Backups); it also turns on the serial journal.", critical=False)
    if age_h > every * 2 + 1:
        return _res("Backups", WARN, detail + f". Overdue (every {every} h)", critical=False)
    return _res("Backups", PASS, detail)


def check_templates():
    s = _settings(C.DB_PATH)
    tdir = (s.get("btw_template_dir") or "").strip() or r"Z:\3 Encoding and Printing Files\Master Templates"
    if not os.path.isdir(tdir):
        return _res("Master Templates", FAIL, f"Not reachable: {tdir}")
    n = 0
    for _root, _dirs, files in os.walk(tdir):
        n += sum(1 for f in files if f.lower().endswith(".btw"))
        if n > 50000:
            break
    if not n:
        return _res("Master Templates", WARN, f"Reachable but no .btw files found: {tdir}", critical=False)
    return _res("Master Templates", PASS, f"{n:,} templates readable: {tdir}")


def check_proofing():
    d = getattr(C, "PROOFING_DATA_DIR", "")
    if not d:
        return _res("Proofing records", SKIP, "Proofing isn't set up in this version", critical=False)
    e = _probe_write(d)
    if e:
        return _res("Proofing records", FAIL, f"Can't write in {d}: {e}", critical=False)
    if getattr(C, "PROOFING_IS_DEFAULT", False):
        return _res("Proofing records", WARN, f"Writable, but {d} is on this PC only: set [proofing] data_dir in config.ini",
                    critical=False)
    return _res("Proofing records", PASS, f"Read and write OK: {d}", critical=False)


def check_bartender():
    exe = ""
    try:
        sp = os.path.join(getattr(C, "PROOFING_DATA_DIR", ""), "config", "settings.json")
        with open(sp, encoding="utf-8") as fh:
            exe = (json.load(fh).get("bartender_executable") or "").strip()
    except (OSError, ValueError):
        pass
    if exe and os.path.isfile(exe):
        return _res("BarTender", PASS, f"Set in Proofing settings: {exe}", critical=False)
    try:
        from proofing.core.bartender import BarTenderEngine
        found = BarTenderEngine.detect_executable()
    except Exception:
        found = None
    if found:
        return _res("BarTender", PASS, f"Found: {found}", critical=False)
    if platform.system() != "Windows":
        return _res("BarTender", SKIP, "Not a Windows PC: BarTender can't be checked here", critical=False)
    return _res("BarTender", WARN, "bartend.exe not found. Proofing previews need it on this PC (or set its path in "
                                   "Proofing > Settings).", critical=False)


def check_integrity():
    import integrity
    conn = _ro(C.DB_PATH)
    try:
        r = integrity.run(conn)
    finally:
        conn.close()
    head = f"{r['jobs_checked']:,} jobs, {r['ranges_checked']:,} serial ranges"
    if r["ok"]:
        return _res("Serial and database check", PASS, f"{head}: everything adds up")
    bad, warn = r["counts"].get("bad", 0), r["counts"].get("warn", 0)
    first = "; ".join(p["detail"] for p in r["problems"][:3])
    return _res("Serial and database check", FAIL if bad else WARN,
                f"{head}: {bad} problem(s), {warn} to look at. {first}", critical=bool(bad))


def check_server(port=None):
    url = f"http://127.0.0.1:{port or C.PORT}/api/stamp"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            d = json.load(r)
        return _res("Job Manager running", PASS, f"Answers on port {port or C.PORT} (page build {d.get('build', '?')})")
    except Exception as e:
        return _res("Job Manager running", FAIL, f"Nothing answers on port {port or C.PORT} ({e}). Start run.bat, then check again.")


AUTOMATIC = [check_packages, check_config, check_database, check_job_folders, check_registry, check_backups,
             check_templates, check_proofing, check_bartender, check_integrity]


def run_automatic(include_server=True, timeout=CHECK_TIMEOUT) -> list:
    """Run every automatic check, each with a time limit. A check that raises is a FAIL; one that
    doesn't finish in time is a FAIL saying so (its thread is left to finish on its own)."""
    checks = list(AUTOMATIC) + ([check_server] if include_server else [])
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(checks), thread_name_prefix="host-check")
    futures = [(f, pool.submit(f)) for f in checks]
    out, deadline = [], time.time() + timeout
    for f, fut in futures:
        name = f.__name__.replace("check_", "").replace("_", " ").capitalize()
        try:
            out.append(fut.result(timeout=max(0.1, deadline - time.time())))
        except concurrent.futures.TimeoutError:
            out.append(_res(name, FAIL, f"Didn't finish within {timeout} s: a share is probably not answering"))
        except Exception as e:
            out.append(_res(name, FAIL, f"The check itself failed: {type(e).__name__}: {e}"))
    pool.shutdown(wait=False, cancel_futures=True)
    return out


def overall(results) -> str:
    if any(r["status"] == FAIL and r["critical"] for r in results):
        return "FAIL"
    if any(r["status"] == SKIP and r["critical"] for r in results):
        return "INCOMPLETE"
    return "PASS"


def report(results, source, started) -> dict:
    return {"title": "Job Manager host PC check", "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": source, "status": overall(results), "duration_seconds": round(time.time() - started, 2),
            "critical_failures": sum(1 for r in results if r["status"] == FAIL and r["critical"]),
            "warnings_or_skips": sum(1 for r in results if r["status"] in (WARN, SKIP)),
            "context": {"host": platform.node(), "python": platform.python_version(), "platform": platform.platform(),
                        "database": C.DB_PATH, "job_folders": C.Z_DRIVE_BASE, "serial_registry": C.SERIAL_REGISTRY_DIR,
                        "port": C.PORT},
            "results": results}


def report_text(rep) -> str:
    lines = ["JOB MANAGER - HOST PC CHECK", "=" * 72, f"Generated: {rep['generated_at']}  ({rep['source']})",
             f"Overall: {rep['status']}", f"Critical failures: {rep['critical_failures']}",
             f"Warnings/skips: {rep['warnings_or_skips']}", ""]
    for r in rep["results"]:
        lines.append(f"[{r['status']}] {r['name']}" + ("" if r["critical"] else " (advisory)"))
        lines.append(f"       {r['detail']}")
    lines += ["", "CONTEXT", "-" * 72] + [f"  {k}: {v}" for k, v in rep["context"].items()]
    return "\n".join(lines) + "\n"


def save(rep) -> str:
    """Write the report as .txt and .json into check_reports/. Returns the .txt path."""
    os.makedirs(REPORT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.join(REPORT_DIR, f"HOST_CHECK_{stamp}")
    with open(base + ".json", "w", encoding="utf-8") as fh:
        json.dump(rep, fh, indent=2)
    with open(base + ".txt", "w", encoding="utf-8") as fh:
        fh.write(report_text(rep))
    return base + ".txt"
