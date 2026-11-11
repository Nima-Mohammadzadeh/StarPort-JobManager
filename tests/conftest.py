"""
Test fixtures. Every test gets a throwaway copy of the app in a temp folder, with its
own database, serial registry, job folders and backup folder, so nothing real is touched.
"""
import os
import sys
import shutil
import importlib

import pytest

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_MODULES = ("config", "db", "serial_registry", "serial_store", "sheet_serials", "generators", "checklist_generator",
               "lists", "app", "backup", "restore_backup", "import_old")


def _upc(n):
    """A valid 12-digit UPC from a number (correct check digit)."""
    from generators import upc_check_digit
    first11 = f"0{(810000000 + n):010d}"[-11:]
    return first11 + str(upc_check_digit(first11))


class JM:
    """Handle to one isolated app install."""

    def __init__(self, root):
        self.root = root
        self.backups = os.path.join(root, "share_backups")
        self.customers = os.path.join(root, "customers")

    def load(self):
        """(Re)import the app modules from this install."""
        for m in APP_MODULES:
            sys.modules.pop(m, None)
        sys.path.insert(0, self.root)
        try:
            self.config = importlib.import_module("config")
            self.db = importlib.import_module("db")
            self.app = importlib.import_module("app")
            self.SS = importlib.import_module("serial_store")
            self.R = importlib.import_module("serial_registry")
            self.SHEETS = importlib.import_module("sheet_serials")
        finally:
            sys.path.remove(self.root)
        self.config.validate_and_prepare()
        self.app.init_db()
        self.SS.set_journal_dir(self.backups)
        return self

    def restore(self):
        """The restore tool module from this install (shares the loaded app modules)."""
        sys.path.insert(0, self.root)
        try:
            return importlib.import_module("restore_backup")
        finally:
            sys.path.remove(self.root)

    def set_backup_dir(self):
        conn = self.app.get_db()
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('backup_dir', ?)", (self.backups,))
        conn.commit(); conn.close()
        os.makedirs(self.backups, exist_ok=True)

    def upc(self, n):
        return _upc(n)

    def job(self, n, qty=100, upc=None, ticket=None):
        res, code = self.app.create_job({
            "customer": "Acme", "title": f"Item {n}", "job_ticket": ticket or f"T{n}",
            "upc": upc or _upc(n), "quantity": qty, "lpr": 50, "overage_pct": 2})
        assert code == 201, res
        return res

    def ranges(self, count=300, per_upc=3, qty=1000):
        """Many UPCs with several ranges each, straight through the serial store (fast)."""
        for n in range(count):
            for k in range(per_upc):
                self.SS.reserve(_upc(1000 + n), f"FIX-{n}-{k}", qty)


@pytest.fixture
def jm(tmp_path):
    root = str(tmp_path / "app")
    shutil.copytree(SRC, root, ignore=shutil.ignore_patterns(
        "tests", ".git", "__pycache__", ".pytest_cache", "local.db*", "serial_registry",
        "backups", "template_thumbnails", "config.ini"))
    h = JM(root)
    os.makedirs(h.customers)
    with open(os.path.join(root, "config.ini"), "w") as fh:
        fh.write(f"[paths]\ndatabase =\nserial_registry =\nz_drive_base = {h.customers}\n"
                 "[server]\nhost = 127.0.0.1\nport = 5999\n")
    h.load()
    h.set_backup_dir()
    yield h
    for m in APP_MODULES:
        sys.modules.pop(m, None)
