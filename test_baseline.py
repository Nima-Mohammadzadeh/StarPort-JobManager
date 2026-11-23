"""Baseline: the app as uploaded can create jobs, allocate serials and make a verified backup."""
import os
import zipfile


def test_job_creation_allocates_forward(jm):
    a = jm.job(1, qty=100)
    b = jm.job(2, qty=100, upc=jm.upc(1))
    assert b["job"]["start_serial"] == a["job"]["start_serial"] + 102   # 100 + 2% overage


def test_backup_is_made_and_verified(jm):
    jm.job(1)
    r = jm.app.make_backup()
    assert r["success"], r
    path = os.path.join(jm.backups, r["file"])
    names = zipfile.ZipFile(path).namelist()
    assert "local.db" in names and any(n.startswith("serial_registry/") for n in names)
