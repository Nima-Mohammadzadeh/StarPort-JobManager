"""The update folder must hold code only, never the live PC's data."""
import os, sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import make_update as MU  # noqa: E402


@pytest.mark.parametrize("name", [
    "local.db", "local.db-wal", "local.db-shm", "demo.db", "jobs.sqlite3", "config.ini", "sub/config.ini",
    "serial_registry/123.txt", "serial_registry/data_sheets/venture_seq.txt", "backups/x.zip",
    "template_thumbnails/a.png", "Master Templates/a.btw", "customers/Venture Research/4 x 2/1.xlsx",
    "LOCAL.DB",
])
def test_data_is_never_shipped(name):
    assert MU.DATA.search(name)


@pytest.mark.parametrize("name", ["app.py", "config.py", "config.ini.example", "customers.py", "db.py",
                                  "sheet_serials.py", "serial_registry.py", "templates/index.html", "run.bat"])
def test_code_is_not_mistaken_for_data(name):
    assert not MU.DATA.search(name)
    assert MU.shipped(name)


@pytest.mark.parametrize("name", ["tests/test_venture.py", "docs/x.md", "tasks/plan.md", "mock_demo/run_mock.py",
                                  ".claude/skills/x/SKILL.md", ".gitignore", "make_update.py"])
def test_dev_files_stay_behind(name):
    assert not MU.shipped(name)
