from __future__ import annotations

import json
import os
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from PIL import Image

from .construction import compose_label_with_inlay, generate_construction_diagram, resolve_inlay_image
from .models import ProofJob
from .version import APP_NAME, APP_VERSION


def public_job_dict(job: ProofJob) -> Dict[str, Any]:
    """Return the customer-safe job snapshot used inside exported proof packages."""
    data = job.as_dict()
    data.pop("internal_comments", None)
    data.pop("internal_comment_log", None)
    # Job Manager link fields are internal.
    data.pop("jm_job_id", None)
    data.pop("jm_job_number", None)
    data.pop("last_generated_snapshot", None)
    data.pop("last_generated_proof_revision", None)
    data.pop("last_generated_master_company", None)
    # Beta 10 operational controls remain internal to the workstation/job record.
    data.pop("priority", None)
    data.pop("due_date", None)
    data.pop("release_checklist", None)
    data.pop("internal_tags", None)
    # Beta 11 approval / queue / batch metadata remains internal.
    for key in ("approval_status", "approval_updated_at", "approval_by", "approval_note", "approval_history",
                "customer_response_due", "pinned", "batch_source"):
        data.pop(key, None)
    # Beta 14 preview-integrity and operator-handoff metadata is internal only.
    for key in ("preview_generated_at", "preview_template_path", "preview_source", "preview_fingerprint",
                "assigned_operator", "handoff_note"):
        data.pop(key, None)
    # Beta 15 output-integrity, recovery, activity, and production-release metadata is internal only.
    for key in (
        "last_generated_fingerprint", "last_generated_output_path", "last_generated_output_kind",
        "production_released_at", "production_released_by", "production_release_note",
        "production_release_fingerprint", "activity_log",
    ):
        data.pop(key, None)
    # Beta 16 production orchestration, holds, escalations, routing, and handoff packets are internal only.
    for key in (
        "production_hold", "hold_reason", "hold_by", "hold_at", "release_workcenter",
        "production_release_workcenter", "escalation_level", "escalation_note", "escalation_at",
        "escalation_by", "handoff_packet_path", "handoff_packet_generated_at",
        "run_sheet_path", "run_sheet_generated_at",
        "rework_active", "rework_reason", "rework_requested_at", "rework_requested_by",
        "rework_count", "rework_history", "production_bundle_path",
        "production_bundle_generated_at", "production_bundle_fingerprint",
    ):
        data.pop(key, None)
    # RFID construction/inlay engineering is intentionally internal-only in Beta 3.
    for key in (
        "inlay", "chip", "inlay_width_mm", "inlay_height_mm", "inlay_image_path",
        "inlay_preview_opacity", "inlay_x_mm", "inlay_y_mm", "inlay_rotation",
    ):
        data.pop(key, None)
    return data


def _write_job_json(job: ProofJob, path: Path) -> None:
    path.write_text(json.dumps(public_job_dict(job), indent=2), encoding="utf-8")


def export_proof_package(
    job: ProofJob,
    pdf_path: str,
    output_zip: str,
    settings: Dict[str, Any],
    include_engineering_assets: bool = False,
) -> str:
    pdf = Path(pdf_path)
    if not pdf.is_file():
        raise FileNotFoundError(f"Proof PDF not found: {pdf}")

    output = Path(output_zip)
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="rfid_package_") as td:
        work = Path(td)
        job_json = work / "proof_job.rfidproof.json"
        _write_job_json(job, job_json)

        manifest = {
            "application": APP_NAME,
            "version": APP_VERSION,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "customer": job.customer,
            "job_number": job.job_number,
            "part_number": job.part_number,
            "label_revision": job.revision,
            "proof_revision": job.proof_revision,
            "operator_initials": job.operator_initials,
            "proof_id": job.proof_id,
            "gtin": job.gtin,
            "encoding_type": job.encoding_type,
            "barcode_type_1": job.barcode_type_1,
            "barcode_data_1": job.barcode_data_1,
            "barcode_type_2": job.barcode_type_2,
            "barcode_data_2": job.barcode_data_2,
            "font_size_callout": job.font_size_callout,
            "files": [pdf.name, job_json.name],
            "note": "Customer-safe RFID proof package.",
        }

        extra_files = []
        preview = Path(job.preview_path) if job.preview_path else None
        if include_engineering_assets and preview and preview.is_file():
            clean_preview = work / "production_label_preview.png"
            Image.open(preview).convert("RGB").save(clean_preview, quality=95)
            extra_files.append(clean_preview)

            if resolve_inlay_image(job.inlay_image_path):
                overlay = work / "engineering_inlay_overlay_NOT_PRINT_ART.png"
                compose_label_with_inlay(job, str(preview)).convert("RGB").save(overlay, quality=95)
                extra_files.append(overlay)

            construction = work / "rfid_construction_NOT_PRINT_ART.png"
            generate_construction_diagram(job, str(construction), size=(1400, 720))
            extra_files.append(construction)

        manifest["files"].extend([p.name for p in extra_files])
        manifest_path = work / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        temp_output = output.with_name(f".{output.name}.{os.getpid()}.tmp")
        try:
            with zipfile.ZipFile(temp_output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.write(pdf, pdf.name)
                zf.write(job_json, job_json.name)
                zf.write(manifest_path, manifest_path.name)
                for extra in extra_files:
                    zf.write(extra, extra.name)
            os.replace(temp_output, output)
        finally:
            temp_output.unlink(missing_ok=True)
    return str(output)
