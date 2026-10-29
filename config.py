"""
config.py — paths and server settings
=====================================
Reads config.ini next to this file. Blank path = local folder next to app.py
(good for home testing). A path that IS set but can't be reached stops the app
at startup with a clear message — it never silently switches to a local copy,
because a second database or serial registry is how duplicate serials happen.
"""
import os
import shutil
import platform
import configparser
from pathlib import Path

HERE        = os.path.dirname(os.path.abspath(__file__))
CONFIG_INI  = os.path.join(HERE, "config.ini")
CONFIG_EX   = os.path.join(HERE, "config.ini.example")
SYSTEM      = platform.system()

if not os.path.isfile(CONFIG_INI) and os.path.isfile(CONFIG_EX):
    shutil.copy2(CONFIG_EX, CONFIG_INI)

_cfg = configparser.ConfigParser()
_cfg.read(CONFIG_INI, encoding="utf-8")


def _get(section, key, fallback=""):
    return _cfg.get(section, key, fallback=fallback).strip()


def _norm(p):
    if p and SYSTEM != "Windows":
        p = p.replace("\\", "/")
    return p


_db   = _norm(_get("paths", "database"))
_reg  = _norm(_get("paths", "serial_registry"))
_zdr  = _norm(_get("paths", "z_drive_base"))

DB_PATH             = _db  or os.path.join(HERE, "local.db")
SERIAL_REGISTRY_DIR = _reg or os.path.join(HERE, "serial_registry")
Z_DRIVE_BASE        = _zdr or os.path.join(str(Path.home()), "Documents", "JobManager", "Customers")
THUMBNAIL_DIR       = os.path.join(HERE, "template_thumbnails")
Z_IS_DEFAULT        = not _zdr          # no job-folder location set: folders stay on this PC

HOST = _get("server", "host", "0.0.0.0")
PORT = int(_get("server", "port", "5050") or 5050)


def validate_and_prepare():
    """Print the resolved paths and stop if a configured path is unreachable."""
    print("=" * 64)
    print(f"  Job Manager  ({SYSTEM})")
    print("=" * 64)
    print(f"  Config      : {CONFIG_INI}")
    print(f"  Database    : {DB_PATH}")
    print(f"  Serials     : {SERIAL_REGISTRY_DIR}")
    print(f"  Job folders : {Z_DRIVE_BASE}")
    print(f"  Port        : {PORT}  (open http://localhost:{PORT} on this PC)")

    problems = []
    if _db and not os.path.isdir(os.path.dirname(_db)):
        problems.append(f"Database folder not reachable: {os.path.dirname(_db)}")
    if _reg and not os.path.isdir(_reg):
        problems.append(f"Serial registry folder not reachable: {_reg}")
    if problems:
        print("-" * 64)
        for p in problems:
            print(f"  [STOP] {p}")
        print("  Map the drive (or fix config.ini) and start again.")
        print("=" * 64)
        raise SystemExit(1)

    for d in (os.path.dirname(DB_PATH), SERIAL_REGISTRY_DIR, THUMBNAIL_DIR):
        os.makedirs(d, exist_ok=True)
    if Z_IS_DEFAULT:
        print("-" * 64)
        print("  [NOTE] z_drive_base is not set in config.ini, so job folders are created")
        print("         on THIS PC only. Other users will not see them. For a shared")
        print("         location, set z_drive_base to the shared folder and restart.")
        print("-" * 64)
    if not os.path.isdir(Z_DRIVE_BASE):
        if _zdr:
            print(f"  [WARN] Job folder root not reachable right now: {Z_DRIVE_BASE}")
            print("         Job creation will report a folder error until it is.")
        else:
            os.makedirs(Z_DRIVE_BASE, exist_ok=True)
    print("=" * 64 + "\n")
