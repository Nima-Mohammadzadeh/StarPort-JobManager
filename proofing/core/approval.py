from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from .models import ProofJob
from .settings import load_settings
from .proof_intelligence import preview_integrity
from .release_control import output_integrity

APPROVAL_STATUSES = (
    "Draft",
    "Internal Review",
    "Customer Review",
    "Approved",
    "Changes Requested",
    "Archived",
)
LOCKED_APPROVAL_STATUSES = {"Approved", "Archived"}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_approval_status(value: str) -> str:
    raw = str(value or "Draft").strip()
    for status in APPROVAL_STATUSES:
        if status.casefold() == raw.casefold():
            return status
    return "Draft"


def is_job_locked(job: ProofJob, settings: Optional[Dict[str, Any]] = None) -> bool:
    settings = settings or load_settings()
    return bool(settings.get("approval_lock_enabled", True)) and normalize_approval_status(job.approval_status) in LOCKED_APPROVAL_STATUSES


def record_approval_event(
    job: ProofJob,
    status: str,
    operator: str = "",
    note: str = "",
    settings: Optional[Dict[str, Any]] = None,
) -> ProofJob:
    settings = settings or load_settings()
    target = normalize_approval_status(status)
    previous = normalize_approval_status(job.approval_status)
    op = str(operator or job.operator_initials or "").strip().upper()[:8]
    note = str(note or "").strip()[:500]

    if target in {"Approved", "Archived"} and not str(job.last_generated_at or "").strip():
        raise ValueError("Generate the customer proof before marking it Approved or Archived.")
    if target == "Approved":
        preview_state = preview_integrity(job)
        if preview_state.get("status") in {"stale", "missing", "missing_file"}:
            raise ValueError("Regenerate the current artwork preview before marking this proof Approved.")
        output_state = output_integrity(job, settings)
        if output_state.get("status") in {"missing", "stale"}:
            raise ValueError("Generate a current customer PDF/package before marking this proof Approved.")

    stamp = now_iso()
    if not isinstance(job.approval_history, list):
        job.approval_history = []
    job.approval_history.append({
        "timestamp": stamp,
        "from": previous,
        "to": target,
        "operator_initials": op,
        "note": note,
    })
    job.approval_history = job.approval_history[-100:]
    job.approval_status = target
    job.approval_updated_at = stamp
    job.approval_by = op
    job.approval_note = note
    return job
