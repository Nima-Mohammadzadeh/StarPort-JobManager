from __future__ import annotations

import os
import sys
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List

from .storage import file_signature, load_json_cached, save_json_atomic

# Job Manager integration: the proofing DATA folder (config/, jobs/, logs/, proofs/,
# backups/) is set by proofing/__init__.py from config.ini before this module is
# imported. Every other module reads ROOT from here, so this is the only switch.
_DATA_ROOT = os.environ.get("JM_PROOFING_DATA", "").strip()
if _DATA_ROOT:
    ROOT = Path(_DATA_ROOT).expanduser().resolve()
elif getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent.parent
ROOT.mkdir(parents=True, exist_ok=True)

CONFIG_DIR = ROOT / "config"
SETTINGS_PATH = CONFIG_DIR / "settings.json"
INLAYS_PATH = CONFIG_DIR / "inlays.json"
TEMPLATE_PROFILES_PATH = CONFIG_DIR / "template_profiles.json"

_SETTINGS_CACHE_LOCK = threading.RLock()
_SETTINGS_CACHE_SIGNATURE: tuple[int, int] | None = None
_SETTINGS_CACHE_VALUE: Dict[str, Any] | None = None

LEGACY_BETA13_DISCLAIMER = {
    "proof_disclaimer_line_1": "CUSTOMER PROOF - FOR APPROVAL PURPOSES ONLY",
    "proof_disclaimer_line_2": "FINAL ARTWORK, BARCODE DATA, FONT SIZE, AND RFID / EPC CONTENT MUST BE REVIEWED BEFORE PRODUCTION.",
    "proof_disclaimer_line_3": "AG PLACEMENT AS SEEN THROUGH THE FACE OF THE LABEL. TAG ORIENTATION MAY VARY.",
    "proof_disclaimer_line_4": "CUSTOMER WILL NEED TO TEST SAMPLES AS PART OF THE APPROVAL PROCESS.",
}

DEFAULT_SETTINGS: Dict[str, Any] = {
    "preview_dpi": 300,
    "default_inlay": "Avery Dennison AD-237",
    "bartender_executable": "",
    "proof_output_folder": "proofs",
    "company_name": "RFID Production",
    "company_subtitle": "RFID Label Engineering Proof",
    "master_company": "MCC Label",
    "revision_guard": True,
    "barcode_quality_checks": True,
    "approval_lock_enabled": True,
    "preview_integrity_required": True,
    "auto_recovery_snapshots": True,
    "auto_recovery_snapshot_minutes": 5,
    "proof_disclaimer_enabled": True,
    "proof_disclaimer_version": 14,
    "proof_disclaimer_opacity": 20,
    "proof_disclaimer_line_1": "PROOFING & RFID PLACEMENT DISCLAIMER",
    "proof_disclaimer_line_2": "This proof is provided for approval of artwork, content, dimensions, barcode information, and general RFID inlay placement.",
    "proof_disclaimer_line_3": "RFID placement is shown as viewed through the face of the label. Actual tag orientation and minor placement tolerances may vary during production.",
    "proof_disclaimer_line_4": "Barcode, RFID, encoding, readability, and application performance may be affected by final construction, product, surface, environment, and intended use.",
    "proof_disclaimer_line_5": "Customer testing of production-equivalent samples is required to confirm barcode readability, RFID performance, placement, and suitability before approval.",
    "proof_disclaimer_line_6": "Approval confirms acceptance of the displayed proof. Changes requested after approval may require a revised proof and additional approval.",
    "logo_path": "",
    "recent_job_files": [],
    "inlay_source_folder": "",
    "default_operator_initials": "",
    "preserve_generated_versions": True,
    "beta_snapshot_on_generation": True,
    "named_data_sources": {
        "customer": "CUSTOMER",
        "job_number": "JOB_NUMBER",
        "part_number": "PART_NUMBER",
        "gtin": "GTIN",
        "encoding_type": "",
        "barcode_type_1": "",
        "barcode_data_1": "",
        "barcode_type_2": "",
        "barcode_data_2": "",
        "font_size_callout": "",
        "epc": "EPC",
        "serial": "SERIAL",
        "quantity": "QUANTITY",
        "revision": "REVISION",
        "proof_revision": "PROOF_REVISION",
        "inlay": "INLAY",
        "chip": "CHIP",
    },
}
CORE_KEYS = frozenset(DEFAULT_SETTINGS)


