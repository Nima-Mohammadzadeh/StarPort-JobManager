from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .models import ProofJob
from .settings import ROOT, CONFIG_DIR
from .storage import atomic_write_text
from .version import APP_VERSION, RELEASE_NAME

SNAPSHOT_ROOT = ROOT / "jobs" / "history"
BACKUP_ROOT = ROOT / "backups"
for p in (SNAPSHOT_ROOT, BACKUP_ROOT):
    p.mkdir(parents=True, exist_ok=True)

_SNAPSHOT_CACHE_LOCK = threading.RLock()
_SNAPSHOT_CACHE: Dict[str, tuple[float, List[Dict[str, Any]]]] = {}
_SNAPSHOT_CACHE_TTL = 1.5


def _invalidate_snapshot_cache() -> None:
    with _SNAPSHOT_CACHE_LOCK:
        _SNAPSHOT_CACHE.clear()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _proof_revision_token(value: str) -> str:
    raw = str(value or "1").strip().upper() or "1"
    clean = "".join(ch for ch in raw if ch.isalnum() or ch in "-_")
    return clean or "1"


def proof_id_for_revision(proof_id: str, proof_revision: str, date_hint: str = "") -> str:
    raw = str(proof_id or "").strip().upper()
    rev = _proof_revision_token(proof_revision)
    date_part = str(date_hint or datetime.now().strftime("%Y%m%d"))
    random_part = ""
    if raw:
        parts = raw.split("-")
        if len(parts) >= 3 and parts[0] == "PRF" and len(parts[1]) == 8 and parts[1].isdigit():
            date_part = parts[1]
        candidate = parts[-1] if parts else ""
        if len(candidate) == 8 and all(ch in "0123456789ABCDEF" for ch in candidate):
            random_part = candidate
    if not random_part:
        random_part = uuid.uuid4().hex[:8].upper()
    return f"PRF-{date_part}-REV{rev}-{random_part}"


def ensure_job_identity(job: ProofJob) -> ProofJob:
    now = now_iso()
    date_hint = ""
    if job.created_at:
        digits = "".join(ch for ch in str(job.created_at)[:10] if ch.isdigit())
        if len(digits) == 8:
            date_hint = digits
    job.proof_id = proof_id_for_revision(job.proof_id, job.proof_revision, date_hint)
    if not job.created_at:
        job.created_at = now
    job.updated_at = now
    if not job.lifecycle_status:
        job.lifecycle_status = "draft"
    return job


def proof_key(job: ProofJob) -> str:
    bits = [job.customer or "Customer", job.job_number or "Job", job.proof_id or "Proof"]
    return "_".join("".join(c if c.isalnum() or c in "-_" else "_" for c in str(x)).strip("_") or "X" for x in bits)[:180]


def create_snapshot(job: ProofJob, reason: str, source_path: str = "", output_path: str = "") -> str:
    ensure_job_identity(job)
    folder = SNAPSHOT_ROOT / proof_key(job)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = folder / f"{stamp}_{reason.replace(' ', '_')}.snapshot.json"
    payload = {
        "snapshot_version": 1,
        "created_at": now_iso(),
        "reason": reason,
        "source_path": source_path,
        "output_path": output_path,
        "app_version": APP_VERSION,
        "job": job.as_dict(),
    }
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False))
    _invalidate_snapshot_cache()
    return str(path)


def list_snapshots(job: Optional[ProofJob] = None, limit: int = 100) -> List[Dict[str, Any]]:
    cache_key = proof_key(job) if job and (job.proof_id or job.job_number) else "__all__"
    now = time.monotonic()
    with _SNAPSHOT_CACHE_LOCK:
        cached = _SNAPSHOT_CACHE.get(cache_key)
        if cached and now - cached[0] <= _SNAPSHOT_CACHE_TTL:
            return [dict(row) for row in cached[1][: max(1, int(limit or 100))]]

    candidates: Iterable[Path]
    if job and (job.proof_id or job.job_number):
        folder = SNAPSHOT_ROOT / proof_key(job)
        candidates = folder.glob("*.snapshot.json") if folder.exists() else []
    else:
        candidates = SNAPSHOT_ROOT.rglob("*.snapshot.json")
    rows: List[Dict[str, Any]] = []
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            data = payload.get("job", {}) if isinstance(payload, dict) else {}
            rows.append({
                "path": str(path),
                "created_at": payload.get("created_at", ""),
                "reason": payload.get("reason", ""),
                "customer": data.get("customer", ""),
                "job_number": data.get("job_number", ""),
                "part_number": data.get("part_number", ""),
                "proof_revision": data.get("proof_revision", ""),
                "proof_id": data.get("proof_id", ""),
            })
        except Exception:
            continue
    rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    with _SNAPSHOT_CACHE_LOCK:
        _SNAPSHOT_CACHE[cache_key] = (now, [dict(row) for row in rows])
        if len(_SNAPSHOT_CACHE) > 256:
            for old_key in list(_SNAPSHOT_CACHE)[:64]:
                _SNAPSHOT_CACHE.pop(old_key, None)
    return rows[: max(1, int(limit or 100))]


def archive_existing_output(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    archive = path.parent / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = archive / f"{path.stem}_{stamp}{path.suffix}"
    counter = 2
    while target.exists():
        target = archive / f"{path.stem}_{stamp}_{counter}{path.suffix}"
        counter += 1
    shutil.move(str(path), str(target))
    return str(target)


def create_backup_bundle() -> str:
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    path = BACKUP_ROOT / f"RFID_Proofing_Backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for base, arc in ((CONFIG_DIR, "config"), (ROOT / "jobs", "jobs"), (ROOT / "logs", "logs")):
            if not base.exists():
                continue
            for file in base.rglob("*"):
                if not file.is_file():
                    continue
                if file.suffix.lower() in {".png", ".jpg", ".jpeg", ".json", ".csv", ".txt", ".log"} or "inlay_images" in file.parts:
                    zf.write(file, f"{arc}/{file.relative_to(base)}")
        zf.writestr("backup_manifest.json", json.dumps({
            "application": "RFID Proofing Utility",
            "version": APP_VERSION,
            "release": RELEASE_NAME,
            "created_at": now_iso(),
            "note": "Local backup of configuration, jobs, inlay assets, and logs. Customer proof PDFs are not included.",
        }, indent=2))
    return str(path)
