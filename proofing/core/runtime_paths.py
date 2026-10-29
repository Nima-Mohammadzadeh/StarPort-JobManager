from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _base_runtime_dir() -> Path:
    """Return a fast local runtime folder, never the synced project folder."""
    if os.name == "nt":
        raw = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        base = Path(raw) / "RFID_Proofing_Utility"
    else:
        base = Path(tempfile.gettempdir()) / "RFID_Proofing_Utility"
    base.mkdir(parents=True, exist_ok=True)
    return base


RUNTIME_ROOT = _base_runtime_dir()
CACHE_DIR = RUNTIME_ROOT / "cache"
BARTENDER_CACHE_DIR = CACHE_DIR / "bartender"
CONSTRUCTION_CACHE_DIR = CACHE_DIR / "construction"
AUTOSAVE_DIR = RUNTIME_ROOT / "autosave"
RUNTIME_LOG_DIR = RUNTIME_ROOT / "logs"

for _path in (CACHE_DIR, BARTENDER_CACHE_DIR, CONSTRUCTION_CACHE_DIR, AUTOSAVE_DIR, RUNTIME_LOG_DIR):
    _path.mkdir(parents=True, exist_ok=True)
