from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from .models import ProofJob

# Fields that can materially change BarTender output or the customer proof artwork.
ARTWORK_FIELDS = (
    "customer", "job_number", "part_number", "quantity", "revision", "proof_revision",
    "gtin", "encoding_type", "barcode_type_1", "barcode_data_1", "barcode_type_2",
    "barcode_data_2", "font_size_callout", "serial", "epc_hex", "inlay", "chip",
)


def _template_signature(path_value: str) -> Dict[str, Any]:
    raw = str(path_value or "").strip()
    result: Dict[str, Any] = {"path": raw}
    if not raw:
        return result
    path = Path(raw)
    try:
        stat = path.stat()
        result.update({"exists": True, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
    except Exception:
        result.update({"exists": False})
    return result


def artwork_fingerprint(job: ProofJob) -> str:
    payload: Dict[str, Any] = {key: getattr(job, key, "") for key in ARTWORK_FIELDS}
    payload["template"] = _template_signature(job.template_path)
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def stamp_preview(job: ProofJob, source: str) -> ProofJob:
    job.preview_generated_at = datetime.now().isoformat(timespec="seconds")
    job.preview_template_path = str(job.template_path or "").strip()
    job.preview_source = str(source or "Preview").strip() or "Preview"
    job.preview_fingerprint = artwork_fingerprint(job)
    return job


def preview_integrity(job: ProofJob) -> Dict[str, Any]:
    if not str(job.preview_path or "").strip():
        return {
            "status": "missing", "tracked": False, "stale": False,
            "message": "No artwork preview has been generated yet.",
        }
    preview = Path(str(job.preview_path))
    if not preview.is_file():
        return {
            "status": "missing_file", "tracked": bool(job.preview_fingerprint), "stale": True,
            "message": "The saved preview file is no longer available.",
        }
    if not str(job.preview_fingerprint or "").strip():
        return {
            "status": "legacy", "tracked": False, "stale": False,
            "message": "Preview exists but predates Beta 14 integrity tracking. Regenerate it when practical.",
        }
    current = artwork_fingerprint(job)
    stale = current != str(job.preview_fingerprint)
    template_changed = bool(job.preview_template_path and str(job.preview_template_path) != str(job.template_path or ""))
    if stale or template_changed:
        return {
            "status": "stale", "tracked": True, "stale": True,
            "message": "Proof data or the BarTender template changed after this preview was generated. Regenerate the preview before release.",
            "generated_at": job.preview_generated_at,
            "source": job.preview_source,
        }
    return {
        "status": "current", "tracked": True, "stale": False,
        "message": "Preview matches the current proof data and template.",
        "generated_at": job.preview_generated_at,
        "source": job.preview_source,
    }