def load_json(path: Path, default):
    return load_json_cached(path, default)


def save_json(path: Path, data: Any) -> None:
    save_json_atomic(path, data)


def _merge_core(loaded: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(DEFAULT_SETTINGS)
    for key in CORE_KEYS:
        if key not in loaded:
            continue
        value = loaded[key]
        if key == "named_data_sources" and isinstance(value, dict):
            result[key].update(value)
        else:
            result[key] = value
    return result


def load_settings() -> Dict[str, Any]:
    global _SETTINGS_CACHE_SIGNATURE, _SETTINGS_CACHE_VALUE
    try:
        signature = file_signature(SETTINGS_PATH)
    except OSError:
        signature = None
    with _SETTINGS_CACHE_LOCK:
        if _SETTINGS_CACHE_VALUE is not None and _SETTINGS_CACHE_SIGNATURE == signature:
            return deepcopy(_SETTINGS_CACHE_VALUE)
    loaded = load_json_cached(SETTINGS_PATH, {}, signature=signature)
    loaded_dict = loaded if isinstance(loaded, dict) else {}
    merged = _merge_core(loaded_dict)
    try:
        disclaimer_version = int(loaded_dict.get("proof_disclaimer_version", 0) or 0)
    except Exception:
        disclaimer_version = 0
    if disclaimer_version < 14:
        uses_legacy_default = all(
            str(loaded_dict.get(key, LEGACY_BETA13_DISCLAIMER[key]) or "").strip() == LEGACY_BETA13_DISCLAIMER[key]
            for key in LEGACY_BETA13_DISCLAIMER
        )
        if uses_legacy_default:
            for i in range(1, 7):
                merged[f"proof_disclaimer_line_{i}"] = DEFAULT_SETTINGS[f"proof_disclaimer_line_{i}"]
        merged["proof_disclaimer_version"] = 14
    # Beta 21 standardizes these regardless of legacy settings.
    merged["proof_disclaimer_opacity"] = 20
    merged["preview_dpi"] = max(150, min(int(merged.get("preview_dpi", 300) or 300), 600))
    merged["auto_recovery_snapshot_minutes"] = max(1, min(int(merged.get("auto_recovery_snapshot_minutes", 5) or 5), 60))
    with _SETTINGS_CACHE_LOCK:
        _SETTINGS_CACHE_SIGNATURE = signature
        _SETTINGS_CACHE_VALUE = deepcopy(merged)
    return deepcopy(merged)


def save_settings(data: Dict[str, Any]) -> None:
    global _SETTINGS_CACHE_SIGNATURE, _SETTINGS_CACHE_VALUE
    current = load_settings()
    current.update({k: v for k, v in data.items() if k in CORE_KEYS})
    merged = _merge_core(current)
    merged["proof_disclaimer_opacity"] = 20
    merged["proof_disclaimer_version"] = 14
    merged["preview_dpi"] = max(150, min(int(merged.get("preview_dpi", 300) or 300), 600))
    merged["auto_recovery_snapshot_minutes"] = max(1, min(int(merged.get("auto_recovery_snapshot_minutes", 5) or 5), 60))
    save_json(SETTINGS_PATH, merged)
    try:
        signature = file_signature(SETTINGS_PATH)
    except OSError:
        signature = None
    with _SETTINGS_CACHE_LOCK:
        _SETTINGS_CACHE_SIGNATURE = signature
        _SETTINGS_CACHE_VALUE = deepcopy(merged)


def load_inlays() -> List[Dict[str, Any]]:
    value = load_json(INLAYS_PATH, [])
    return value if isinstance(value, list) else []


def save_inlays(data: List[Dict[str, Any]]) -> None:
    save_json(INLAYS_PATH, data)


def resolve_output_folder(settings: Dict[str, Any]) -> Path:
    raw = str(settings.get("proof_output_folder", "proofs") or "proofs").strip()
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path
