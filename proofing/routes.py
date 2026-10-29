"""
proofing/routes.py — the RFID Proofing Utility's server, as a Job Manager blueprint
===================================================================================
A port of the proofing app's support/runtime/web_server.py. Every route keeps its JSON
contract ({"ok": true, ...} / {"ok": false, "error": ...}) so the proofing page works the
same; only the address moves from http://127.0.0.1:8765/api/... to /proofing/api/...

What changed because the server is now shared by everyone on the network instead of one
person's PC:
  • Removed: the 5-minute heartbeat shutdown, /api/shutdown, and the Windows file/folder
    pickers that opened on the host PC (nobody at another PC would see them). Templates are
    picked from Job Manager's Master Templates index; preview images are uploaded.
  • Paths sent by the browser are only accepted inside the proofing data folder (saved
    proofs) or the job folders (outputs). Nothing else can be read, written or opened.
  • Saving checks the file hasn't been changed by someone else since it was opened
    ("rev"); a conflict asks before overwriting. New proofs never overwrite an existing file.
  • BarTender previews run one at a time, each in its own folder, and the image is kept in
    the data folder (previews/), so two people previewing at once can't swap images and an
    older proof never loses its preview to the 14-day cache cleanup.
  • Proofs can be linked to a Job Manager job. A linked proof's PDF / package is written to
    that job's Proof folder, its events show in the job's activity, and validation warns
    when its GTIN / EPC / serial don't match what Job Manager encodes for the job.
"""
from __future__ import annotations

import contextlib
import copy
import csv
import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import threading
import time
import urllib.parse
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from flask import Blueprint, abort, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

from .core.approval import is_job_locked, record_approval_event
from .core.audit import record_proof_history
from .core.bartender import BarTenderError, BarTenderPreviewProvider
from .core.beta_services import (archive_existing_output, create_backup_bundle, create_snapshot,
                                 ensure_job_identity, list_snapshots, proof_id_for_revision)
from .core.epc import PARTITIONS, digits_only, encode_sgtin96
from .core.job_store import (archive_job, delete_saved_job, list_job_entries, load_job,
                             load_job_with_signature, organize_approved_jobs, restore_archived_job,
                             route_job_by_approval, save_job)
from .core.maintenance import startup_maintenance
from .core.models import ENCODING_TYPES, ProofJob
from .core.operations import approved_queue, archived_queue, operations_overview, work_queue
from .core.package_export import export_proof_package
from .core.profiles import find_template_profile, load_template_profiles, next_revision
from .core.proof_intelligence import preview_integrity, stamp_preview
from .core.proof_pdf import generate_proof_pdf
from .core.release_control import release_fingerprint
from .core.runtime_paths import BARTENDER_CACHE_DIR, RUNTIME_ROOT
from .core.settings import (ROOT, load_inlays, load_settings, resolve_output_folder, save_inlays,
                            save_settings)
from .core.validators import ValidationItem, all_pass, validate_job
from .core.version import APP_NAME, APP_VERSION, RELEASE_CHANNEL, RELEASE_NAME
from . import jm_link

from generators import CDO_SHEET, GO2_PREFIX, GO2_SHEET, cdo_epc, generate_epc, production_qty

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
JOBS_DIR = ROOT / "jobs"
APPROVED_DIR = JOBS_DIR / "approved"
ARCHIVE_DIR = JOBS_DIR / "archive"
INLAY_IMAGES_DIR = ROOT / "config" / "inlay_images"
PREVIEWS_DIR = ROOT / "previews"
for _p in (JOBS_DIR, APPROVED_DIR, ARCHIVE_DIR, INLAY_IMAGES_DIR, PREVIEWS_DIR):
    _p.mkdir(parents=True, exist_ok=True)
BARTENDER_PREVIEW_LOG = ROOT / "logs" / "bartender_preview.log"

bp = Blueprint("proofing", __name__, url_prefix="/proofing",
               static_folder="static", static_url_path="/static")

# Filled by proofing.init_app() with functions from app.py (no circular import):
#   get_db, log_activity(conn, kind, job_id, message, who), job_label(job), now_str(),
#   match_templates(upc, customer, label_size, part_number), jm_settings(), z_drive_base
HOOKS: Dict[str, Any] = {}

MEDIA_REGISTRY: Dict[str, Path] = {}
MEDIA_TOKEN_BY_PATH: Dict[str, tuple] = {}
MEDIA_LOCK = threading.Lock()
VALIDATION_CACHE: Dict[str, tuple] = {}
VALIDATION_CACHE_LOCK = threading.Lock()
PREVIEW_TASKS: Dict[str, Dict[str, Any]] = {}
PREVIEW_TASKS_LOCK = threading.Lock()
BARTENDER_LOCK = threading.Lock()       # one BarTender render at a time on the host PC
SAVE_LOCK = threading.RLock()           # check-then-write of proof files is atomic


class Conflict(Exception):
    """Someone else changed or moved the proof since this browser opened it."""

    def __init__(self, message, **extra):
        super().__init__(message)
        self.extra = extra


# ══════════════════════════════════════════════════════════════════════════════
# Small helpers
# ══════════════════════════════════════════════════════════════════════════════
def _now() -> str:
    return HOOKS["now_str"]() if "now_str" in HOOKS else datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _who() -> Optional[str]:
    try:
        return (request.headers.get("X-Operator") or "").strip()[:40] or None
    except RuntimeError:
        return None


@contextlib.contextmanager
def _db():
    conn = HOOKS["get_db"]()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _payload() -> Dict[str, Any]:
    data = request.get_json(silent=True)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object.")
    return data


def ok(**kw):
    return jsonify({"ok": True, **kw})


def _rev(path) -> str:
    """Change marker for a saved proof (mtime + size). Survives the moves between the
    active / approved / archive folders, changes on every write."""
    try:
        st = Path(path).stat()
        return f"{st.st_mtime_ns:x}-{st.st_size:x}"
    except OSError:
        return ""


def _preview_log(message: str) -> None:
    try:
        BARTENDER_PREVIEW_LOG.parent.mkdir(parents=True, exist_ok=True)
        with BARTENDER_PREVIEW_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n")
    except Exception:
        pass


def sanitize_filename(value: str, fallback: str = "proof") -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip()).strip("._")
    return value[:100] or fallback


def _norm(p) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


def _inside(path, *roots) -> bool:
    target = _norm(path)
    for r in roots:
        if not r:
            continue
        root = _norm(r)
        try:
            if os.path.commonpath([root, target]) == root:
                return True
        except ValueError:          # different drives on Windows
            continue
    return False


def _job_file(raw, *, allow_archive=False, must_exist=True) -> Path:
    """A saved-proof path from the browser, accepted only inside jobs/, jobs/approved/
    (and jobs/archive/ when allowed) of the proofing data folder."""
    raw = str(raw or "").strip()
    if not raw:
        raise ValueError("No saved proof was selected.")
    p = Path(raw)
    if not p.name.endswith(".rfidproof.json"):
        raise PermissionError("That file isn't a saved proof.")
    folders = {_norm(JOBS_DIR), _norm(APPROVED_DIR)} | ({_norm(ARCHIVE_DIR)} if allow_archive else set())
    if _norm(p.parent) not in folders:
        raise PermissionError("That file isn't in the proofing data folder.")
    if must_exist and not p.is_file():
        raise FileNotFoundError(f"Saved proof not found: {p.name}. It may have been moved; open it from the Queue.")
    return Path(os.path.join(os.path.realpath(str(p.parent)), p.name))


def _unique_new_path(folder: Path, stem: str) -> Path:
    """A new proof never overwrites another one with the same customer / job / revision."""
    target, n = folder / f"{stem}.rfidproof.json", 2
    while target.exists() or (APPROVED_DIR / target.name).exists():
        target = folder / f"{stem}_{n}.rfidproof.json"
        n += 1
    return target


def _check_rev(target: Path, payload: Dict[str, Any]) -> None:
    if payload.get("force"):
        return
    if not target.is_file():
        raise Conflict("This proof was moved, archived or deleted since it was opened here. "
                       "Open it again from the Queue.", moved=True)
    sent = str(payload.get("rev") or "").strip()
    if sent and sent != _rev(target):
        try:
            other = load_job(str(target))
            when = other.updated_at.replace("T", " ") if other.updated_at else "just now"
        except Exception:
            when = "just now"
        raise Conflict(f"Someone else saved this proof ({when}) after it was opened here. "
                       "Reload it to see their changes, or overwrite them with yours.", current_rev=_rev(target))


# ── media (preview images, PDFs, packages) served by token ────────────────────
def register_media(path) -> str:
    p = Path(path).resolve()
    key = str(p)
    try:
        st = p.stat()
        signature = (st.st_mtime_ns, st.st_size)
    except OSError:
        signature = None
    with MEDIA_LOCK:
        existing = MEDIA_TOKEN_BY_PATH.get(key)
        if existing and existing[1] == signature and existing[0] in MEDIA_REGISTRY:
            token = existing[0]
        else:
            token = secrets.token_urlsafe(16)
            MEDIA_REGISTRY[token] = p
            MEDIA_TOKEN_BY_PATH[key] = (token, signature)
        if len(MEDIA_REGISTRY) > 2000:
            for old_token in list(MEDIA_REGISTRY)[:500]:
                old_path = MEDIA_REGISTRY.pop(old_token, None)
                if old_path is not None:
                    cur = MEDIA_TOKEN_BY_PATH.get(str(old_path))
                    if cur and cur[0] == old_token:
                        MEDIA_TOKEN_BY_PATH.pop(str(old_path), None)
    return f"/proofing/media/{token}/{urllib.parse.quote(p.name)}"


def _resolve_asset(value: str) -> Optional[Path]:
    if not value:
        return None
    p = Path(value)
    if not p.is_absolute():
        for candidate in (ROOT / p, ROOT / "config" / p, INLAY_IMAGES_DIR / p.name):
            if candidate.is_file():
                return candidate
    return p if p.is_file() else None


def media_for_job(job: ProofJob) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for key, value in (("preview_url", job.preview_path), ("inlay_image_url", job.inlay_image_path)):
        p = _resolve_asset(value)
        if p:
            result[key] = register_media(p)
    return result


# ── previews kept in the data folder ──────────────────────────────────────────
def _store_preview(src: Path, job: ProofJob, kind: str) -> Path:
    """Copy a rendered or uploaded preview into previews/<proof key>/ and trim old ones
    (the newest 6 per proof always stay; nothing younger than 30 days is removed)."""
    key = jm_link.proof_key(job.proof_id) or "unsaved"
    folder = PREVIEWS_DIR / key
    folder.mkdir(parents=True, exist_ok=True)
    suffix = src.suffix.lower() if src.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp") else ".png"
    dest = folder / f"{kind}_{datetime.now():%Y%m%d_%H%M%S_%f}{suffix}"
    shutil.copy2(src, dest)
    keep, min_age = (50, 7) if key == "unsaved" else (6, 30)
    try:
        files = sorted((f for f in folder.iterdir() if f.is_file()), key=lambda f: f.stat().st_mtime, reverse=True)
        for old in files[keep:]:
            if time.time() - old.stat().st_mtime > min_age * 86400:
                old.unlink(missing_ok=True)
    except OSError:
        pass
    return dest


