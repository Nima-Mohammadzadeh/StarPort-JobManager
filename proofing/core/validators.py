from dataclasses import dataclass
from pathlib import Path
import re
from typing import List

from .construction import placement_detail
from .epc import digits_only, encode_sgtin96, normalize_gtin14, validate_gtin
from .models import ProofJob
from .proof_intelligence import preview_integrity


@dataclass
class ValidationItem:
    ok: bool
    name: str
    detail: str = ""
    severity: str = "error"  # error or warning when not ok

    @property
    def status(self) -> str:
        if self.ok:
            return "PASS"
        return "WARN" if self.severity == "warning" else "FAIL"


def validate_job(job: ProofJob, require_preview: bool = True) -> List[ValidationItem]:
    results: List[ValidationItem] = []

    def add(ok: bool, name: str, detail: str = "", severity: str = "error"):
        results.append(ValidationItem(ok, name, detail, severity))

    add(bool(job.customer.strip()), "Customer entered", job.customer if job.customer.strip() else "Customer is required.")
    add(bool(job.job_number.strip()), "Job number entered", job.job_number if job.job_number.strip() else "Job number is required.")
    add(bool(job.proof_revision.strip()), "Proof revision entered", job.proof_revision if job.proof_revision.strip() else "Proof revision is required.")
    initials = str(job.operator_initials or "").strip().upper()
    add(bool(initials), "Operator initials entered", initials if initials else "Operator initials are required for proof traceability.")
    if initials:
        add(bool(re.fullmatch(r"[A-Z0-9.-]{1,8}", initials)), "Operator initials format valid", initials if len(initials) <= 8 else "Use 1-8 characters for operator initials.")
    if job.quantity:
        add(job.quantity.isdigit() and int(job.quantity) > 0, "Quantity valid", job.quantity)
    else:
        add(False, "Quantity entered", "Quantity is recommended for production proof control.", "warning")

    add(bool(job.template_path.strip()), "BarTender template selected", Path(job.template_path).name if job.template_path.strip() else "Select a .BTW template.")
    if job.template_path:
        add(Path(job.template_path).suffix.lower() == ".btw", "Template is a .BTW file", job.template_path)
        add(Path(job.template_path).exists(), "Template file exists", job.template_path)

    encoding_type = str(getattr(job, "encoding_type", "SGTIN-96") or "SGTIN-96").strip()
    if encoding_type in {"SGTIN-96", "SGTIN-198"}:
        valid_gtin, gtin_detail = validate_gtin(job.gtin)
        add(valid_gtin, "GTIN / UPC check digit valid", gtin_detail)
    elif str(job.gtin or "").strip():
        valid_gtin, gtin_detail = validate_gtin(job.gtin)
        add(valid_gtin, "GTIN / UPC check digit valid", gtin_detail)
    else:
        add(True, "GTIN / UPC not required for selected encoding", encoding_type)

    barcode1 = str(job.barcode_type_1 or "").strip()
    barcode2 = str(job.barcode_type_2 or "").strip()
    barcode_data1 = str(job.barcode_data_1 or "").strip()
    barcode_data2 = str(job.barcode_data_2 or "").strip()
    barcode1_used = bool(barcode1) and barcode1.lower() not in {"none", "n/a", "not applicable", "no barcode / n/a"}
    barcode2_used = bool(barcode2) and barcode2.lower() not in {"none", "n/a", "not applicable", "no barcode / n/a"}
    add(bool(barcode1), "Primary barcode type selected", barcode1 if barcode1 else "Select the primary barcode symbology for the proof.")
    if barcode1_used:
        add(bool(barcode_data1), "Barcode 1 data entered", barcode_data1 if barcode_data1 else "Enter the exact data that Barcode 1 is expected to encode.")
    if barcode2_used:
        add(True, "Secondary barcode type selected", barcode2)
        add(bool(barcode_data2), "Barcode 2 data entered", barcode_data2 if barcode_data2 else "Enter the exact data that Barcode 2 is expected to encode.")

    def barcode_quality(symbology: str, data: str, slot: str):
        sym = str(symbology or "").strip().upper()
        raw = str(data or "").strip()
        if not raw:
            return
        numeric_expectations = {
            "UPC-A": {12},
            "UPC-E": {6, 8},
            "EAN-13": {13},
            "EAN-8": {8},
            "ITF-14": {14},
        }
        if sym in numeric_expectations:
            digits = raw.isdigit()
            expected = numeric_expectations[sym]
            expected_text = " or ".join(str(x) for x in sorted(expected))
            add(digits, f"{slot} data characters match {sym}", raw if digits else f"{sym} normally contains numeric digits only.", "warning")
            if digits:
                add(len(raw) in expected, f"{slot} data length matches {sym}", f"{len(raw)} digit(s); expected {expected_text}.", "warning")
        if sym == "CODE 39":
            allowed = bool(re.fullmatch(r"[0-9A-Z .\-$/+%]*", raw.upper()))
            add(allowed, f"{slot} data characters match Code 39", raw if allowed else "Review unsupported Code 39 characters.", "warning")

    if barcode1_used and barcode_data1:
        barcode_quality(barcode1, barcode_data1, "Barcode 1")
    if barcode2_used and barcode_data2:
        barcode_quality(barcode2, barcode_data2, "Barcode 2")

    # Beta 14 proof-data cross-checking. Only compare symbologies where the
    # expected GTIN representation is deterministic; mismatches are warnings
    # because some customers intentionally encode alternate payloads.
    def expected_from_gtin(symbology: str) -> str:
        sym = str(symbology or "").strip().upper()
        raw = digits_only(job.gtin)
        if not raw:
            return ""
        try:
            gtin14 = normalize_gtin14(raw)
        except Exception:
            return ""
        if sym == "UPC-A" and gtin14.startswith("00"):
            return gtin14[-12:]
        if sym == "EAN-13" and gtin14.startswith("0"):
            return gtin14[-13:]
        if sym == "EAN-8" and len(raw) == 8:
            return raw
        if sym == "ITF-14":
            return gtin14
        return ""

    for sym, data, slot in ((barcode1, barcode_data1, "Barcode 1"), (barcode2, barcode_data2, "Barcode 2")):
        expected = expected_from_gtin(sym)
        if expected and data:
            actual = digits_only(data)
            add(actual == expected, f"{slot} payload cross-check", f"Barcode payload {actual or data}; GTIN-derived value {expected}.", "warning")

    font_callout = str(job.font_size_callout or "").strip()
    add(bool(font_callout), "Font size callout entered", font_callout if font_callout else "Enter the approved font size callout, for example 8 pt or 8 pt minimum.")

    try:
        width = float(job.label_width)
        height = float(job.label_height)
        add(width > 0 and height > 0, "Label dimensions valid", f"{width:.3f} x {height:.3f} in")
    except Exception:
        add(False, "Label dimensions valid", "Width and height must be numbers greater than zero.")

    add(bool(job.inlay.strip()), "RFID inlay selected", job.inlay if job.inlay.strip() else "Select an inlay.")
    add(bool(job.chip.strip()), "RFID chip identified", job.chip or "Chip data is missing from the selected inlay.")

    if job.inlay_width_mm > 0 and job.inlay_height_mm > 0:
        placement_ok, placement_msg = placement_detail(job)
        add(placement_ok, "RFID inlay placement fits label", placement_msg)
    else:
        add(False, "RFID inlay construction dimensions", "Add approved inlay width/height in Tools > Inlay Manager to enable placement validation.", "warning")

    add(bool(encoding_type), "Encoding type selected", encoding_type or "Select an RFID / EPC encoding type.")

    if encoding_type == "SGTIN-96":
        try:
            result = encode_sgtin96(job.gtin, int(job.company_prefix_digits), int(job.serial), int(job.filter_value))
            add(True, "SGTIN-96 generated", result.epc_hex)
            add(len(result.epc_hex) == 24, "EPC is exactly 96 bits", f"{len(result.epc_hex) * 4} bits")
            add(result.epc_hex == job.epc_hex, "Displayed EPC matches calculated EPC", result.epc_hex)
            add(True, "SGTIN round-trip GTIN validation", result.gtin14)
        except Exception as exc:
            add(False, "SGTIN-96 generated", str(exc))
            add(False, "EPC is exactly 96 bits", "EPC could not be generated.")
            add(False, "Displayed EPC matches calculated EPC", "Encoding inputs are invalid.")
            add(False, "SGTIN round-trip GTIN validation", "Encoding inputs are invalid.")
    else:
        epc_value = str(job.epc_hex or "").strip()
        add(bool(epc_value), "Encoded EPC / data entered", epc_value if epc_value else f"Enter the encoded EPC / data for {encoding_type}.")
        if epc_value and encoding_type not in {"Custom EPC / Hex", "Other / Customer Specific"}:
            compact = re.sub(r"\s+", "", epc_value)
            is_hex = bool(re.fullmatch(r"[0-9A-Fa-f]+", compact))
            add(is_hex, "Encoded EPC uses hexadecimal characters", compact if is_hex else "Use hexadecimal characters 0-9 and A-F for this EPC encoding type.")
            if is_hex and encoding_type.endswith("-96"):
                add(len(compact) == 24, "96-bit EPC length", f"{len(compact) * 4} bits" if len(compact) != 24 else "96 bits", "warning" if len(compact) != 24 else "error")
            if is_hex and encoding_type == "SGTIN-198":
                add(len(compact) in {49, 50}, "SGTIN-198 EPC length", f"{len(compact)} hex characters", "warning" if len(compact) not in {49, 50} else "error")

    if require_preview:
        preview_ok = bool(job.preview_path) and Path(job.preview_path).exists()
        add(preview_ok, "Proof preview generated", job.preview_path or "Generate or select a preview image.")
        if preview_ok:
            integrity = preview_integrity(job)
            if integrity["status"] == "stale":
                add(False, "Artwork preview is current", integrity["message"])
            elif integrity["status"] == "legacy":
                add(False, "Artwork preview integrity tracked", integrity["message"], "warning")
            elif integrity["status"] == "current":
                add(True, "Artwork preview is current", integrity["message"])

    return results


def all_pass(results: List[ValidationItem]) -> bool:
    return all(item.ok or item.severity == "warning" for item in results)
