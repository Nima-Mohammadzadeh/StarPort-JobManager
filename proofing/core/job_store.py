from __future__ import annotations

from copy import deepcopy
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
import threading
import time
from typing import Dict, List, Tuple

from .models import ProofJob
from .settings import ROOT
from .storage import atomic_write_text, file_signature, path_cache_key
from .version import APP_VERSION

_CACHE_LOCK = threading.RLock()
_JOB_CACHE: Dict[str, Tuple[tuple[int, int], dict]] = {}
_MAX_CACHE_ITEMS = 512
_PATH_CACHE: Dict[str, tuple[float, List[tuple[tuple[int, int], Path]]]] = {}
_PATH_CACHE_TTL_SECONDS = 0.75
_MUTATION_EPOCH = 0


def _cache_key(path: Path) -> str:
    return path_cache_key(path)


def _trim_cache() -> None:
    if len(_JOB_CACHE) <= _MAX_CACHE_ITEMS:
        return
    # Dicts preserve insertion order. Remove the oldest quarter in one pass.
    for key in list(_JOB_CACHE)[: max(1, _MAX_CACHE_ITEMS // 4)]:
        _JOB_CACHE.pop(key, None)


def _bump_mutation_epoch() -> None:
    global _MUTATION_EPOCH
    _MUTATION_EPOCH += 1


def job_store_epoch() -> int:
    with _CACHE_LOCK:
        return _MUTATION_EPOCH


def invalidate_job_cache(path: str | Path | None = None) -> None:
    with _CACHE_LOCK:
        _bump_mutation_epoch()
        if path is None:
            _JOB_CACHE.clear()
            _PATH_CACHE.clear()
            return
        target = Path(path)
        _JOB_CACHE.pop(_cache_key(target), None)
        _PATH_CACHE.pop(_cache_key(target.parent), None)


def list_job_entries(limit: int = 100, folder: Path | None = None) -> List[tuple[Path, tuple[int, int]]]:
    """Return newest saved jobs with the signature gathered during discovery.

    Carrying the signature forward avoids a second filesystem round trip when the
    caller immediately loads each job, which is especially useful on mapped shares.
    """
    base = folder or (ROOT / "jobs")
    if not base.exists():
        return []
    base_key = _cache_key(base)
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _PATH_CACHE.get(base_key)
        if cached and now - cached[0] <= _PATH_CACHE_TTL_SECONDS:
            rows = cached[1]
        else:
            rows = []
            # A single scandir/stat pass avoids the glob -> is_file -> stat chain,
            # which can otherwise become multiple metadata round trips on mapped
            # Windows/network shares. DirEntry also lets the OS reuse directory
            # metadata gathered during enumeration.
            try:
                with os.scandir(base) as entries:
                    for entry in entries:
                        try:
                            if not entry.name.endswith(".rfidproof.json") or not entry.is_file():
                                continue
                            stat = entry.stat()
                            rows.append(((stat.st_mtime_ns, stat.st_size), Path(entry.path)))
                        except OSError:
                            continue
            except OSError:
                rows = []
            rows.sort(key=lambda item: item[0][0], reverse=True)
            _PATH_CACHE[base_key] = (now, rows)
    cap = max(1, int(limit or 100))
    return [(path, signature) for signature, path in rows[:cap]]


def list_job_paths(limit: int = 100, folder: Path | None = None) -> List[Path]:
    return [path for path, _signature in list_job_entries(limit, folder)]



def _unique_destination(folder: Path, filename: str, suffix_label: str = "") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / filename
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    label = f"_{suffix_label}" if suffix_label else ""
    counter = 1
    candidate = folder / f"{stem}{label}_{stamp}{suffix}"
    while candidate.exists():
        candidate = folder / f"{stem}{label}_{stamp}_{counter}{suffix}"
        counter += 1
    return candidate


def approved_jobs_root() -> Path:
    root = ROOT / "jobs" / "approved"
    root.mkdir(parents=True, exist_ok=True)
    return root


def route_job_by_approval(path: str | Path, approval_status: str) -> str:
    """Move a saved proof between Active and Approved storage based on status."""
    source = Path(path)
    jobs_root = ROOT / "jobs"
    approved_root = approved_jobs_root()
    if not source.is_file() or source.parent not in {jobs_root, approved_root}:
        raise ValueError("Only active or approved saved proofs can be routed.")
    target_root = approved_root if str(approval_status or "").strip().casefold() == "approved" else jobs_root
    if source.parent == target_root:
        return str(source)
    target = _unique_destination(target_root, source.name, "approved" if target_root == approved_root else "reopened")
    shutil.move(str(source), str(target))
    invalidate_job_cache(source)
    invalidate_job_cache(target)
    return str(target)


def organize_approved_jobs(limit: int = 1000) -> int:
    """Migrate older saved jobs into the correct Active/Approved folder."""
    moved = 0
    jobs_root = ROOT / "jobs"
    approved_root = approved_jobs_root()
    for folder, want_approved in ((jobs_root, True), (approved_root, False)):
        for path, signature in list_job_entries(limit, folder):
            try:
                job, _ = load_job_with_signature(str(path), signature)
                is_approved = str(job.approval_status or "").strip().casefold() == "approved"
                if (folder == jobs_root and is_approved) or (folder == approved_root and not is_approved):
                    route_job_by_approval(path, job.approval_status)
                    moved += 1
            except Exception:
                continue
    return moved


def archive_job(path: str | Path) -> str:
    source = Path(path)
    jobs_root = ROOT / "jobs"
    approved_root = approved_jobs_root()
    archive_root = jobs_root / "archive"
    if not source.is_file() or source.parent not in {jobs_root, approved_root}:
        raise ValueError("Only active or approved saved proofs can be archived.")
    target = _unique_destination(archive_root, source.name)
    shutil.move(str(source), str(target))
    invalidate_job_cache(source)
    invalidate_job_cache(target)
    return str(target)


def restore_archived_job(path: str | Path) -> str:
    source = Path(path)
    jobs_root = ROOT / "jobs"
    approved_root = approved_jobs_root()
    archive_root = jobs_root / "archive"
    if not source.is_file() or source.parent != archive_root:
        raise ValueError("Only archived saved proofs can be restored.")
    try:
        job = load_job(str(source))
        target_root = approved_root if str(job.approval_status or "").strip().casefold() == "approved" else jobs_root
    except Exception:
        target_root = jobs_root
    target = _unique_destination(target_root, source.name, "restored")
    shutil.move(str(source), str(target))
    invalidate_job_cache(source)
    invalidate_job_cache(target)
    return str(target)


def delete_saved_job(path: str | Path) -> None:
    source = Path(path)
    jobs_root = ROOT / "jobs"
    archive_root = jobs_root / "archive"
    approved_root = approved_jobs_root()
    if not source.is_file() or source.parent not in {jobs_root, archive_root, approved_root}:
        raise ValueError("Only active, approved, or archived proof records can be deleted.")
    source.unlink()
    invalidate_job_cache(source)


def save_job(job: ProofJob, path: str) -> str:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().isoformat(timespec="seconds")
    if not job.created_at:
        job.created_at = now
    job.updated_at = now
    job_data = job.as_dict()
    payload = {
        "schema_version": 11,
        "app_version": APP_VERSION,
        "job": job_data,
    }
    atomic_write_text(output, json.dumps(payload, indent=2, ensure_ascii=False))
    with _CACHE_LOCK:
        _bump_mutation_epoch()
        _PATH_CACHE.pop(_cache_key(output.parent), None)
    try:
        signature = file_signature(output)
    except OSError:
        invalidate_job_cache(output)
    else:
        with _CACHE_LOCK:
            _JOB_CACHE[_cache_key(output)] = (signature, deepcopy(job_data))
            _trim_cache()
    return str(output)


def load_job_with_signature(path: str, signature: tuple[int, int] | None = None) -> tuple[ProofJob, tuple[int, int]]:
    """Load a job and return/reuse its file signature."""
    source = Path(path)
    signature = signature or file_signature(source)
    key = _cache_key(source)
    with _CACHE_LOCK:
        cached = _JOB_CACHE.get(key)
        if cached and cached[0] == signature:
            return ProofJob.from_dict(deepcopy(cached[1])), signature

    payload = json.loads(source.read_text(encoding="utf-8"))
    data = payload.get("job", payload)
    if not isinstance(data, dict):
        raise ValueError("The selected file does not contain a valid RFID proof job.")
    isolated = deepcopy(data)
    with _CACHE_LOCK:
        _JOB_CACHE[key] = (signature, isolated)
        _trim_cache()
    # `data` was just decoded from disk and is not shared with the cache, so the
    # first load can build the dataclass directly without a second deep copy.
    return ProofJob.from_dict(data), signature


def load_job(path: str) -> ProofJob:
    return load_job_with_signature(path)[0]