def _adopt_preview(job: ProofJob) -> None:
    """On a proof's first save, move its preview out of previews/unsaved into its own folder."""
    key = jm_link.proof_key(job.proof_id)
    p = Path(job.preview_path) if job.preview_path else None
    if not (key and p and p.is_file() and _norm(p.parent) == _norm(PREVIEWS_DIR / "unsaved")):
        return
    folder = PREVIEWS_DIR / key
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / p.name
    try:
        shutil.move(str(p), str(dest))
        job.preview_path = str(dest)
    except OSError:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# Kept from web_server.py (same behaviour)
# ══════════════════════════════════════════════════════════════════════════════
def job_from_payload(payload: Dict[str, Any]) -> ProofJob:
    data = payload.get("job", payload)
    if not isinstance(data, dict):
        raise ValueError("Job payload is invalid.")
    clean = dict(data)
    for key in ("label_width", "label_height", "inlay_width_mm", "inlay_height_mm", "inlay_x_mm", "inlay_y_mm"):
        try:
            clean[key] = float(clean.get(key, 0) or 0)
        except Exception:
            clean[key] = 0.0
    for key in ("inlay_preview_opacity", "inlay_rotation", "filter_value", "company_prefix_digits"):
        try:
            clean[key] = int(float(clean.get(key, 0) or 0))
        except Exception:
            clean[key] = 0
    if str(clean.get("encoding_type", "SGTIN-96") or "SGTIN-96").strip() == "SGTIN-96":
        try:
            clean["serial"] = int(float(clean.get("serial", 0) or 0))
        except Exception:
            clean["serial"] = 0
    else:
        clean["serial"] = str(clean.get("serial", "") or "").strip()
    job = ProofJob.from_dict(clean)
    company = str(job.preparing_company or "").strip()
    if company.casefold().startswith("starport"):
        job.preparing_company = "Starport Technologies"
    elif company:
        job.preparing_company = "MCC Label"
    return job


