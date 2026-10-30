"""
jm_tools.py — maintenance and checks for the host PC (JM_TOOLS.bat runs this as a menu)
========================================================================================
    python jm_tools.py                 menu
    python jm_tools.py check           host PC check: automatic checks, then a short hands-on list
    python jm_tools.py check --auto    automatic checks only
    python jm_tools.py integrity       serial and database check
    python jm_tools.py backup          back up now            (Job Manager must be running)
    python jm_tools.py verify          check the newest backup (Job Manager must be running)
    python jm_tools.py upkeep          database upkeep: SQLite optimize + WAL checkpoint
    python jm_tools.py reports         open the check reports folder
Restoring a backup stays in restore_backup.bat (menu option 7 starts it).
Nothing here changes jobs or serials.
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import config as C          # noqa: E402
import host_check as H      # noqa: E402

BASE = f"http://127.0.0.1:{C.PORT}"


def _post(path, timeout=600):
    req = urllib.request.Request(BASE + path, data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json", "X-Operator": "Tools"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {"error": f"HTTP {e.code}"}


def running():
    try:
        with urllib.request.urlopen(BASE + "/api/stamp", timeout=3):
            return True
    except Exception:
        return False


def _ask(prompt, choices="ynsq"):
    while True:
        a = input(prompt).strip().lower()[:1]
        if a in choices:
            return a
        print(f"   Answer one of: {'/'.join(choices)}")


def cmd_check(auto=False):
    started = time.time()
    print("\nAutomatic checks (each has a 20 s limit)...\n")
    results = H.run_automatic(include_server=True)
    for r in results:
        print(f"  [{r['status']}] {r['name']}\n         {r['detail']}")
    if not auto:
        print("\nHands-on checks. For each: y = done and it worked, n = it didn't, s = skip (counts as not checked).\n")
        for name, how in H.MANUAL_CHECKS:
            print(f"  {name}: {how}")
            a = _ask("     Worked? [y/n/s] ", "yns")
            if a == "y":
                results.append(H._res(name, H.PASS, how))
            elif a == "n":
                note = input("     What happened? ").strip() or "Didn't work"
                results.append(H._res(name, H.FAIL, f"{how} -> {note}"))
            else:
                results.append(H._res(name, H.SKIP, f"{how} (not checked)"))
    rep = H.report(results, "JM_TOOLS check" + (" --auto" if auto else ""), started)
    path = H.save(rep)
    print(f"\nOverall: {rep['status']}   ({rep['critical_failures']} critical failure(s), "
          f"{rep['warnings_or_skips']} warning(s)/skip(s))\nReport: {path}")
    return 0 if rep["status"] == "PASS" else 1


def cmd_integrity():
    if running():
        d = _post("/api/integrity")
        r = d.get("result")
        if not r:
            print(d.get("error")); return 1
    else:
        import integrity
        conn = sqlite3.connect(C.DB_PATH, timeout=15); conn.row_factory = sqlite3.Row
        try:
            r = integrity.run(conn)
        finally:
            conn.close()
    print(f"\nChecked {r['jobs_checked']:,} jobs and {r['ranges_checked']:,} serial ranges.")
    if r["ok"]:
        print("Everything adds up."); return 0
    for p in r["problems"]:
        print(f"  [{'PROBLEM' if p['level'] == 'bad' else 'LOOK'}] {p['detail']}")
    return 1 if r["counts"].get("bad") else 0


def cmd_backup():
    if not running():
        print("Job Manager isn't running. Start run.bat first (backups are made by the app).")
        return 1
    d = _post("/api/backups")
    print(f"Backup saved: {d['file']} ({d.get('size_kb', 0):,} KB), checked OK." if d.get("success")
          else f"Backup FAILED: {d.get('error')}")
    return 0 if d.get("success") else 1


def cmd_verify():
    if not running():
        print("Job Manager isn't running. Start run.bat first.")
        return 1
    d = _post("/api/backups/verify")
    print(f"{d.get('file')}: " + ("sound, database passes its integrity check." if d.get("ok") else f"FAILED: {d.get('error') or d.get('integrity')}"))
    return 0 if d.get("ok") else 1


def upkeep_direct(path):
    """SQLite optimize + checkpoint on the database file itself (Job Manager stopped)."""
    conn = sqlite3.connect(path, timeout=15)
    try:
        before = os.path.getsize(path + "-wal") if os.path.exists(path + "-wal") else 0
        conn.execute("PRAGMA optimize")
        busy, frames, done = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        qc = conn.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        conn.close()
    return {"optimized": True, "checkpoint": {"busy": busy, "frames": frames, "checkpointed": done},
            "wal_before_kb": round(before / 1024), "quick_check": qc}


def cmd_upkeep():
    if running():
        d = _post("/api/tools/db-upkeep")
    else:
        d = upkeep_direct(C.DB_PATH)
    if d.get("error"):
        print("Upkeep failed:", d["error"]); return 1
    cp = d.get("checkpoint", {})
    print(f"Optimized. Write-ahead log: {cp.get('checkpointed', 0)} of {cp.get('frames', 0)} pages written back"
          + (" (busy: some readers are active, that's fine)" if cp.get("busy") else "")
          + (f". quick_check: {d['quick_check']}" if d.get("quick_check") else ""))
    return 0


def cmd_reports():
    os.makedirs(H.REPORT_DIR, exist_ok=True)
    if os.name == "nt":
        os.startfile(H.REPORT_DIR)            # noqa
    else:
        print(H.REPORT_DIR)
    return 0


MENU = [("Host PC check (automatic + hands-on)", lambda: cmd_check()),
        ("Host PC check (automatic only)", lambda: cmd_check(auto=True)),
        ("Serial and database check", cmd_integrity),
        ("Back up now", cmd_backup),
        ("Check the newest backup", cmd_verify),
        ("Database upkeep (optimize + checkpoint)", cmd_upkeep),
        ("Restore a backup (restore_backup.bat)", lambda: subprocess.call(["cmd", "/c", os.path.join(HERE, "restore_backup.bat")])
         if os.name == "nt" else print("Run restore_backup.py")),
        ("Open check reports", cmd_reports)]


def menu():
    while True:
        print("\n" + "=" * 60 + "\n Job Manager tools" + ("   (Job Manager is running)" if running() else "   (Job Manager is NOT running)")
              + "\n" + "=" * 60)
        for i, (label, _) in enumerate(MENU, start=1):
            print(f" {i}. {label}")
        print(" 0. Exit")
        c = input("\nSelect: ").strip()
        if c == "0" or c.lower() == "q":
            return 0
        if c.isdigit() and 1 <= int(c) <= len(MENU):
            try:
                MENU[int(c) - 1][1]()
            except KeyboardInterrupt:
                print("\nStopped.")
            input("\nPress Enter for the menu...")


if __name__ == "__main__":
    args = sys.argv[1:]
    cmds = {"check": lambda: cmd_check(auto="--auto" in args), "integrity": cmd_integrity, "backup": cmd_backup,
            "verify": cmd_verify, "upkeep": cmd_upkeep, "reports": cmd_reports}
    if not args:
        sys.exit(menu())
    if args[0] not in cmds:
        print(__doc__); sys.exit(2)
    sys.exit(cmds[args[0]]() or 0)
