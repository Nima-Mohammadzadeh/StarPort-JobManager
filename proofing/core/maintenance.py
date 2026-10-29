from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Dict

from .runtime_paths import AUTOSAVE_DIR, BARTENDER_CACHE_DIR, CACHE_DIR, CONSTRUCTION_CACHE_DIR, RUNTIME_LOG_DIR, RUNTIME_ROOT
from .settings import ROOT


def _remove_path(path: Path) -> int:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            return 1
        if path.exists():
            path.unlink(missing_ok=True)
            return 1
    except Exception:
        return 0
    return 0


def prune_old_files(folder: Path, max_age_days: int, *, include_dirs: bool = False) -> int:
    if not folder.exists():
        return 0
    cutoff = time.time() - max(1, int(max_age_days)) * 86400
    removed = 0
    for item in list(folder.iterdir()):
        try:
            if item.name == '.keep':
                continue
            if item.stat().st_mtime >= cutoff:
                continue
            if item.is_dir() and not include_dirs:
                continue
            removed += _remove_path(item)
        except Exception:
            continue
    return removed


def clean_python_caches(root: Path | None = None) -> int:
    base = root or ROOT
    removed = 0
    for cache in list(base.rglob('__pycache__')):
        removed += _remove_path(cache)
    pytest_cache = base / '.pytest_cache'
    removed += _remove_path(pytest_cache)
    for pyc in list(base.rglob('*.pyc')):
        removed += _remove_path(pyc)
    return removed


def startup_maintenance() -> Dict[str, int]:
    # Conservative cleanup only. Never touches jobs, proofs, configuration, snapshots or backups.
    return {
        'runtime_cache': prune_old_files(CACHE_DIR, 14, include_dirs=True),
        'bartender_cache': prune_old_files(BARTENDER_CACHE_DIR, 14, include_dirs=True),
        'construction_cache': prune_old_files(CONSTRUCTION_CACHE_DIR, 14, include_dirs=True),
        'autosave': prune_old_files(AUTOSAVE_DIR, 7, include_dirs=True),
        'runtime_logs': prune_old_files(RUNTIME_LOG_DIR, 30, include_dirs=True),
        'temp': prune_old_files(ROOT / 'temp', 7, include_dirs=True),
    }


def full_safe_cleanup() -> Dict[str, int]:
    result = startup_maintenance()
    result['python_caches'] = clean_python_caches(ROOT)
    return result


def installation_health() -> Dict[str, object]:
    required = [
        ROOT / 'app', ROOT / 'web', ROOT / 'config', ROOT / 'assets' / 'branding',
        ROOT / 'jobs', ROOT / 'proofs', ROOT / 'logs', ROOT / 'support',
        ROOT / 'START_HERE.bat',
    ]
    missing = [str(p) for p in required if not p.exists()]
    return {
        'ok': not missing,
        'missing': missing,
        'runtime_root': str(RUNTIME_ROOT),
        'app_root': str(ROOT),
    }