def validation_payload(job: ProofJob, require_preview: bool = True, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    settings = settings or load_settings()
    barcode_quality = bool(settings.get("barcode_quality_checks", True))
    cache_material = json.dumps({"job": job.as_dict(), "require_preview": require_preview,
                                 "barcode_quality": barcode_quality},
                                sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    cache_key = hashlib.blake2b(cache_material, digest_size=16).hexdigest()
    now = time.monotonic()
    with VALIDATION_CACHE_LOCK:
        cached = VALIDATION_CACHE.get(cache_key)
        if cached and now - cached[0] < 2.5:
            return dict(cached[1])
    results = validate_job(job, require_preview=require_preview) + jm_checks(job)
    if not barcode_quality:
        results = [r for r in results if not (r.name.startswith("Barcode 1 data characters") or r.name.startswith("Barcode 1 data length")
                                              or r.name.startswith("Barcode 2 data characters") or r.name.startswith("Barcode 2 data length"))]
    blocking = [r for r in results if not r.ok and r.severity != "warning"]
    warnings = [r for r in results if not r.ok and r.severity == "warning"]
    payload = {
        "ready": all_pass(results),
        "status": "ready" if not blocking and not warnings else ("warning" if not blocking else "error"),
        "blocking_count": len(blocking),
        "warning_count": len(warnings),
        "message": ("Ready to generate final proof." if not blocking and not warnings else
                    f"Ready with {len(warnings)} warning(s)." if not blocking else
                    f"{len(blocking)} item(s) need correction before final output."),
        "issues": [{"name": r.name, "detail": r.detail, "severity": r.severity} for r in blocking + warnings],
    }
    with VALIDATION_CACHE_LOCK:
        VALIDATION_CACHE[cache_key] = (now, payload)
        if len(VALIDATION_CACHE) > 256:
            for key, _ in sorted(VALIDATION_CACHE.items(), key=lambda item: item[1][0])[:64]:
                VALIDATION_CACHE.pop(key, None)
    return dict(payload)


REVIEW_FIELD_LABELS: Dict[str, str] = {
    "customer": "Customer", "job_number": "Job #", "part_number": "Part #", "quantity": "Quantity",
    "revision": "Label Revision", "proof_revision": "Proof Revision", "gtin": "GTIN / UPC",
    "barcode_type_1": "Barcode 1 Type", "barcode_data_1": "Barcode 1 Data", "barcode_type_2": "Barcode 2 Type",
    "barcode_data_2": "Barcode 2 Data", "font_size_callout": "Font Size", "label_size": "Label Size",
    "encoding_type": "Encoding Type", "serial": "Serial", "epc_hex": "EPC / Data", "notes": "Proof Notes",
    "preview_path": "Artwork Preview", "inlay": "RFID Inlay", "inlay_placement": "RFID Placement",
    "master_company": "Preparing Company",
}


def proof_review_snapshot(job: ProofJob, settings: Dict[str, Any]) -> Dict[str, Any]:
    return {
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
        "label_size": f"{float(job.label_width):.3f} x {float(job.label_height):.3f} in",
        "encoding_type": str(job.encoding_type or "").strip(),
        "serial": str(job.serial or ""),
        "epc_hex": str(job.epc_hex or "").strip(),
        "notes": str(job.notes or "").strip(),
        "preview_path": str(job.preview_path or "").strip(),
        "inlay": str(job.inlay or "").strip(),
        "inlay_placement": f"X {float(job.inlay_x_mm):.2f} mm / Y {float(job.inlay_y_mm):.2f} mm / {int(job.inlay_rotation)} deg",
        "master_company": str(settings.get("master_company", "MCC Label") or "MCC Label").strip(),
    }


def proof_review_payload(job: ProofJob, settings: Dict[str, Any], require_preview: bool = True) -> Dict[str, Any]:
    validation = validation_payload(job, require_preview=require_preview, settings=settings)
    current = proof_review_snapshot(job, settings)
    previous = dict(job.last_generated_snapshot or {})
    changes = []
    if previous:
        for key in REVIEW_FIELD_LABELS:
            before, after = previous.get(key, ""), current.get(key, "")
            if str(before) != str(after):
                changes.append({"key": key, "label": REVIEW_FIELD_LABELS.get(key, key), "before": before, "after": after})
    same_revision_changed = bool(previous and str(job.last_generated_proof_revision or "").strip()
                                 == str(job.proof_revision or "").strip() and changes)
    return {"validation": validation, "current": current, "previous": previous, "changes": changes,
            "change_count": len(changes), "same_revision_changed": same_revision_changed,
            "last_generated_revision": str(job.last_generated_proof_revision or ""),
            "first_generation": not bool(previous)}


def named_data_for_job(job: ProofJob, mapping: Dict[str, str]) -> Dict[str, str]:
    values = {
        "customer": job.customer, "job_number": job.job_number, "part_number": job.part_number,
        "gtin": job.gtin, "encoding_type": job.encoding_type, "barcode_type_1": job.barcode_type_1,
        "barcode_data_1": job.barcode_data_1, "barcode_type_2": job.barcode_type_2,
        "barcode_data_2": job.barcode_data_2, "font_size_callout": job.font_size_callout,
        "epc": job.epc_hex, "serial": str(job.serial), "quantity": job.quantity, "revision": job.revision,
        "proof_revision": job.proof_revision, "inlay": job.inlay, "chip": job.chip,
    }
    return {str(mapping.get(k, "")): str(v) for k, v in values.items() if mapping.get(k)}


def _bartender_mapping(job: ProofJob, settings: Dict[str, Any]) -> Dict[str, str]:
    mapping = dict(settings.get("named_data_sources", {}))
    profile = find_template_profile(load_template_profiles(), job.template_path)
    if profile and isinstance(profile.get("named_data_sources"), dict):
        mapping.update(profile["named_data_sources"])
    return mapping


def _check_template(job: ProofJob) -> None:
    t = str(job.template_path or "").strip()
    if not t:
        raise ValueError("Select a BarTender template first.")
    if Path(t).suffix.lower() != ".btw":
        raise ValueError("The template must be a BarTender .btw file.")


def _copy_inlay_image(src: Path, destination: Optional[Path] = None) -> str:
    destination = destination or INLAY_IMAGES_DIR
    destination.mkdir(parents=True, exist_ok=True)
    dest = destination / sanitize_filename(src.name, "inlay.png")
    stem, suffix = dest.stem, dest.suffix or src.suffix or ".png"
    counter = 2
    while dest.exists():
        try:
            if dest.read_bytes() == src.read_bytes():
                break
        except Exception:
            pass
        dest = destination / f"{stem}_{counter}{suffix}"
        counter += 1
    if not dest.exists():
        shutil.copy2(src, dest)
    try:
        return str(dest.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(dest)


def _read_inlay_folder_metadata(folder: Path) -> list:
    rows: list = []
    json_path = folder / "inlays.json"
    if json_path.is_file():
        try:
            raw = json.loads(json_path.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                raw = raw.get("inlays", [])
            if isinstance(raw, list):
                rows.extend(dict(x) for x in raw if isinstance(x, dict))
        except Exception:
            pass
    csv_path = folder / "inlays.csv"
    if csv_path.is_file():
        try:
            with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
                rows.extend(dict(x) for x in csv.DictReader(fh))
        except Exception:
            pass
    return rows


def sync_inlays_from_folder(folder_path: str, current: Optional[list] = None,
                            image_destination: Optional[Path] = None) -> tuple:
    """Import/update inlay profiles from an image folder; never auto-deletes a profile."""
    folder = Path(str(folder_path or "").strip()).expanduser()
    if not folder.is_dir():
        raise ValueError("Select a valid inlay source folder first (a folder the host PC can reach).")
    destination = image_destination or INLAY_IMAGES_DIR
    current_items = [dict(x) for x in (current if current is not None else load_inlays())]
    by_name = {str(x.get("name", "")).strip().casefold(): i for i, x in enumerate(current_items) if str(x.get("name", "")).strip()}
    metadata = _read_inlay_folder_metadata(folder)

    def norm(v):
        return re.sub(r"[^a-z0-9]+", "", str(v or "").casefold())

    meta_by_file, meta_by_name = {}, {}
    for row in metadata:
        name = str(row.get("name", "") or "").strip()
        filename = str(row.get("image", row.get("filename", row.get("image_path", row.get("file", "")))) or "").strip()
        if name:
            meta_by_name[norm(name)] = row
        if filename:
            meta_by_file[Path(filename).name.casefold()] = row
            meta_by_file[norm(Path(filename).stem)] = row

    image_exts = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
    images = sorted([x for x in folder.rglob("*") if x.is_file() and x.suffix.lower() in image_exts], key=lambda x: str(x).casefold())
    touched = set()
    added = updated = imported_images = 0

    def num(row, key, fallback=0.0):
        try:
            raw = row.get(key, "")
            return float(raw) if str(raw).strip() else fallback
        except Exception:
            return fallback

    def opacity(row, fallback=45):
        try:
            raw = row.get("preview_opacity", row.get("opacity", ""))
            value = int(float(raw)) if str(raw).strip() else fallback
            return max(0, min(100, value))
        except Exception:
            return fallback

    stamp = lambda: datetime.now().isoformat(timespec="seconds")
    for src in images:
        meta = meta_by_file.get(src.name.casefold()) or meta_by_file.get(norm(src.stem)) or meta_by_name.get(norm(src.stem)) or {}
        name = str(meta.get("name", "") or "").strip() or re.sub(r"[_]+", " ", src.stem).strip()
        key = name.casefold()
        existing = current_items[by_name[key]] if key in by_name else None
        image_path = _copy_inlay_image(src, destination)
        imported_images += 1
        if existing is None:
            current_items.append({
                "name": name, "chip": str(meta.get("chip", meta.get("ic", "")) or "").strip(),
                "width_mm": num(meta, "width_mm", num(meta, "inlay_width_mm", 0.0)),
                "height_mm": num(meta, "height_mm", num(meta, "inlay_height_mm", 0.0)),
                "notes": str(meta.get("notes", meta.get("construction_notes", "")) or "").strip(),
                "image_path": image_path, "preview_opacity": opacity(meta, 45), "source_type": "folder",
                "source_folder": str(folder), "source_file": src.name, "last_synced_at": stamp()})
            by_name[key] = len(current_items) - 1
            added += 1
        else:
            existing.update(image_path=image_path, source_type="folder", source_folder=str(folder),
                            source_file=src.name, last_synced_at=stamp())
            if str(meta.get("chip", meta.get("ic", "")) or "").strip():
                existing["chip"] = str(meta.get("chip", meta.get("ic", ""))).strip()
            width = num(meta, "width_mm", num(meta, "inlay_width_mm", 0.0))
            height = num(meta, "height_mm", num(meta, "inlay_height_mm", 0.0))
            if width > 0:
                existing["width_mm"] = width
            if height > 0:
                existing["height_mm"] = height
            if str(meta.get("notes", meta.get("construction_notes", "")) or "").strip():
                existing["notes"] = str(meta.get("notes", meta.get("construction_notes", ""))).strip()
            if str(meta.get("preview_opacity", meta.get("opacity", "")) or "").strip():
                existing["preview_opacity"] = opacity(meta, int(existing.get("preview_opacity", 45) or 45))
            updated += 1
        touched.add(key)

    for meta in metadata:
        name = str(meta.get("name", "") or "").strip()
        if not name or name.casefold() in touched or name.casefold() in by_name:
            continue
        filename = str(meta.get("image", meta.get("filename", meta.get("image_path", ""))) or "").strip()
        image_path = ""
        if filename:
            candidate = folder / filename
            if candidate.is_file() and candidate.suffix.lower() in image_exts:
                image_path = _copy_inlay_image(candidate, destination)
                imported_images += 1
        current_items.append({
            "name": name, "chip": str(meta.get("chip", meta.get("ic", "")) or "").strip(),
            "width_mm": num(meta, "width_mm", num(meta, "inlay_width_mm", 0.0)),
            "height_mm": num(meta, "height_mm", num(meta, "inlay_height_mm", 0.0)),
            "notes": str(meta.get("notes", meta.get("construction_notes", "")) or "").strip(),
            "image_path": image_path, "preview_opacity": opacity(meta, 45), "source_type": "folder",
            "source_folder": str(folder), "source_file": filename, "last_synced_at": stamp()})
        by_name[name.casefold()] = len(current_items) - 1
        added += 1
    return current_items, {"added": added, "updated": updated, "images": imported_images, "total": len(current_items)}


def recent_job_details(paths: list) -> list:
    rows = []
    for raw in paths[:20]:
        try:
            path = Path(str(raw))
            if not path.is_file():
                continue
            job = load_job(str(path))
            rows.append({
                "path": str(path), "filename": path.name, "customer": job.customer, "job_number": job.job_number,
                "part_number": job.part_number, "label_revision": job.revision, "proof_revision": job.proof_revision,
                "operator_initials": job.operator_initials, "proof_id": job.proof_id,
                "lifecycle_status": job.lifecycle_status, "last_generated_at": job.last_generated_at, "inlay": job.inlay,
                "jm_job_number": job.jm_job_number,
                "modified_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            })
        except Exception:
            continue
    return rows


def _remember_recent(saved: str, drop: str = "") -> tuple:
    settings = load_settings()
    recent = [saved] + [str(x) for x in settings.get("recent_job_files", [])
                        if str(x) not in (saved, drop) and Path(str(x)).is_file()]
    settings["recent_job_files"] = recent[:12]
    save_settings(settings)
    return recent[:12], recent_job_details(recent[:12])


def _forget_recent(path: str) -> None:
    settings = load_settings()
    settings["recent_job_files"] = [str(x) for x in settings.get("recent_job_files", [])
                                    if str(x) != path and Path(str(x)).is_file()]
    save_settings(settings)


# ══════════════════════════════════════════════════════════════════════════════
# Job Manager link
# ══════════════════════════════════════════════════════════════════════════════
JM_PUBLIC = ("id", "job_number", "job_ticket", "customer", "title", "status", "upc", "start_serial", "quantity",
             "overage_pct", "lpr", "label_size", "inlay_type", "part_number", "po_number", "folder_path",
             "archived_at", "data_sheet", "template_path", "job_type")


def _jm_public(row) -> Optional[dict]:
    if not row:
        return None
    out = {k: row.get(k) for k in JM_PUBLIC}
    out["open_url"] = f"/?job={row['id']}"
    return out


def _jm_row(job: ProofJob, conn=None) -> Optional[dict]:
    if not (job.jm_job_id or job.jm_job_number):
        return None
    if conn is not None:
        return jm_link.find_job(conn, job.jm_job_id, job.jm_job_number)
    with _db() as c:
        return jm_link.find_job(c, job.jm_job_id, job.jm_job_number)


def _log(conn, row, message: str) -> None:
    if row and "log_activity" in HOOKS:
        label = HOOKS["job_label"](row) if "job_label" in HOOKS else row.get("job_number", "")
        HOOKS["log_activity"](conn, "proof", row["id"], f"{label}: {message}", _who() or "Job sync")


def _index(job: ProofJob, path, log_message: Optional[str] = None) -> Optional[dict]:
    """Update the job_proofs index after a save / move, optionally logging to the job's activity."""
    with _db() as c:
        row = _jm_row(job, c)
        jm_link.upsert(c, _now(), job, path, row)
        if log_message:
            _log(c, row, log_message)
    return row


def _proof_label(job: ProofJob) -> str:
    return f"proof P{job.proof_revision or '1'}" + (f" ({job.proof_id})" if job.proof_id else "")


def jm_checks(job: ProofJob) -> list:
    """Warnings for a linked proof whose data doesn't match the job in Job Manager."""
    if not (job.jm_job_id or job.jm_job_number):
        return []
    try:
        row = _jm_row(job)
    except Exception:
        return []
    if not row:
        return [ValidationItem(False, "Linked Job Manager job found",
                               f"This proof is linked to {job.jm_job_number or job.jm_job_id}, which is no longer in Job Manager.",
                               "warning")]
    items = []
    upc = (row.get("upc") or "").strip()
    encoding = str(job.encoding_type or "").strip()
    if upc and encoding == "SGTIN-96":
        g = digits_only(job.gtin)
        if g and g.lstrip("0") != upc.lstrip("0"):
            items.append(ValidationItem(False, "GTIN matches the Job Manager job",
                                        f"Proof GTIN {job.gtin}; {row['job_number']} is UPC {upc}.", "warning"))
            return items
        try:
            serial = int(job.serial)
        except (TypeError, ValueError):
            serial = 0
        epc = re.sub(r"\s", "", str(job.epc_hex or "")).upper()
        if epc and 0 < serial < (1 << 38):
            expect = generate_epc(upc, serial)
            if epc != expect:
                items.append(ValidationItem(False, "EPC matches Job Manager encoding",
                                            f"Job Manager encodes UPC {upc} serial {serial:,} as {expect} "
                                            f"(filter 1, 7-digit company prefix). The proof shows {epc}.", "warning"))
            else:
                items.append(ValidationItem(True, "EPC matches Job Manager encoding", expect))
        start = row.get("start_serial")
        if start and row.get("quantity") and serial:
            end = start + production_qty(row["quantity"], row.get("overage_pct") or 0) - 1
            if not start <= serial <= end:
                items.append(ValidationItem(False, "Serial is in the job's range",
                                            f"Serial {serial:,} is outside {row['job_number']}'s range {start:,}–{end:,}.",
                                            "warning"))
    elif row.get("data_sheet") == CDO_SHEET and row.get("start_serial") and encoding != "SGTIN-96":
        expect = cdo_epc(row["start_serial"])
        epc = re.sub(r"\s", "", str(job.epc_hex or "")).upper()
        if epc and epc != expect and str(job.serial) == str(row["start_serial"]):
            items.append(ValidationItem(False, "EPC matches the CDO data sheet",
                                        f"Serial {row['start_serial']:,} is {expect} on the CDO sheet; the proof shows {epc}.",
                                        "warning"))
    return items


VARIABLE_SPEC_START = "[[RFID_VARIABLE_SPEC_V1]]"
VARIABLE_SPEC_END = "[[/RFID_VARIABLE_SPEC_V1]]"


def _initials(name: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", name or "")
    if not words:
        return ""
    if len(words) == 1:
        return words[0][:3].upper()
    return "".join(w[0] for w in words)[:8].upper()


def _label_size(text: str) -> tuple:
    m = re.search(r"(\d*\.?\d+)\s*(?:\"|''|”|in\b|inch(?:es)?\b)?\s*[xX×]\s*(\d*\.?\d+)", text or "")
    if not m:
        return None, None
    try:
        return float(m.group(1)), float(m.group(2))
    except ValueError:
        return None, None


def _match_inlay(name: str) -> Optional[dict]:
    key = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    if not key:
        return None
    items = load_inlays()
    for item in items:
        if re.sub(r"[^a-z0-9]", "", str(item.get("name", "")).lower()) == key:
            return item
    hits = [i for i in items if key in re.sub(r"[^a-z0-9]", "", str(i.get("name", "")).lower())]
    return hits[0] if len(hits) == 1 else None


def proof_from_jm_job(row: dict) -> tuple:
    """(ProofJob, notes): a new proof filled from a Job Manager job. The EPC is made the way
    Job Manager encodes this job (filter 1, 7-digit prefix, first serial of the job), so the
    proof shows the first tag the encoding database will hold."""
    settings = load_settings()
    notes = []
    job = ProofJob()
    job.customer = row.get("customer") or ""
    job.preparing_company = settings.get("master_company") or "MCC Label"
    job.job_number = str(row.get("job_ticket") or row.get("job_number") or "").strip()
    job.part_number = row.get("part_number") or ""
    job.quantity = str(row["quantity"]) if row.get("quantity") else ""
    job.due_date = row.get("due_date") or ""
    job.operator_initials = (_initials(_who() or "") or str(settings.get("default_operator_initials") or "").upper())[:8]
    w, h = _label_size(row.get("label_size") or "")
    if w and h:
        job.label_width, job.label_height = w, h
    spec = {
        "version": 1, "po_data_reference": row.get("po_number") or "",
        "variable_data_source": "Customer PO / approved data file",
        "barcode_1_classification": "Static", "barcode_1_source": "Customer PO / item specification",
        "barcode_1_rule": "Use the approved barcode data shown on this proof",
        "barcode_2_classification": "Static", "barcode_2_source": "Customer PO / item specification",
        "barcode_2_rule": "Use the approved barcode data shown on this proof",
        "serial_classification": "Variable", "serial_source": "Production serial allocation",
        "serial_rule": "One unique serial / reference per label",
        "epc_classification": "Variable", "epc_source": "Generated from approved encoding fields",
        "epc_rule": "Generate per the selected encoding scheme using the approved static fields and variable serial / reference",
        "variable_data_notes": "",
    }
    upc = (row.get("upc") or "").strip()
    start = row.get("start_serial")
    total = production_qty(row["quantity"], row.get("overage_pct") or 0) if row.get("quantity") else None
    if upc:
        job.gtin, job.barcode_type_1, job.barcode_data_1 = upc, "UPC-A", upc
        job.encoding_type, job.filter_value, job.company_prefix_digits = "SGTIN-96", 1, 7
        serial = int(start or 1)
        r = encode_sgtin96(upc, 7, serial, 1)
        if r.epc_hex != generate_epc(upc, serial):         # the two encoders must agree
            raise RuntimeError(f"EPC encoders disagree for UPC {upc}; proof not created.")
        job.serial, job.epc_hex, job.company_prefix, job.item_reference = serial, r.epc_hex, r.company_prefix, r.item_reference
        if start and total:
            spec["serial_source"] = f"Job Manager serial allocation ({row['job_number']})"
            spec["serial_rule"] = f"Sequential, one per label: {start:,} to {start + total - 1:,}"
            spec["epc_rule"] = "SGTIN-96, filter 1, 7-digit company prefix, from the UPC and each label's serial"
        else:
            notes.append("The job has no serial range yet; the proof shows serial 1.")
    else:
        sheet = row.get("data_sheet")
        job.barcode_type_1 = ""
        job.encoding_type = "Custom EPC / Hex" if sheet in (CDO_SHEET, GO2_SHEET) else "Other / Customer Specific"
        notes.append("No UPC on this job: choose the barcode type and enter its data.")
        if sheet == CDO_SHEET and start:
            job.serial, job.epc_hex = str(start), cdo_epc(start)
            spec["serial_source"] = f"Job Manager CDO serial series ({row['job_number']})"
            spec["serial_rule"] = (f"Sequential, one per label: {start:,} to {start + total - 1:,}" if total
                                   else "Sequential, one per label, never repeated")
            spec["epc_source"] = "CDO data sheet (Job Manager)"
            spec["epc_rule"] = "The serial as 8 hexadecimal digits"
        elif sheet == GO2_SHEET:
            job.serial = ""
            spec["serial_classification"] = "Static"
            spec["serial_source"] = spec["serial_rule"] = "Not used (random EPC)"
            spec["epc_source"] = "GO2 random EPC data sheet (Job Manager)"
            spec["epc_rule"] = f"{GO2_PREFIX} followed by 19 random hexadecimal digits, unique per label"
            notes.append("GO2 EPCs are random: enter one sample EPC from the job's Data file on the proof.")
        else:
            job.serial = ""
            notes.append("Special job: enter the encoding type and EPC / data from the customer's order.")
    inlay = _match_inlay(row.get("inlay_type") or "")
    if inlay:
        job.inlay = inlay.get("name") or ""
        job.chip = inlay.get("chip") or inlay.get("ic") or ""
        job.inlay_width_mm = float(inlay.get("width_mm") or inlay.get("inlay_width_mm") or 0)
        job.inlay_height_mm = float(inlay.get("height_mm") or inlay.get("inlay_height_mm") or 0)
        job.inlay_image_path = inlay.get("image_path") or inlay.get("preview_image") or ""
        if inlay.get("preview_opacity") is not None:
            job.inlay_preview_opacity = int(inlay.get("preview_opacity") or 45)
    elif row.get("inlay_type"):
        notes.append(f"Inlay \"{row['inlay_type']}\" isn't in the inlay library; pick the inlay on the proof.")
    tpl = row.get("template_path") or ""
    if tpl and os.path.isfile(tpl):
        job.template_path = tpl
    elif "match_templates" in HOOKS:
        try:
            m = HOOKS["match_templates"](upc, row.get("customer") or "", row.get("label_size") or "",
                                         row.get("part_number") or "")
            pick = next((c for c in m["candidates"] if c["id"] == m.get("auto_id")), None)
            if pick and m.get("confidence") in ("upc", "history") and os.path.isfile(pick["file_path"]):
                job.template_path = pick["file_path"]
                notes.append(f"Template matched by {', '.join(pick['reasons'])}: {pick['file_name']}.")
        except Exception:
            pass
    if not job.template_path:
        notes.append("No template was found for this job; choose one under Artwork.")
    job.internal_comments = (f"Started from Job Manager {row['job_number']}.\n"
                             f"{VARIABLE_SPEC_START}{json.dumps(spec)}{VARIABLE_SPEC_END}")
    job.jm_job_id, job.jm_job_number = int(row["id"]), row["job_number"]
    # Sync baseline: nothing on a new proof was typed by anyone yet, so every job-owned field
    # (filled from the job, or still a default) may follow later changes to the job.
    pv = proof_values(job)
    job.jm_sync = {f: pv.get(f) for f, _ in SYNC_FIELDS}
    return job, notes


# ══════════════════════════════════════════════════════════════════════════════
# Job ⇄ proof sync
# ══════════════════════════════════════════════════════════════════════════════
# Job → proof: the fields below belong to the job. Each proof remembers the value it last took
# from the job (job.jm_sync). When the job changes, a field is updated only while the proof
# still holds that remembered value (or is blank), so anything typed on the proof is kept and
# reported as "differs". Approved / archived proofs are never changed, only reported.
# Proof → job: blank job fields in BACKFILL are filled in from the proof when it is saved.
SYNC_FIELDS = (("customer", "Customer"), ("job_number", "Job #"), ("part_number", "Part #"),
               ("quantity", "Quantity"), ("gtin", "GTIN / UPC"), ("barcode_data_1", "Barcode 1 data"),
               ("label_size", "Label size"), ("serial", "Serial"), ("epc_hex", "EPC"), ("inlay", "RFID inlay"),
               ("template_path", "BarTender template"), ("po_data_reference", "PO / data reference"))
SYNC_LABELS = dict(SYNC_FIELDS)
BACKFILL = (("part_number", "part_number", "Part #"), ("po_data_reference", "po_number", "PO #"),
            ("inlay", "inlay_type", "Inlay"))
SYNC_INTERVAL = 30          # seconds between background checks for changed jobs


def _spec_of(job: ProofJob) -> dict:
    raw = str(job.internal_comments or "")
    a = raw.find(VARIABLE_SPEC_START)
    b = raw.find(VARIABLE_SPEC_END, a + 1) if a >= 0 else -1
    if a < 0 or b < 0:
        return {}
    try:
        spec = json.loads(raw[a + len(VARIABLE_SPEC_START):b])
        return spec if isinstance(spec, dict) else {}
    except ValueError:
        return {}


def _set_spec(job: ProofJob, key: str, value) -> None:
    raw = str(job.internal_comments or "")
    spec = _spec_of(job) or {"version": 1}
    spec[key] = value
    a = raw.find(VARIABLE_SPEC_START)
    b = raw.find(VARIABLE_SPEC_END, a + 1) if a >= 0 else -1
    rest = (raw[:a] + raw[b + len(VARIABLE_SPEC_END):]).strip() if a >= 0 and b >= 0 else raw.strip()
    job.internal_comments = "\n".join(x for x in (rest, f"{VARIABLE_SPEC_START}{json.dumps(spec)}{VARIABLE_SPEC_END}") if x)


def job_values(row: dict) -> dict:
    """The job's values in proof terms. A field the job doesn't specify is left out."""
    v = {"customer": row.get("customer"), "job_number": row.get("job_ticket") or row.get("job_number"),
         "part_number": row.get("part_number"), "quantity": str(row["quantity"]) if row.get("quantity") else None,
         "po_data_reference": row.get("po_number")}
    upc, start = (row.get("upc") or "").strip(), row.get("start_serial")
    if upc:
        v["gtin"] = v["barcode_data_1"] = upc
        if start:
            v["serial"], v["epc_hex"] = int(start), generate_epc(upc, int(start))
    elif row.get("data_sheet") == CDO_SHEET and start:
        v["serial"], v["epc_hex"] = str(start), cdo_epc(start)
    w, h = _label_size(row.get("label_size") or "")
    if w and h:
        v["label_size"] = f"{w:g} x {h:g}"
    inlay = _match_inlay(row.get("inlay_type") or "")
    if inlay:
        v["inlay"] = inlay.get("name")
    tpl = row.get("template_path") or ""
    if tpl and os.path.isfile(tpl):
        v["template_path"] = tpl
    return {k: x for k, x in v.items() if x not in (None, "")}


def proof_values(job: ProofJob) -> dict:
    return {"customer": job.customer, "job_number": job.job_number, "part_number": job.part_number,
            "quantity": job.quantity, "gtin": job.gtin, "barcode_data_1": job.barcode_data_1,
            "label_size": f"{float(job.label_width or 0):g} x {float(job.label_height or 0):g}",
            "serial": job.serial, "epc_hex": job.epc_hex, "inlay": job.inlay, "template_path": job.template_path,
            "po_data_reference": _spec_of(job).get("po_data_reference", "")}


def _key(field: str, value) -> str:
    """Comparison form: case and spacing that don't matter are ignored."""
    s = "" if value is None else str(value).strip()
    if field == "template_path":
        return os.path.normcase(s)
    if field == "epc_hex":
        return re.sub(r"\s", "", s).upper()
    if field == "serial":
        try:
            return str(int(float(s))) if s else ""
        except ValueError:
            return s
    if field == "label_size" and s in ("0 x 0",):
        return ""
    return s.casefold()


def _show(field: str, value) -> str:
    s = "" if value is None else str(value)
    return re.split(r"[\\/]", s)[-1] if field == "template_path" else s


def _apply(job: ProofJob, field: str, value) -> None:
    if field == "label_size":
        job.label_width, job.label_height = _label_size(value)
    elif field == "serial":
        job.serial = int(value) if str(job.encoding_type) == "SGTIN-96" else str(value)
    elif field == "inlay":
        item = _match_inlay(value) or {"name": value}
        job.inlay = item.get("name") or value
        job.chip = item.get("chip") or item.get("ic") or job.chip
        job.inlay_width_mm = float(item.get("width_mm") or item.get("inlay_width_mm") or job.inlay_width_mm or 0)
        job.inlay_height_mm = float(item.get("height_mm") or item.get("inlay_height_mm") or job.inlay_height_mm or 0)
        job.inlay_image_path = item.get("image_path") or item.get("preview_image") or job.inlay_image_path
    elif field == "po_data_reference":
        _set_spec(job, "po_data_reference", value)
    else:
        setattr(job, field, value)


def _fix_encoding(job: ProofJob, changed: set) -> None:
    """After the job's UPC / serial / EPC were applied, keep the SGTIN fields consistent with
    Job Manager's encoding (filter 1, 7-digit company prefix)."""
    if str(job.encoding_type) != "SGTIN-96" or not changed & {"gtin", "serial", "epc_hex"}:
        return
    if "epc_hex" in changed:
        job.filter_value, job.company_prefix_digits = 1, 7
    try:
        r = encode_sgtin96(job.gtin, job.company_prefix_digits, int(job.serial), job.filter_value)
        if r.epc_hex == re.sub(r"\s", "", str(job.epc_hex or "")).upper():
            job.company_prefix, job.item_reference = r.company_prefix, r.item_reference
    except Exception:
        pass


def sync_proof(job: ProofJob, row: dict, now: str = "") -> dict:
    """Bring one proof up to date with its job (in memory). Returns what changed / differs."""
    jv, pv = job_values(row), proof_values(job)
    stored = dict(job.jm_sync or {})
    base = {k: v for k, v in stored.items() if not k.startswith("_")}
    meta = {k: v for k, v in stored.items() if k.startswith("_")}
    locked = is_job_locked(job)
    updated, differs = [], []
    for f, label in SYNC_FIELDS:
        if f not in jv:
            continue
        J, P = jv[f], pv.get(f)
        if _key(f, P) == _key(f, J):
            base[f] = J
            continue
        untouched = f in base and _key(f, P) == _key(f, base[f])
        if not locked and (_key(f, P) == "" or untouched):
            _apply(job, f, J)
            updated.append({"field": f, "label": label, "from": _show(f, P), "to": _show(f, J)})
            base[f] = J
        else:
            differs.append({"field": f, "label": label, "proof": _show(f, P), "job": _show(f, J)})
    if updated:
        _fix_encoding(job, {u["field"] for u in updated})
        meta = {"_at": now or _now(), "_changes": [u["label"] for u in updated], "_job": row.get("job_number")}
    job.jm_sync = {**base, **meta}
    return {"updated": updated, "differs": differs, "locked": locked,
            "state": ("approved_changed" if locked else "differs") if differs else "ok",
            "note": ", ".join(d["label"] for d in differs), "job_number": row.get("job_number"),
            "baseline_changed": {k: v for k, v in base.items()} != {k: v for k, v in stored.items() if not k.startswith("_")}}


def _sync_log_text(job: ProofJob, report: dict) -> str:
    parts = [f"{u['label']} {u['from'] or '(blank)'} → {u['to']}" for u in report["updated"]]
    text = f"{_proof_label(job)} updated from the job: " + "; ".join(parts)
    return text if len(text) <= 400 else text[:397] + "…"


def _sync_path(path, *, log: bool = True) -> tuple:
    """Sync one saved proof with its job and write it if anything changed. (job, report|None)"""
    path = Path(path)
    with SAVE_LOCK:
        job = load_job(str(path))
        row = _jm_row(job)
        if not row:
            return job, None
        report = sync_proof(job, row)
        idle = time.time() - path.stat().st_mtime > 600       # not being edited right now, most likely
        if report["updated"] or (report["baseline_changed"] and idle and not report["locked"]):
            save_job(job, str(path))
    key = jm_link.proof_key(job.proof_id)
    with _db() as c:
        prev = c.execute("SELECT sync_state FROM job_proofs WHERE proof_key=?", (key,)).fetchone()
        if report["updated"]:
            jm_link.upsert(c, _now(), job, path, row)
        jm_link.set_sync(c, key, report["state"], report["note"], row.get("updated_at"))
        if log and report["updated"]:
            _log(c, row, _sync_log_text(job, report))
        if log and report["state"] == "approved_changed" and (not prev or prev["sync_state"] != "approved_changed"):
            _log(c, row, f"{_proof_label(job)} is {job.approval_status.lower()} but the job changed since "
                         f"({report['note']}): start a new proof revision to update it")
    return job, report


def backfill_job(job: ProofJob, row: dict) -> list:
    """Proof → job: fill blank job fields (part #, PO #, inlay) from the proof."""
    pv, filled = proof_values(job), []
    with _db() as c:
        for pf, col, label in BACKFILL:
            val = str(pv.get(pf) or "").strip()
            if not val or str(row.get(col) or "").strip():
                continue
            cur = c.execute(f"UPDATE jobs SET {col}=?, updated_at=? WHERE id=? AND COALESCE(TRIM({col}),'')=''",
                            (val, _now(), row["id"]))
            if cur.rowcount:
                filled.append({"field": col, "label": label, "value": val})
                job.jm_sync = {**(job.jm_sync or {}), pf: val}
        if filled:
            _log(c, row, f"filled in from {_proof_label(job)}: " + "; ".join(f"{f['label']} {f['value']}" for f in filled))
    return filled


def reconcile(job_id: Optional[int] = None) -> int:
    """Sync every linked proof whose job changed since it was last compared (or all of one job's)."""
    with _db() as c:
        if job_id is None:
            rows = jm_link.stale_for_sync(c)
        else:
            rows = [dict(r) for r in c.execute(
                """SELECT p.proof_key, p.path FROM job_proofs p WHERE p.job_id=? AND p.location!='archive'""", (job_id,))]
    n = 0
    for r in rows:
        if not os.path.isfile(r["path"]):
            continue
        try:
            _, rep = _sync_path(r["path"])
            n += bool(rep and rep["updated"])
        except Exception as e:
            print(f"  [PRF] sync of {os.path.basename(r['path'])} failed: {e}")
            with _db() as c:
                jm_link.set_sync(c, r["proof_key"], "error", str(e)[:200], r.get("job_updated_at"))
    return n


def job_changed(job_id: int) -> None:
    """Called by Job Manager after a job is edited: update its proofs now, off the request."""
    threading.Thread(target=lambda: reconcile(int(job_id)), daemon=True, name="proof-sync-job").start()


def rebuild_index(match_tickets: bool = False) -> Dict[str, int]:
    """Bring job_proofs in step with the proof files. Runs at startup (match_tickets=False:
    only proofs that already carry a job link) and from Settings > Link proofs to jobs
    (match_tickets=True: an unlinked proof whose job # is the ticket of exactly one job, with
    the same customer when several share the ticket, is linked to it)."""
    counts = {"scanned": 0, "indexed": 0, "linked": 0, "ambiguous": 0, "no_match": 0, "removed": 0}
    seen = set()
    now = _now()
    for folder in (JOBS_DIR, APPROVED_DIR, ARCHIVE_DIR):
        for path, sig in list_job_entries(10000, folder):
            try:
                job, _ = load_job_with_signature(str(path), sig)
            except Exception:
                continue
            counts["scanned"] += 1
            seen.add(str(path))
            write = False
            with _db() as c:
                row = _jm_row(job, c)
                if row is None and match_tickets and not job.jm_job_number:
                    cands = jm_link.jobs_for_ticket(c, job.job_number, job.customer)
                    if len(cands) == 1:
                        row = jm_link.find_job(c, cands[0]["id"])
                        write = True
                    elif len(cands) > 1:
                        counts["ambiguous"] += 1
                    else:
                        counts["no_match"] += 1
            if write:
                with SAVE_LOCK:
                    job.jm_job_id, job.jm_job_number = int(row["id"]), row["job_number"]
                    if not job.proof_id:
                        ensure_job_identity(job)
                    sync_proof(job, row)                 # fill blanks from the job, remember the baseline
                    save_job(job, str(path))
                counts["linked"] += 1
            if not job.proof_id:
                continue
            with _db() as c:
                jm_link.upsert(c, now, job, path, row)
                if write:
                    _log(c, row, f"{_proof_label(job)} linked to this job")
            counts["indexed"] += 1
    with _db() as c:
        for r in c.execute("SELECT path FROM job_proofs").fetchall():
            if r["path"] not in seen and not os.path.isfile(r["path"]):
                jm_link.forget(c, r["path"])
                counts["removed"] += 1
    return counts


def start_background() -> None:
    def run():
        try:
            startup_maintenance()
            organize_approved_jobs()
            r = rebuild_index(False)
            print(f"  [PRF] {r['scanned']} saved proofs, {r['indexed']} indexed")
        except Exception as e:
            print(f"  [PRF] proof index not rebuilt: {e}")
        while True:                                   # keep proofs in step with their jobs
            try:
                n = reconcile()
                if n:
                    print(f"  [PRF] {n} proof(s) updated from their jobs")
            except Exception as e:
                print(f"  [PRF] proof sync failed: {e}")
            time.sleep(SYNC_INTERVAL)
    threading.Thread(target=run, daemon=True, name="proof-sync").start()


def _output_dir(job: ProofJob, settings: Dict[str, Any]) -> tuple:
    """A linked proof's outputs go to the job's Proof folder; otherwise the proofing output folder."""
    row = _jm_row(job)
    folder = row.get("folder_path") if row else None
    if folder and os.path.isdir(folder):
        d = Path(folder) / "Proof"
        d.mkdir(parents=True, exist_ok=True)
        return d, row
    return resolve_output_folder(settings), row


def _job_file_url(row, path) -> Optional[str]:
    """Job Manager's own link to a file in a job folder (works from every PC, survives restarts)."""
    if not (row and path and row.get("folder_path")):
        return None
    root = row["folder_path"]
    if not _inside(path, root) or not os.path.isfile(path):
        return None
    rel = os.path.relpath(os.path.realpath(path), os.path.realpath(root)).replace("\\", "/")
    return f"/api/jobs/{row['id']}/file/" + "/".join(urllib.parse.quote(p) for p in rel.split("/"))


def _load_payload(path, job: Optional[ProofJob] = None, **extra) -> Dict[str, Any]:
    """A saved proof for the page. A linked proof is synced with its job first (unless the
    caller already has the job), so the page always opens on current job data."""
    path = Path(path)
    report = None
    if job is None:
        job, report = _sync_path(path) if _norm(path.parent) != _norm(ARCHIVE_DIR) else (load_job(str(path)), None)
    row = _jm_row(job)
    if report is None and row:
        report = sync_proof(copy.deepcopy(job), row)
    out = {"ok": True, "path": str(path), "rev": _rev(path), "job": job.as_dict(), "jm_job": _jm_public(row),
           "sync": _public_sync(report), **media_for_job(job), **extra}
    if job.last_generated_output_path and Path(job.last_generated_output_path).is_file():
        out["latest_output_url"] = register_media(job.last_generated_output_path)
    return out


def _public_sync(report) -> Optional[dict]:
    if not report:
        return None
    return {k: report[k] for k in ("updated", "differs", "locked", "state", "note", "job_number")}


# ══════════════════════════════════════════════════════════════════════════════
# Errors
# ══════════════════════════════════════════════════════════════════════════════
@bp.errorhandler(Exception)
def _api_error(e):
    if isinstance(e, HTTPException):
        if request.path.startswith("/proofing/api/"):
            return jsonify({"ok": False, "error": e.description or e.name}), e.code
        return e
    if isinstance(e, Conflict):
        return jsonify({"ok": False, "conflict": True, "error": str(e), **e.extra}), 409
    if isinstance(e, PermissionError):
        return jsonify({"ok": False, "error": str(e)}), 403
    if isinstance(e, (ValueError, OSError, BarTenderError, RuntimeError)):
        return jsonify({"ok": False, "error": str(e)}), 400
    import traceback
    print(f"  [PRF] {request.path} failed:")              # the host PC's console window
    traceback.print_exc()
    return jsonify({"ok": False, "error": f"Unexpected application error: {e}"}), 500


# ══════════════════════════════════════════════════════════════════════════════
# Page, static files, media
# ══════════════════════════════════════════════════════════════════════════════
@bp.get("/")
def page():
    resp = send_file(STATIC_DIR / "index.html", mimetype="text/html", max_age=0)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.get("/media/<token>/<path:_name>")
def media(token, _name):
    with MEDIA_LOCK:
        target = MEDIA_REGISTRY.get(token)
    if not target or not target.is_file():
        abort(404)
    inline = target.suffix.lower() in (".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp")
    resp = send_file(target, as_attachment=not inline, download_name=target.name, max_age=0,
                     mimetype=mimetypes.guess_type(target.name)[0] or "application/octet-stream")
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ══════════════════════════════════════════════════════════════════════════════
# API — same routes as the standalone app
# ══════════════════════════════════════════════════════════════════════════════
@bp.get("/api/health")
def api_health():
    return ok(app_name=APP_NAME, version=APP_VERSION, release=RELEASE_NAME, channel=RELEASE_CHANNEL,
              integrated=True)


@bp.get("/api/bootstrap")
def api_bootstrap():
    settings = load_settings()
    decorated = []
    for item in load_inlays():
        row = dict(item)
        p = _resolve_asset(str(item.get("image_path", item.get("preview_image", "")) or ""))
        if p:
            row["image_url"] = register_media(p)
        decorated.append(row)
    provider = BarTenderPreviewProvider(str(settings.get("bartender_executable", "") or ""))
    recent = [p for p in settings.get("recent_job_files", []) if Path(str(p)).is_file()][:12]
    jm = HOOKS["jm_settings"]() if "jm_settings" in HOOKS else {}
    return jsonify({
        "app_name": APP_NAME, "version": APP_VERSION, "release": RELEASE_NAME, "channel": RELEASE_CHANNEL,
        "settings": settings, "inlays": decorated, "recent_jobs": recent,
        "recent_job_details": recent_job_details(recent), "encoding_types": ENCODING_TYPES,
        "bartender": {"detected": provider.available, "executable": provider.executable,
                      "label": "Detected" if provider.available else "Not detected"},
        "integrated": True, "data_root": str(ROOT), "template_dir": jm.get("btw_template_dir", ""),
    })


@bp.post("/api/heartbeat")
def api_heartbeat():
    return ok()      # kept for old pages; the shared server never shuts itself down


@bp.post("/api/operations/overview")
def api_overview():
    limit = int(load_settings().get("work_queue_limit", 100) or 100)
    return ok(**operations_overview(limit))


@bp.post("/api/work-queue")
def api_work_queue():
    p = _payload()
    settings = load_settings()
    limit = int(p.get("limit", settings.get("work_queue_limit", 100)) or 100)
    resp = {"queue": work_queue(limit, settings), "approved": approved_queue(limit, settings)}
    if bool(p.get("include_archived", True)):
        resp["archived"] = archived_queue(limit, settings)
    with _db() as c:
        links = jm_link.by_path(c)
    for rows in resp.values():
        for r in rows:
            link = links.get(r.get("path"))
            if link and link.get("job_number"):
                r.update(jm_job_id=link["job_id"], jm_job_number=link["job_number"], jm_ticket=link.get("job_ticket"),
                         jm_job_status=link.get("job_status"), jm_sync_state=link.get("sync_state"),
                         jm_sync_note=link.get("sync_note"))
    return ok(**resp)


@bp.post("/api/job/archive")
def api_job_archive():
    p = _payload()
    with SAVE_LOCK:
        src = _job_file(p.get("path"))
        job = load_job(str(src))
        archived = archive_job(str(src))
    _forget_recent(str(src))
    with _db() as c:
        jm_link.move(c, _now(), src, archived)
        _log(c, _jm_row(job, c), f"{_proof_label(job)} archived")
    return ok(archived_path=archived)


@bp.post("/api/job/restore")
def api_job_restore():
    p = _payload()
    with SAVE_LOCK:
        src = _job_file(p.get("path"), allow_archive=True)
        restored = restore_archived_job(str(src))
    with _db() as c:
        jm_link.move(c, _now(), src, restored)
    _index(load_job(restored), restored, "proof restored from the archive")
    return ok(path=restored)


@bp.post("/api/job/delete")
def api_job_delete():
    p = _payload()
    with SAVE_LOCK:
        src = _job_file(p.get("path"), allow_archive=True)
        job = load_job(str(src))
        delete_saved_job(str(src))
    _forget_recent(str(src))
    with _db() as c:
        row = _jm_row(job, c)
        jm_link.forget(c, src)
        _log(c, row, f"{_proof_label(job)} record deleted (generated PDFs are kept)")
    return ok()


def _approval(status_override: Optional[str] = None, default_note: str = ""):
    p = _payload()
    job = ensure_job_identity(job_from_payload(p))
    settings = load_settings()
    raw = str(p.get("path", "") or "").strip()
    target_status = status_override or str(p.get("status", job.approval_status))
    saved = ""
    with SAVE_LOCK:
        target = _job_file(raw) if raw else None
        if target:
            _check_rev(target, p)
            if status_override or target_status in {"Customer Review", "Approved", "Changes Requested"}:
                try:
                    create_snapshot(job, "before_approval_reopen" if status_override else
                                    f"before_approval_{sanitize_filename(target_status, 'status')}", str(target))
                except Exception:
                    pass
        job = record_approval_event(job, target_status, str(p.get("operator", job.operator_initials)),
                                    str(p.get("note", default_note)), settings)
        if target:
            saved = route_job_by_approval(save_job(job, str(target)), job.approval_status)
    if saved:
        if saved != str(target):
            _remember_recent(saved, drop=str(target))
            with _db() as c:
                jm_link.move(c, _now(), target, saved)
        note = f" — {job.approval_note}" if job.approval_note else ""
        msg = (f"{_proof_label(job)} reopened for editing{note}" if status_override
               else f"{_proof_label(job)} set to {job.approval_status}{note}")
        row = _index(job, saved, msg)
    else:
        row = _jm_row(job)
    return ok(job=job.as_dict(), path=saved, rev=_rev(saved) if saved else "",
              locked=False if status_override else is_job_locked(job, settings), jm_job=_jm_public(row))


@bp.post("/api/approval/update")
def api_approval_update():
    return _approval()


@bp.post("/api/approval/reopen")
def api_approval_reopen():
    return _approval("Internal Review", "Reopened for editing")


@bp.post("/api/maintenance/installation-health")
def api_installation_health():
    settings = load_settings()
    checks = []

    def add(name, good, detail, critical=True):
        checks.append({"name": name, "ok": bool(good), "detail": detail, "critical": critical})

    try:
        probe = ROOT / f".write_check_{os.getpid()}_{threading.get_ident()}"
        probe.write_text("ok", encoding="utf-8"); probe.unlink()
        add("Proofing data folder", True, str(ROOT))
    except OSError as e:
        add("Proofing data folder", False, f"{ROOT}: {e}")
    provider = BarTenderPreviewProvider(str(settings.get("bartender_executable", "") or ""))
    add("BarTender on the host PC", provider.available, provider.executable or "bartend.exe not found", critical=False)
    if os.name == "nt":
        try:
            import win32com.client  # noqa: F401
            add("pywin32 (template inspection)", True, "installed", critical=False)
        except Exception as e:
            add("pywin32 (template inspection)", False, str(e), critical=False)
    zb = HOOKS.get("z_drive_base")
    add("Job folders", bool(zb and os.path.isdir(zb)), zb or "not set", critical=False)
    with _db() as c:
        n = c.execute("SELECT COUNT(*) FROM btw_templates WHERE active=1").fetchone()[0]
    add("Master Templates index", n > 0, f"{n:,} templates indexed", critical=False)
    return ok(ok_all=all(x["ok"] for x in checks if x["critical"]), checks=checks, app_root=str(ROOT),
              runtime_root=str(RUNTIME_ROOT))


@bp.post("/api/compute-epc")
def api_compute_epc():
    job = job_from_payload(_payload())
    if str(job.encoding_type or "SGTIN-96") != "SGTIN-96":
        return jsonify({"ok": False, "error": "Automatic EPC calculation is currently available for SGTIN-96. "
                                              f"Enter the approved EPC/data for {job.encoding_type}."}), 400
    result = encode_sgtin96(job.gtin, job.company_prefix_digits, job.serial, job.filter_value)
    cp_bits, item_bits, cp_digits, item_digits = PARTITIONS[result.partition]
    inspector = {
        "header_hex": "30", "header_bits": result.binary[:8],
        "filter": int(job.filter_value), "filter_bits": result.binary[8:11],
        "partition": result.partition, "partition_bits": result.binary[11:14],
        "company_prefix": result.company_prefix, "company_prefix_bits": cp_bits, "company_prefix_digits": cp_digits,
        "item_reference": result.item_reference, "item_reference_bits": item_bits, "item_reference_digits": item_digits,
        "serial": result.serial, "serial_bits": 38, "total_bits": 96,
    }
    return ok(**asdict(result), inspector=inspector)


@bp.post("/api/validate")
def api_validate():
    p = _payload()
    return jsonify(validation_payload(job_from_payload(p), bool(p.get("require_preview", True))))


@bp.post("/api/inlays/sync-folder")
def api_inlays_sync():
    p = _payload()
    settings = load_settings()
    folder = str(p.get("folder", settings.get("inlay_source_folder", "")) or "").strip()
    items, summary = sync_inlays_from_folder(folder, load_inlays())
    save_inlays(items)
    settings["inlay_source_folder"] = folder
    settings["inlay_last_sync"] = datetime.now().isoformat(timespec="seconds")
    save_settings(settings)
    return ok(inlays=items, summary=summary, folder=folder)


@bp.post("/api/job/save")
def api_job_save():
    p = _payload()
    job = ensure_job_identity(job_from_payload(p))
    raw = str(p.get("path", "") or "").strip()
    settings = load_settings()
    created = False
    with SAVE_LOCK:
        if raw:
            target = _job_file(raw, must_exist=False)
            _check_rev(target, p)
            if bool(settings.get("auto_recovery_snapshots", True)):
                try:
                    existing = load_job(str(target))
                    if release_fingerprint(existing, settings) != release_fingerprint(job, settings):
                        minutes = max(1, int(settings.get("auto_recovery_snapshot_minutes", 5) or 5))
                        recent_auto = [x for x in list_snapshots(existing, 20)
                                       if str(x.get("reason", "")) == "auto_recovery_before_save"]
                        allow = True
                        if recent_auto:
                            try:
                                last = datetime.fromisoformat(str(recent_auto[0].get("created_at", "")))
                                allow = (datetime.now() - last).total_seconds() >= minutes * 60
                            except Exception:
                                pass
                        if allow:
                            create_snapshot(existing, "auto_recovery_before_save", str(target))
                except Exception:
                    pass
        else:
            name = "_".join(filter(None, [sanitize_filename(job.customer, "Customer"),
                                          sanitize_filename(job.job_number, "Job"),
                                          f"P{sanitize_filename(job.proof_revision, '1')}"]))
            target = _unique_new_path(JOBS_DIR, name)
            created = True
        _adopt_preview(job)
        row, report, filled = _jm_row(job), None, []
        if row:
            report = sync_proof(job, row)               # job values the page didn't have yet
            filled = backfill_job(job, row)             # blank job fields from the proof
        saved = route_job_by_approval(save_job(job, str(target)), job.approval_status)
        rev = _rev(saved)
    recent, details = _remember_recent(saved, drop=raw)
    if raw and saved != raw:
        with _db() as c:
            jm_link.move(c, _now(), raw, saved)
    row = _index(job, saved, f"{_proof_label(job)} started" if created else None)
    if report:
        with _db() as c:
            jm_link.set_sync(c, jm_link.proof_key(job.proof_id), report["state"], report["note"],
                             (_jm_row(job, c) or {}).get("updated_at"))
    return ok(path=saved, rev=rev, job=job.as_dict(), recent_jobs=recent, recent_job_details=details,
              jm_job=_jm_public(row), sync=_public_sync(report), filled_job=filled, **media_for_job(job))


@bp.post("/api/job/load")
def api_job_load():
    p = _payload()
    return jsonify(_load_payload(_job_file(p.get("path"), allow_archive=True)))


@bp.post("/api/maintenance/backup")
def api_backup():
    generated = create_backup_bundle()
    return ok(path=generated, url=register_media(generated))


@bp.post("/api/next-revision")
def api_next_revision():
    p = _payload()
    value = next_revision(str(p.get("value", "")))
    proof_id = proof_id_for_revision(str(p.get("proof_id", "")), value) if p.get("proof_id") else ""
    return jsonify({"value": value, "proof_id": proof_id})


# ── BarTender preview ─────────────────────────────────────────────────────────
def _set_preview_task(task_id: str, **updates) -> None:
    with PREVIEW_TASKS_LOCK:
        task = PREVIEW_TASKS.setdefault(task_id, {})
        task.update(updates)
        task["updated_at"] = time.time()


def _get_preview_task(task_id: str) -> Dict[str, Any]:
    now = time.time()
    with PREVIEW_TASKS_LOCK:
        for key in [k for k, row in PREVIEW_TASKS.items() if now - float(row.get("updated_at", now)) > 900]:
            PREVIEW_TASKS.pop(key, None)
        return dict(PREVIEW_TASKS.get(task_id, {}))


def _render_preview(job: ProofJob, on_start=None) -> ProofJob:
    """Render Record 1 / Page 1 in a folder of its own, one render at a time, and keep the
    image in the data folder."""
    settings = load_settings()
    provider = BarTenderPreviewProvider(str(settings.get("bartender_executable", "") or ""))
    work = BARTENDER_CACHE_DIR / f"task_{secrets.token_hex(6)}"
    try:
        with BARTENDER_LOCK:
            if on_start:
                on_start()
            preview = provider.render_preview(job.template_path, str(work),
                                              named_data_for_job(job, _bartender_mapping(job, settings)),
                                              int(settings.get("preview_dpi", 300)))
            kept = _store_preview(Path(preview), job, "bartender")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    job.preview_path = str(kept)
    stamp_preview(job, "BarTender Record 1 / Page 1")
    return job


def _run_bartender_preview_task(task_id: str, job_payload: Dict[str, Any]) -> None:
    try:
        job = ProofJob.from_dict(job_payload)
        if BARTENDER_LOCK.locked():
            _set_preview_task(task_id, status="waiting")
        _preview_log(f"START {task_id} | {job.template_path}")
        job = _render_preview(job, on_start=lambda: _set_preview_task(task_id, status="running"))
        _set_preview_task(task_id, status="done", path=job.preview_path, url=register_media(job.preview_path),
                          job=job.as_dict(), integrity=preview_integrity(job))
        _preview_log(f"DONE {task_id} | {job.preview_path}")
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        _set_preview_task(task_id, status="error", error=message)
        _preview_log(f"ERROR {task_id} | {message}")


@bp.post("/api/bartender/preview/start")
def api_preview_start():
    job = job_from_payload(_payload())
    _check_template(job)
    task_id = secrets.token_hex(12)
    _set_preview_task(task_id, status="waiting" if BARTENDER_LOCK.locked() else "queued", created_at=time.time())
    threading.Thread(target=_run_bartender_preview_task, args=(task_id, job.as_dict()), daemon=True,
                     name="bartender-preview").start()
    return jsonify({"ok": True, "task_id": task_id, "status": "queued"}), 202


@bp.post("/api/bartender/preview/status")
def api_preview_status():
    task_id = str(_payload().get("task_id", "") or "").strip()
    if not task_id:
        raise ValueError("Preview task ID is required.")
    task = _get_preview_task(task_id)
    if not task:
        return jsonify({"ok": False, "error": "BarTender preview task was not found or expired."}), 404
    return ok(**task)


@bp.post("/api/bartender/preview")
def api_preview_sync():
    job = job_from_payload(_payload())
    _check_template(job)
    job = _render_preview(job)
    return ok(path=job.preview_path, url=register_media(job.preview_path), job=job.as_dict(),
              integrity=preview_integrity(job))


@bp.post("/api/bartender/diagnostics")
def api_bartender_diagnostics():
    settings = load_settings()
    diag = asdict(BarTenderPreviewProvider(str(settings.get("bartender_executable", "") or "")).diagnose())
    return ok(**diag, available=diag["executable_found"], message=diag["detail"])


@bp.post("/api/settings/save")
def api_settings_save():
    updates = _payload().get("settings", {})
    if not isinstance(updates, dict):
        raise ValueError("Settings payload must be an object.")
    updates.pop("recent_job_files", None)
    exe = str(updates.get("bartender_executable") or "").strip()
    if exe:
        # This program is run on the host PC, so only BarTender itself is accepted.
        if Path(exe).name.lower() != "bartend.exe":
            raise ValueError("The BarTender executable must be bartend.exe.")
        if not Path(exe).is_file():
            raise FileNotFoundError(f"bartend.exe was not found at that path on the host PC: {exe}")
    settings = load_settings()
    settings.update(updates)
    save_settings(settings)
    return ok(settings=load_settings())


# ── output ────────────────────────────────────────────────────────────────────
def _generate(kind: str):
    p = _payload()
    job = ensure_job_identity(job_from_payload(p))
    settings = load_settings()
    proof_settings = dict(settings)
    proof_settings["master_company"] = job.preparing_company or settings.get("master_company", "MCC Label")
    review = proof_review_payload(job, proof_settings, require_preview=True)
    if not review["validation"]["ready"]:
        return jsonify({"ok": False, "validation": review["validation"]}), 409
    if bool(settings.get("revision_guard", True)) and review["same_revision_changed"] and not bool(p.get("allow_same_revision_changes", False)):
        return jsonify({"ok": False, "revision_guard": True, **review}), 409
    out_dir, row = _output_dir(job, settings)
    base = f"{sanitize_filename(job.customer, 'Customer')}_{sanitize_filename(job.job_number, 'Job')}_P{sanitize_filename(job.proof_revision, '1')}"
    pdf_path = out_dir / f"{base}_RFID_Proof.pdf"
    zip_path = out_dir / f"{base}_Proof_Package.zip"
    archived = []
    if bool(settings.get("preserve_generated_versions", True)):
        for target in ((pdf_path, zip_path) if kind == "package" else (pdf_path,)):
            prior = archive_existing_output(target)
            if prior:
                archived.append(prior)
    job.lifecycle_status = "generated"
    job.last_generated_at = datetime.now().isoformat(timespec="seconds")
    generated = generate_proof_pdf(job, str(pdf_path), proof_settings)
    if kind == "package":
        generated = export_proof_package(job, str(pdf_path), str(zip_path), proof_settings, False)
    job.last_generated_proof_revision = str(job.proof_revision or "")
    job.last_generated_master_company = str(proof_settings.get("master_company", "MCC Label") or "MCC Label")
    job.last_generated_snapshot = proof_review_snapshot(job, proof_settings)
    job.last_generated_output_path = str(generated)
    job.last_generated_output_kind = "Package" if kind == "package" else "PDF"
    job.last_generated_fingerprint = release_fingerprint(job, proof_settings)
    if bool(settings.get("beta_snapshot_on_generation", True)):
        create_snapshot(job, "generated_package" if kind == "package" else "generated_pdf", output_path=str(generated))
    record_proof_history(job, str(pdf_path), action="PACKAGE" if kind == "package" else "PDF",
                         package_path=str(generated) if kind == "package" else None)
    with _db() as c:
        _log(c, row, f"{_proof_label(job)} {'customer package' if kind == 'package' else 'PDF'} written to "
                     f"{'the Proof folder' if row and _inside(generated, row.get('folder_path')) else 'the proofing output folder'}"
                     f" ({Path(generated).name})")
    resp = {"ok": True, "path": str(generated), "url": register_media(generated), "archived_previous": archived,
            "job": job.as_dict(), "job_file_url": _job_file_url(row, generated)}
    if kind == "package":
        resp["pdf_url"] = register_media(pdf_path)
    return jsonify(resp)


@bp.post("/api/proof/pdf")
def api_proof_pdf():
    return _generate("pdf")


@bp.post("/api/proof/package")
def api_proof_package():
    return _generate("package")


@bp.post("/api/output/link")
def api_output_link():
    """A download/view link for a generated proof, only inside the proofing data folder,
    the proof output folder or the job folders."""
    raw = str(_payload().get("path", "") or "").strip()
    p = Path(raw)
    if not raw or not p.is_file() or p.suffix.lower() not in (".pdf", ".zip", ".png", ".jpg", ".jpeg"):
        raise FileNotFoundError("That output file is not there any more.")
    if not _inside(p, ROOT, resolve_output_folder(load_settings()), HOOKS.get("z_drive_base")):
        raise PermissionError("That file is outside the proof folders.")
    return ok(url=register_media(p), path=str(p))


@bp.post("/api/output/folder")
def api_output_folder():
    """Where this proof's outputs go: the job's Proof folder, or the proofing output folder."""
    p = _payload()
    job = job_from_payload(p) if p.get("job") else ProofJob()
    folder, row = _output_dir(job, load_settings())
    return ok(path=str(folder), jm_job=_jm_public(row), in_job_folder=bool(row and _inside(folder, row.get("folder_path"))))


# ── templates from Job Manager's Master Templates index ───────────────────────
@bp.post("/api/templates/search")
def api_templates_search():
    p = _payload()
    q = str(p.get("q", "") or "").strip()
    job = job_from_payload({"job": p.get("job") or {}})
    sql, params = "SELECT id, name, file_path FROM btw_templates WHERE active=1", []
    for w in re.split(r"[\s\-_]+", q):
        if w:
            sql += " AND (name || ' ' || file_path) LIKE ?"
            params.append(f"%{w}%")
    sql += " ORDER BY name LIMIT 40"
    with _db() as c:
        rows = [dict(r) for r in c.execute(sql, params)]
        total = c.execute("SELECT COUNT(*) FROM btw_templates WHERE active=1").fetchone()[0]
        jm = _jm_row(job, c)
    for r in rows:
        r["file_name"] = re.split(r"[\\/]", r["file_path"])[-1]
    suggestions, seen = [], set()

    def suggest(path, name, reasons):
        if path and path.lower() not in seen:
            seen.add(path.lower())
            suggestions.append({"file_path": path, "file_name": name, "reasons": reasons, "exists": os.path.isfile(path)})

    if jm and jm.get("template_path"):
        suggest(jm["template_path"], re.split(r"[\\/]", jm["template_path"])[-1], [f"In {jm['job_number']}'s Print folder"])
    if "match_templates" in HOOKS and not q:
        gtin = digits_only(job.gtin)
        upc = gtin[-12:] if len(gtin) >= 12 else ""
        size = f"{job.label_width:g} x {job.label_height:g}" if job.label_width and job.label_height else ""
        try:
            m = HOOKS["match_templates"](upc, job.customer, size, job.part_number)
            for cand in m["candidates"][:6]:
                suggest(cand["file_path"], cand["file_name"], cand["reasons"])
        except Exception:
            pass
    return ok(results=rows, suggestions=suggestions, total=total)


@bp.post("/api/templates/choose")
def api_templates_choose():
    raw = str(_payload().get("path", "") or "").strip()
    if Path(raw).suffix.lower() != ".btw":
        raise ValueError("Choose a BarTender .btw template.")
    if not os.path.isfile(raw):
        raise FileNotFoundError(f"The host PC can't reach that template: {raw}")
    return ok(path=raw, profile=find_template_profile(load_template_profiles(), raw))


# ── preview image upload (replaces the host-PC file picker) ───────────────────
@bp.post("/api/preview/upload")
def api_preview_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        raise ValueError("Choose an image.")
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        raise ValueError("Use a PNG, JPG or WEBP image.")
    data = f.read()
    if len(data) > 25 * 1024 * 1024:
        raise ValueError("The image is larger than 25 MB.")
    try:
        job = job_from_payload({"job": json.loads(request.form.get("job") or "{}")})
    except json.JSONDecodeError:
        job = ProofJob()
    tmp = BARTENDER_CACHE_DIR / f"upload_{secrets.token_hex(6)}{ext}"
    tmp.write_bytes(data)
    try:
        from PIL import Image
        with Image.open(tmp) as im:
            im.verify()                       # a real image, not just a renamed file
        kept = _store_preview(tmp, job, "manual_" + sanitize_filename(os.path.splitext(f.filename)[0], "image")[:40])
    except Exception as e:
        raise ValueError(f"That file couldn't be read as an image: {e}")
    finally:
        tmp.unlink(missing_ok=True)
    job.preview_path = str(kept)
    stamp_preview(job, "Manual preview")
    return ok(path=str(kept), url=register_media(kept), job=job.as_dict())


# ══════════════════════════════════════════════════════════════════════════════
# API — Job Manager link
# ══════════════════════════════════════════════════════════════════════════════
def _proof_rows_for(job_row) -> list:
    with _db() as c:
        rows = jm_link.for_job(c, job_row["id"])
    out = []
    for r in rows:
        exists = os.path.isfile(r["path"])
        out.append({**{k: r[k] for k in ("proof_key", "proof_id", "proof_revision", "approval_status",
                                         "lifecycle_status", "last_generated_at", "location", "updated_at",
                                         "output_path", "path", "sync_state", "sync_note")},
                    "exists": exists, "open_url": f"/proofing/?proof={r['proof_key']}",
                    "output_url": _job_file_url(job_row, r["output_path"]),
                    "output_name": os.path.basename(r["output_path"] or "") or None})
    return out


@bp.get("/api/jm/jobs/<int:job_id>/proofs")
def api_jm_job_proofs(job_id):
    with _db() as c:
        row = jm_link.find_job(c, job_id)
    if not row:
        abort(404, "Job not found")
    return ok(proofs=_proof_rows_for(row), start_url=f"/proofing/?job={job_id}")


@bp.post("/api/jm/jobs/<int:job_id>/open")
def api_jm_open(job_id):
    """The job's current proof, or a new one filled from the job ({"new": true} always
    makes a new one, e.g. a second label on the same ticket)."""
    p = _payload()
    with _db() as c:
        row = jm_link.find_job(c, job_id)
    if not row:
        abort(404, "Job not found")
    with SAVE_LOCK:              # two people pressing Start proof at once get one proof
        if not p.get("new"):
            current = [r for r in _proof_rows_for(row) if r["exists"] and r["location"] != "archive"]
            if current:
                return jsonify(_load_payload(current[0]["path"]))
        job, notes = proof_from_jm_job(row)
        job = ensure_job_identity(job)
        stem = "_".join(filter(None, [sanitize_filename(job.customer, "Customer"),
                                      sanitize_filename(job.job_number, "Job"), "P1"]))
        saved = save_job(job, str(_unique_new_path(JOBS_DIR, stem)))
        _index(job, saved, f"{_proof_label(job)} started from this job")
    _remember_recent(saved)
    return jsonify(_load_payload(saved, job, created=True, notes=notes))


@bp.get("/api/jm/proof/<key>")
def api_jm_proof_by_key(key):
    with _db() as c:
        r = c.execute("SELECT path FROM job_proofs WHERE proof_key=?", (key.upper(),)).fetchone()
    if not r:
        abort(404, "That proof isn't in the index. Open it from the Queue.")
    return jsonify(_load_payload(_job_file(r["path"], allow_archive=True)))


@bp.post("/api/jm/find-jobs")
def api_jm_find_jobs():
    q = str(_payload().get("q", "") or "").strip()
    if not q:
        return ok(jobs=[])
    like = f"%{q}%"
    with _db() as c:
        rows = [dict(r) for r in c.execute(
            """SELECT id, job_number, job_ticket, customer, title, status, upc, archived_at FROM jobs
               WHERE status!='Cancelled' AND (job_ticket LIKE ? OR job_number LIKE ? OR customer LIKE ? OR upc LIKE ?)
               ORDER BY (archived_at IS NULL) DESC, id DESC LIMIT 20""", (like, like, like, like))]
    return ok(jobs=rows)


@bp.post("/api/jm/link")
def api_jm_link():
    """Link a saved proof to a job ({"job_id": 0} unlinks)."""
    p = _payload()
    job_id = int(p.get("job_id") or 0)
    with SAVE_LOCK:
        target = _job_file(p.get("path"), allow_archive=True)
        _check_rev(target, p)
        job = load_job(str(target))
        before = _jm_row(job)
        with _db() as c:
            row = jm_link.find_job(c, job_id) if job_id else None
        if job_id and not row:
            abort(404, "Job not found")
        job.jm_job_id, job.jm_job_number = (int(row["id"]), row["job_number"]) if row else (0, "")
        if row:
            sync_proof(job, row)
        else:
            job.jm_sync = {}
        ensure_job_identity(job)
        saved = save_job(job, str(target))
    with _db() as c:
        jm_link.upsert(c, _now(), job, saved, row)
        if before and (not row or before["id"] != row["id"]):
            _log(c, before, f"{_proof_label(job)} unlinked from this job")
        if row:
            _log(c, row, f"{_proof_label(job)} linked to this job")
    return jsonify(_load_payload(saved, job))


@bp.post("/api/jm/relink")
def api_jm_relink():
    return ok(**rebuild_index(match_tickets=True))


@bp.post("/api/jm/jobs")
def api_jm_jobs():
    """Job Manager jobs for the Proofing home page: open jobs with their proof state.
    show=needs (default): no proof yet, not approved yet, or approved before the job changed."""
    p = _payload()
    q, show = str(p.get("q", "") or "").strip(), p.get("show", "needs")
    where, params = ["j.archived_at IS NULL", "j.status != 'Cancelled'"], []
    for w in q.split():
        where.append("(j.job_ticket LIKE ? OR j.job_number LIKE ? OR j.customer LIKE ? OR j.title LIKE ? OR j.upc LIKE ?)")
        params += [f"%{w}%"] * 5
    sql = f"""SELECT * FROM (SELECT j.id, j.job_number, j.job_ticket, j.customer, j.title, j.status, j.upc, j.label_size,
                  j.quantity, j.due_date, j.created_at, j.job_type,
                  (SELECT p.approval_status FROM job_proofs p WHERE p.job_id=j.id AND p.location!='archive'
                     ORDER BY p.updated_at DESC LIMIT 1) AS proof_status,
                  (SELECT p.sync_state FROM job_proofs p WHERE p.job_id=j.id AND p.location!='archive'
                     ORDER BY p.updated_at DESC LIMIT 1) AS proof_sync,
                  (SELECT p.sync_note FROM job_proofs p WHERE p.job_id=j.id AND p.location!='archive'
                     ORDER BY p.updated_at DESC LIMIT 1) AS proof_sync_note,
                  (SELECT p.proof_key FROM job_proofs p WHERE p.job_id=j.id AND p.location!='archive'
                     ORDER BY p.updated_at DESC LIMIT 1) AS proof_key
              FROM jobs j WHERE {' AND '.join(where)}) x"""
    if show == "needs":
        sql += " WHERE proof_status IS NULL OR proof_status != 'Approved' OR proof_sync = 'approved_changed'"
    sql += " ORDER BY (proof_status IS NOT NULL), created_at DESC LIMIT 200"
    with _db() as c:
        rows = [dict(r) for r in c.execute(sql, params)]
        counts = c.execute("""SELECT COUNT(*) FROM jobs j WHERE j.archived_at IS NULL AND j.status != 'Cancelled'
                              AND NOT EXISTS (SELECT 1 FROM job_proofs p WHERE p.job_id=j.id AND p.location!='archive')""").fetchone()[0]
    return ok(jobs=rows, without_proof=counts)


@bp.post("/api/job/rev")
def api_job_rev():
    """Cheap check the page polls: has this saved proof changed on the server (a job sync, or
    someone else's save)?"""
    raw = str(_payload().get("path", "") or "").strip()
    try:
        target = _job_file(raw, allow_archive=True)
    except FileNotFoundError:
        return ok(rev="", exists=False)
    meta, sync = {}, None
    try:
        job = load_job(str(target))
        meta = {k: v for k, v in (job.jm_sync or {}).items() if k.startswith("_")}
        row = _jm_row(job)
        if row:                      # what differs from the job right now (read only; nothing is applied)
            sync = _public_sync({**sync_proof(copy.deepcopy(job), row), "updated": []})
    except Exception:
        pass
    return ok(rev=_rev(target), exists=True, last_sync=meta, sync=sync)


@bp.post("/api/jm/use-job-values")
def api_jm_use_job_values():
    """Replace the listed fields (or every differing field) with the job's values."""
    p = _payload()
    with SAVE_LOCK:
        target = _job_file(p.get("path"))
        _check_rev(target, p)
        job = load_job(str(target))
        if is_job_locked(job):
            raise ValueError("This proof is approved. Reopen it (or start the next proof revision) to take the job's values.")
        row = _jm_row(job)
        if not row:
            raise ValueError("This proof isn't linked to a Job Manager job.")
        jv, wanted = job_values(row), set(p.get("fields") or SYNC_LABELS)
        taken = []
        for f in [f for f, _ in SYNC_FIELDS if f in wanted and f in jv]:
            if _key(f, proof_values(job).get(f)) != _key(f, jv[f]):
                _apply(job, f, jv[f])
                taken.append(f)
            job.jm_sync = {**(job.jm_sync or {}), f: jv[f]}
        _fix_encoding(job, set(taken))
        taken = [SYNC_LABELS[f] for f in taken]
        saved = save_job(job, str(target))
    if taken:
        with _db() as c:
            _log(c, row, f"{_proof_label(job)}: took the job's {', '.join(taken)}")
    return jsonify(_load_payload(saved))

