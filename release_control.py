from __future__ import annotations

import hashlib
import json
from typing import Any, Dict

from .models import ProofJob


def release_payload(job: ProofJob, settings: Dict[str, Any]) -> Dict[str, Any]:
    """Customer/output-facing proof state used to detect stale generated PDFs."""
    return {
        "proof_id": str(job.proof_id or "").strip(),
        "customer": str(job.customer or "").strip(),
        "job_number": str(job.job_number or "").strip(),
        "part_number": str(job.part_number or "").strip(),
        "quantity": str(job.quantity or "").strip(),
        "revision": str(job.revision or "").strip(),
        "proof_revision": str(job.proof_revision or "").strip(),
        "gtin": str(job.gtin or "").strip(),
        "barcode_type_1": str(job.barcode_type_1 or "").strip(),
        "barcode_data_1": str(job.barcode_data_1 or "").strip(),
        "barcode_type_2": str(job.barcode_type_2 or "").strip(),
        "barcode_data_2": str(job.barcode_data_2 or "").strip(),
        "font_size_callout": str(job.font_size_callout or "").strip(),
        "label_width": round(float(job.label_width or 0), 4),
        "label_height": round(float(job.label_height or 0), 4),
        "encoding_type": str(job.encoding_type or "").strip(),
        "serial": str(job.serial or ""),
        "epc_hex": str(job.epc_hex or "").strip(),
        "inlay": str(job.inlay or "").strip(),
        "chip": str(job.chip or "").strip(),
        "inlay_width_mm": round(float(job.inlay_width_mm or 0), 3),
        "inlay_height_mm": round(float(job.inlay_height_mm or 0), 3),
        "inlay_x_mm": round(float(job.inlay_x_mm or 0), 3),
        "inlay_y_mm": round(float(job.inlay_y_mm or 0), 3),
        "inlay_rotation": int(job.inlay_rotation or 0),
        "notes": str(job.notes or "").strip(),
        "template_path": str(job.template_path or "").strip(),
        "preview_path": str(job.preview_path or "").strip(),
        "preview_fingerprint": str(job.preview_fingerprint or "").strip(),
        "master_company": str(job.preparing_company or settings.get("master_company", "MCC Label") or "MCC Label").strip(),
        "company_name": str(settings.get("company_name", "RFID Production") or "RFID Production").strip(),
        "company_subtitle": str(settings.get("company_subtitle", "") or "").strip(),
        "logo_path": str(settings.get("logo_path", "") or "").strip(),
        "proof_disclaimer_enabled": bool(settings.get("proof_disclaimer_enabled", True)),
        "proof_disclaimer_version": int(settings.get("proof_disclaimer_version", 14) or 14),
        "proof_disclaimer_opacity": int(settings.get("proof_disclaimer_opacity", 20) or 20),
        "proof_disclaimer_lines": [str(settings.get(f"proof_disclaimer_line_{i}", "") or "").strip() for i in range(1, 7)],
    }


def release_fingerprint(job: ProofJob, settings: Dict[str, Any]) -> str:
    raw = json.dumps(release_payload(job, settings), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def output_integrity(job: ProofJob, settings: Dict[str, Any]) -> Dict[str, Any]:
    if not str(job.last_generated_at or "").strip():
        return {"status": "missing", "stale": True, "message": "No customer proof has been generated yet."}
    current = release_fingerprint(job, settings)
    stamped = str(job.last_generated_fingerprint or "").strip()
    if stamped:
        stale = stamped != current
        return {
            "status": "stale" if stale else "current",
            "stale": stale,
            "generated_at": job.last_generated_at,
            "output_path": str(job.last_generated_output_path or ""),
            "output_kind": str(job.last_generated_output_kind or "PDF"),
            "message": "Proof data changed after the last generated output." if stale else "Generated output matches the current proof data.",
        }
    if isinstance(job.last_generated_snapshot, dict) and job.last_generated_snapshot:
        snap = job.last_generated_snapshot
        legacy = {
            "customer": str(job.customer or "").strip(), "job_number": str(job.job_number or "").strip(),
            "part_number": str(job.part_number or "").strip(), "quantity": str(job.quantity or "").strip(),
            "revision": str(job.revision or "").strip(), "proof_revision": str(job.proof_revision or "").strip(),
            "gtin": str(job.gtin or "").strip(), "barcode_type_1": str(job.barcode_type_1 or "").strip(),
            "barcode_data_1": str(job.barcode_data_1 or "").strip(), "barcode_type_2": str(job.barcode_type_2 or "").strip(),
            "barcode_data_2": str(job.barcode_data_2 or "").strip(), "font_size_callout": str(job.font_size_callout or "").strip(),
            "label_size": f"{float(job.label_width):.3f} x {float(job.label_height):.3f} in", "encoding_type": str(job.encoding_type or "").strip(),
            "serial": str(job.serial or ""), "epc_hex": str(job.epc_hex or "").strip(), "notes": str(job.notes or "").strip(),
            "preview_path": str(job.preview_path or "").strip(), "inlay": str(job.inlay or "").strip(),
            "inlay_placement": f"X {float(job.inlay_x_mm):.2f} mm / Y {float(job.inlay_y_mm):.2f} mm / {int(job.inlay_rotation)} deg",
            "master_company": str(job.preparing_company or settings.get("master_company", "MCC Label") or "MCC Label").strip(),
        }
        changed = any(str(snap.get(k, "")) != str(v) for k, v in legacy.items())
        return {"status": "stale" if changed else "legacy", "stale": changed, "generated_at": job.last_generated_at, "message": "Legacy output no longer matches the current proof." if changed else "Legacy output exists; regenerate once to enable current integrity tracking."}
    return {"status": "legacy", "stale": False, "generated_at": job.last_generated_at, "message": "Generated output exists but predates integrity tracking."}

