from __future__ import annotations

import csv
from copy import deepcopy
from datetime import datetime
import threading
from typing import Dict, List, Optional

from .models import ProofJob
from .settings import ROOT
from .storage import file_signature

HISTORY_PATH = ROOT / "logs" / "proof_history.csv"
HISTORY_FIELDS = [
    "timestamp", "action", "proof_id", "customer", "job_number", "part_number", "label_revision",
    "proof_revision", "operator_initials", "gtin", "inlay", "epc_hex", "lifecycle_status", "output_path", "package_path",
]
_HISTORY_LOCK = threading.RLock()
_HISTORY_CACHE: tuple[tuple[int, int], List[Dict[str, str]]] | None = None


def _invalidate_history_cache() -> None:
    global _HISTORY_CACHE
    with _HISTORY_LOCK:
        _HISTORY_CACHE = None


def _read_existing_rows() -> List[Dict[str, str]]:
    global _HISTORY_CACHE
    if not HISTORY_PATH.is_file():
        return []
    try:
        signature = file_signature(HISTORY_PATH)
    except OSError:
        return []
    with _HISTORY_LOCK:
        if _HISTORY_CACHE and _HISTORY_CACHE[0] == signature:
            return deepcopy(_HISTORY_CACHE[1])
    try:
        with HISTORY_PATH.open("r", newline="", encoding="utf-8-sig") as fh:
            rows = [dict(row) for row in csv.DictReader(fh)]
    except (OSError, UnicodeError, csv.Error):
        return []
    with _HISTORY_LOCK:
        _HISTORY_CACHE = (signature, deepcopy(rows))
    return rows


def _ensure_history_schema() -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not HISTORY_PATH.exists():
        return
    try:
        with HISTORY_PATH.open("r", newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            header = next(reader, [])
    except (OSError, UnicodeError, csv.Error):
        return
    if header == HISTORY_FIELDS:
        return
    rows = _read_existing_rows()
    with _HISTORY_LOCK:
        with HISTORY_PATH.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=HISTORY_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in HISTORY_FIELDS})
        _invalidate_history_cache()


def record_proof_history(job: ProofJob, output_path: str, action: str = "PDF", package_path: Optional[str] = None) -> None:
    _ensure_history_schema()
    with _HISTORY_LOCK:
        exists = HISTORY_PATH.exists()
        with HISTORY_PATH.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=HISTORY_FIELDS)
            if not exists or HISTORY_PATH.stat().st_size == 0:
                writer.writeheader()
            writer.writerow({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "action": action,
                "proof_id": job.proof_id,
                "customer": job.customer,
                "job_number": job.job_number,
                "part_number": job.part_number,
                "label_revision": job.revision,
                "proof_revision": job.proof_revision,
                "operator_initials": job.operator_initials,
                "gtin": job.gtin,
                "inlay": job.inlay,
                "epc_hex": job.epc_hex,
                "lifecycle_status": job.lifecycle_status,
                "output_path": output_path,
                "package_path": package_path or "",
            })
        _invalidate_history_cache()


def load_proof_history(limit: int = 200) -> List[Dict[str, str]]:
    _ensure_history_schema()
    rows = _read_existing_rows()
    rows.reverse()
    return rows[: max(1, int(limit or 200))]
