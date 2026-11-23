"""T1 / T1b: every backup is a self-contained snapshot that proves its serial history is complete."""
import os
import json
import shutil
import zipfile
import threading

import pytest


def _backup(jm):
    r = jm.app.make_backup()
    return r, os.path.join(jm.backups, r["file"])


def _rewrite_zip(src, dst, change):
    """Copy a zip, letting change(name, data) return new bytes or None to drop the entry."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for n in zin.namelist():
            out = change(n, zin.read(n))
            if out is not None:
                zout.writestr(n, out)


def _drop_last_line(data):
    lines = data.decode().rstrip("\n").split("\n")
    return ("\n".join(lines[:-1]) + "\n").encode()


# ── contents ──────────────────────────────────────────────────────────────────
def test_zip_contains_every_main_data_source(jm):
    jm.job(1)
    r, path = _backup(jm)
    assert r["success"], r
    names = set(zipfile.ZipFile(path).namelist())
    assert {"local.db", "config.ini", "manifest.json", "serial_journal.csv"} <= names
    assert f"serial_registry/{jm.upc(1)}.txt" in names


def test_manifest_records_highest_serial_per_upc(jm):
    a = jm.job(1, qty=100)
    jm.ranges(count=5)
    r, path = _backup(jm)
    m = json.loads(zipfile.ZipFile(path).read("manifest.json"))
    assert m["format"] == 2
    expected_end = a["job"]["start_serial"] + 102 - 1
    assert m["serial_high"][jm.upc(1)] == expected_end
    assert len(m["serial_high"]) == 6
    assert m["counts"]["jobs"] == 1


def test_verification_reports_serials_checked(jm):
    jm.ranges(count=20)
    r, _ = _backup(jm)
    assert r["verify"]["ok"]
    assert r["verify"]["upcs_checked"] == 20
    assert r["verify"]["serial_problems"] == []


# ── catching bad zips ─────────────────────────────────────────────────────────
def test_truncated_registry_file_is_caught_and_named(jm, tmp_path):
    jm.ranges(count=10)
    _, path = _backup(jm)
    bad_upc = jm.upc(1003)
    bad = str(tmp_path / "bad.zip")
    _rewrite_zip(path, bad, lambda n, d: _drop_last_line(d) if n == f"serial_registry/{bad_upc}.txt" else d)
    v = jm.app.backup.verify_backup(bad)
    assert not v["ok"]
    assert bad_upc in {p["upc"] for p in v["serial_problems"]}


def test_missing_registry_file_is_caught(jm, tmp_path):
    jm.ranges(count=10)
    _, path = _backup(jm)
    gone = jm.upc(1007)
    bad = str(tmp_path / "bad.zip")
    _rewrite_zip(path, bad, lambda n, d: None if n == f"serial_registry/{gone}.txt" else d)
    v = jm.app.backup.verify_backup(bad)
    assert not v["ok"]
    assert gone in {p["upc"] for p in v["serial_problems"]}


def test_database_ahead_of_registry_is_caught_even_if_manifest_agrees(jm, tmp_path):
    """A stale registry copy with a manifest that was (wrongly) written to match it."""
    jm.ranges(count=3)
    _, path = _backup(jm)
    upc = jm.upc(1001)

    def change(n, d):
        if n == f"serial_registry/{upc}.txt":
            return _drop_last_line(d)
        if n == "manifest.json":
            m = json.loads(d); m["serial_high"].pop(upc); return json.dumps(m).encode()
        return d
    bad = str(tmp_path / "bad.zip")
    _rewrite_zip(path, bad, change)
    v = jm.app.backup.verify_backup(bad)
    assert not v["ok"]
    assert upc in {p["upc"] for p in v["serial_problems"]}


def test_unreadable_registry_line_is_caught(jm, tmp_path):
    jm.ranges(count=2)
    _, path = _backup(jm)
    upc = jm.upc(1000)
    bad = str(tmp_path / "bad.zip")
    _rewrite_zip(path, bad, lambda n, d: d + b"2026-01-01 | JOB-9 | garbage\n"
                 if n == f"serial_registry/{upc}.txt" else d)
    v = jm.app.backup.verify_backup(bad)
    assert not v["ok"]
    assert upc in {p["upc"] for p in v["serial_problems"]}


def test_old_format_zip_still_verifies_with_a_note(jm, tmp_path):
    jm.ranges(count=2)
    _, path = _backup(jm)
    old = str(tmp_path / "old.zip")
    _rewrite_zip(path, old, lambda n, d: None if n == "manifest.json" else d)
    v = jm.app.backup.verify_backup(old)
    assert v["ok"], v
    assert v["format"] == 1


# ── what happens to a failed backup ───────────────────────────────────────────
def test_failed_backup_is_kept_aside_and_never_trusted(jm, monkeypatch):
    jm.job(1)
    good, _ = _backup(jm)
    assert good["success"]
    real = jm.app.backup.verify_backup
    monkeypatch.setattr(jm.app.backup, "verify_backup",
                        lambda p: {**real(p), "ok": False, "error": "forced failure"})
    jm.app._backup_state["last_ok_at"] = None
    r = jm.app.make_backup()
    assert not r["success"]
    files = os.listdir(jm.backups)
    assert good["file"] in files                                   # older good one untouched
    assert any(f.startswith("failed_backup_") for f in files)       # bad one kept for inspection
    assert jm.app._list_backups(jm.backups) == [good["file"]]       # but never listed as a backup
    assert jm.app._backup_state["last_ok_at"] is None               # and never counts as last good


def test_no_partial_zip_is_left_when_writing_fails(jm, monkeypatch):
    jm.job(1)

    def boom(*a, **k):
        part = a[0]
        with open(part, "wb") as fh:
            fh.write(b"PK half a zip")
        raise OSError("share dropped")
    monkeypatch.setattr(jm.app.backup, "write_snapshot", boom)
    r = jm.app.make_backup()
    assert not r["success"] and "share dropped" in r["error"]
    assert [f for f in os.listdir(jm.backups) if f.endswith((".zip", ".part"))] == []


# ── allocations during a backup ───────────────────────────────────────────────
def test_allocations_during_backups_never_cause_false_failures(jm):
    jm.ranges(count=30, per_upc=1)
    stop = threading.Event()
    errors = []

    def allocate():
        i = 0
        while not stop.is_set():
            try:
                jm.SS.reserve(jm.upc(1000 + i % 30), f"LIVE-{i}", 10)
            except Exception as e:          # pragma: no cover
                errors.append(e)
            i += 1
    workers = [threading.Thread(target=allocate) for _ in range(3)]
    for w in workers:
        w.start()
    try:
        results = [jm.app.make_backup() for _ in range(5)]
    finally:
        stop.set()
        for w in workers:
            w.join()
    assert not errors
    assert all(r["success"] for r in results), [r.get("verify") for r in results if not r["success"]]
