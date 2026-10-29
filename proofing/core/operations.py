from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, date
import hashlib
import json
from pathlib import Path
import threading
import time
from typing import Any, Dict, List

from .job_store import job_store_epoch, list_job_entries, load_job_with_signature
from .settings import ROOT, load_settings
from .proof_intelligence import preview_integrity
from .release_control import output_integrity
from .validators import validate_job
from .approval import is_job_locked, normalize_approval_status
from .maintenance import startup_maintenance
from .storage import file_signature, path_cache_key

_ANALYSIS_CACHE_LOCK = threading.RLock()
_ANALYSIS_CACHE: "OrderedDict[tuple[Any, ...], Dict[str, Any]]" = OrderedDict()
_ANALYSIS_CACHE_MAX = 512
_QUEUE_CACHE_LOCK = threading.RLock()
_QUEUE_CACHE: Dict[tuple[Any, ...], tuple[float, List[Dict[str, Any]]]] = {}
_QUEUE_CACHE_TTL_SECONDS = 0.75


def _settings_digest(settings: Dict[str, Any]) -> str:
    relevant = {
        key: settings.get(key) for key in (
            "master_company", "company_name", "company_subtitle", "logo_path",
            "proof_disclaimer_enabled", "proof_disclaimer_version", "proof_disclaimer_opacity",
            "preview_integrity_required", "approval_lock_enabled", "barcode_quality_checks",
        )
    }
    relevant["proof_disclaimer_lines"] = [settings.get(f"proof_disclaimer_line_{i}") for i in range(1, 7)]
    raw = json.dumps(relevant, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")
    return hashlib.blake2b(raw, digest_size=8).hexdigest()


def _artifact_signature(raw: str) -> tuple[int, int] | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        path = Path(text)
        if not path.is_absolute():
            from .settings import ROOT
            path = ROOT / path
        return file_signature(path) if path.is_file() else None
    except OSError:
        return None


def _analysis_key(path: Path, job, settings_key: str, job_signature: tuple[int, int]) -> tuple[Any, ...]:
    return (
        path_cache_key(path), job_signature, settings_key,
        _artifact_signature(job.preview_path), _artifact_signature(job.last_generated_output_path),
    )


def analyze_saved_job(path: Path, job, settings: Dict[str, Any], settings_key: str, job_signature: tuple[int, int]) -> Dict[str, Any]:
    key = _analysis_key(path, job, settings_key, job_signature)
    with _ANALYSIS_CACHE_LOCK:
        cached = _ANALYSIS_CACHE.get(key)
        if cached is not None:
            _ANALYSIS_CACHE.move_to_end(key)
            return cached
    results = validate_job(job, require_preview=True)
    blocking = [r for r in results if not r.ok and r.severity != "warning"]
    warnings = [r for r in results if not r.ok and r.severity == "warning"]
    preview = preview_integrity(job)
    output = output_integrity(job, settings)
    value = {"results": results, "blocking": blocking, "warnings": warnings, "preview": preview, "output": output}
    with _ANALYSIS_CACHE_LOCK:
        _ANALYSIS_CACHE[key] = value
        _ANALYSIS_CACHE.move_to_end(key)
        while len(_ANALYSIS_CACHE) > _ANALYSIS_CACHE_MAX:
            _ANALYSIS_CACHE.popitem(last=False)
    return value


def clear_analysis_cache() -> int:
    with _ANALYSIS_CACHE_LOCK:
        count = len(_ANALYSIS_CACHE)
        _ANALYSIS_CACHE.clear()
    with _QUEUE_CACHE_LOCK:
        _QUEUE_CACHE.clear()
    return count


def _build_work_queue(limit: int, settings: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    settings_key = _settings_digest(settings)
    for path, signature in list_job_entries(limit):
        try:
            job, signature = load_job_with_signature(str(path), signature)
            analysis = analyze_saved_job(path, job, settings, settings_key, signature)
            blocking = analysis["blocking"]
            warnings = analysis["warnings"]
            output = analysis["output"]
            if blocking:
                status = "needs_attention"
            elif warnings:
                status = "warning"
            elif str(job.lifecycle_status or "").lower() == "generated" and output.get("status") == "current":
                status = "generated"
            else:
                status = "ready"
            updated = job.updated_at or datetime.fromtimestamp(signature[0] / 1_000_000_000).isoformat(timespec="seconds")
            company = str(job.preparing_company or job.last_generated_master_company or settings.get("master_company", "MCC Label") or "MCC Label").strip()
            if company.casefold().startswith("starport"):
                company = "Starport Technologies"
            else:
                company = "MCC Label"
            rows.append({
                "path": str(path), "filename": path.name,
                "preparing_company": company,
                "customer": job.customer, "job_number": job.job_number, "part_number": job.part_number,
                "proof_revision": job.proof_revision, "label_revision": job.revision, "proof_id": job.proof_id,
                "approval_status": normalize_approval_status(job.approval_status),
                "status": status, "blocking_count": len(blocking), "warning_count": len(warnings),
                "updated_at": updated, "last_generated_at": job.last_generated_at,
                "preview_integrity": analysis["preview"].get("status", "missing"),
                "output_integrity": output.get("status", "missing"),
                "locked": is_job_locked(job, settings),
            })
        except Exception:
            continue
    status_rank = {"needs_attention": 0, "warning": 1, "ready": 2, "generated": 3}
    rows.sort(key=lambda r: (status_rank.get(r["status"], 9), str(r.get("updated_at") or "")), reverse=False)
    return rows


def work_queue(limit: int = 100, settings: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    settings = settings or load_settings()
    cap = max(1, min(int(limit or 100), 500))
    key = (cap, _settings_digest(settings), job_store_epoch(), date.today().isoformat())
    now = time.monotonic()
    with _QUEUE_CACHE_LOCK:
        cached = _QUEUE_CACHE.get(key)
        if cached and now - cached[0] <= _QUEUE_CACHE_TTL_SECONDS:
            return list(cached[1])
    rows = _build_work_queue(cap, settings)
    with _QUEUE_CACHE_LOCK:
        _QUEUE_CACHE.clear()
        _QUEUE_CACHE[key] = (now, rows)
    return list(rows)


def operations_overview(limit: int = 100) -> Dict[str, Any]:
    settings = load_settings()
    queue = work_queue(limit, settings)
    return {
        "stats": {
            "queue_total": len(queue),
            "queue_ready": sum(1 for r in queue if r["status"] in {"ready", "generated"}),
            "queue_attention": sum(1 for r in queue if r["status"] in {"needs_attention", "warning"}),
            "queue_generated": sum(1 for r in queue if r["status"] == "generated"),
        },
        "queue": queue[:12],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def optimize_runtime(validation_cache: Dict[str, Any], media_registry: Dict[str, Any]) -> Dict[str, int]:
    before_validation = len(validation_cache)
    before_media = len(media_registry)
    analysis_cleared = clear_analysis_cache()
    validation_cache.clear()
    if len(media_registry) > 40:
        for key in list(media_registry)[:-40]:
            media_registry.pop(key, None)
    maintenance = startup_maintenance()
    return {
        "validation_cache_cleared": before_validation,
        "analysis_cache_cleared": analysis_cleared,
        "media_entries_before": before_media,
        "media_entries_after": len(media_registry),
        "stale_files_removed": sum(int(v or 0) for v in maintenance.values()),
    }


def approved_queue(limit: int = 250, settings: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    """Lightweight approved-proof listing. Approved jobs are already locked/current."""
    settings = settings or load_settings()
    approved_dir = ROOT / "jobs" / "approved"
    rows: List[Dict[str, Any]] = []
    for path, signature in list_job_entries(max(1, min(int(limit or 250), 500)), approved_dir):
        try:
            job, signature = load_job_with_signature(str(path), signature)
            company = str(job.preparing_company or job.last_generated_master_company or settings.get("master_company", "MCC Label") or "MCC Label").strip()
            company = "Starport Technologies" if company.casefold().startswith("starport") else "MCC Label"
            updated = job.updated_at or datetime.fromtimestamp(signature[0] / 1_000_000_000).isoformat(timespec="seconds")
            rows.append({
                "path": str(path), "filename": path.name, "preparing_company": company,
                "customer": job.customer, "job_number": job.job_number, "part_number": job.part_number,
                "proof_revision": job.proof_revision, "label_revision": job.revision, "proof_id": job.proof_id,
                "approval_status": "Approved", "status": "approved", "updated_at": updated,
                "last_generated_at": job.last_generated_at, "approval_by": job.approval_by,
                "approval_updated_at": job.approval_updated_at,
            })
        except Exception:
            continue
    rows.sort(key=lambda r: str(r.get("approval_updated_at") or r.get("updated_at") or ""), reverse=True)
    return rows


def archived_queue(limit: int = 250, settings: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    """Lightweight archived-proof listing. Archived proofs do not run full validation."""
    settings = settings or load_settings()
    archive_dir = ROOT / "jobs" / "archive"
    rows: List[Dict[str, Any]] = []
    for path, signature in list_job_entries(max(1, min(int(limit or 250), 500)), archive_dir):
        try:
            job, signature = load_job_with_signature(str(path), signature)
            company = str(job.preparing_company or job.last_generated_master_company or settings.get("master_company", "MCC Label") or "MCC Label").strip()
            if company.casefold().startswith("starport"):
                company = "Starport Technologies"
            else:
                company = "MCC Label"
            updated = job.updated_at or datetime.fromtimestamp(signature[0] / 1_000_000_000).isoformat(timespec="seconds")
            rows.append({
                "path": str(path), "filename": path.name, "preparing_company": company,
                "customer": job.customer, "job_number": job.job_number, "part_number": job.part_number,
                "proof_revision": job.proof_revision, "label_revision": job.revision, "proof_id": job.proof_id,
                "approval_status": normalize_approval_status(job.approval_status),
                "status": "archived", "updated_at": updated, "last_generated_at": job.last_generated_at,
            })
        except Exception:
            continue
    rows.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
    return rows
