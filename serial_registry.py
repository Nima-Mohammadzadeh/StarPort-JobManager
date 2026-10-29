"""
Serial Number Registry
======================
Guarantees non-overlapping, non-reusable serial number ranges per UPC.

Concurrency model
-----------------
TWO layers of locking for full reliability:

  1. threading.Lock (in-process)
     Protects against concurrent requests within a single Flask server.

  2. File-based lock with os.O_EXCL (cross-process / cross-machine)
     Protects against concurrent allocation when multiple workstations
     each run their own app.py against a shared registry directory.
     Uses an atomic ".lock" sentinel file. Works on:
         - Mac / Linux (POSIX)
         - Windows (NTFS)
         - SMB / NFS network drives

This means the same code path works seamlessly whether you're:
  - Testing locally on a Mac at home (single process)
  - Running production on a Windows host (single process, many browsers)
  - Future: multiple workstations each running their own app.py
    against a shared Z: drive (multiple processes, cross-machine)

Storage
-------
One UTF-8 text file per UPC in `serial_registry/{upc}.txt`.
Each allocation appends one line — file is a permanent human-readable audit log.

Lock files
----------
Created at `serial_registry/{upc}.lock` during allocation and deleted
immediately after. Stale locks (>30s old) are automatically broken to
prevent zombie locks from crashed processes.
"""

import os
import time
import socket
import threading
from datetime import datetime

# ── Constants ─────────────────────────────────────────────────────────────────
SERIAL_START_DEFAULT = 26_000_000_001
LOCK_TIMEOUT_SECONDS = 10        # max time to wait for a peer to release a lock
STALE_LOCK_SECONDS   = 30        # break locks held longer than this (crash recovery)

# ── Registry directory — loaded from config.py ────────────────────────────────
try:
    from config import SERIAL_REGISTRY_DIR as REGISTRY_DIR
except ImportError:
    REGISTRY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'serial_registry')

# ── Per-UPC threading locks (in-process only) ─────────────────────────────────
_thread_locks_mu = threading.Lock()
_thread_locks: dict = {}


def _thread_lock_for(upc: str) -> threading.Lock:
    with _thread_locks_mu:
        if upc not in _thread_locks:
            _thread_locks[upc] = threading.Lock()
        return _thread_locks[upc]


# ── File paths ────────────────────────────────────────────────────────────────
def _registry_path(upc: str) -> str:
    return os.path.join(REGISTRY_DIR, f"{upc}.txt")


def _lock_path(upc: str) -> str:
    return os.path.join(REGISTRY_DIR, f"{upc}.lock")


# ── Cross-process file lock ───────────────────────────────────────────────────
class FileLockTimeout(Exception):
    """Raised when a file lock could not be acquired within the timeout."""


def _acquire_file_lock(upc: str, timeout: float = LOCK_TIMEOUT_SECONDS) -> str:
    """
    Acquire a cross-process lock for a UPC by atomically creating a sentinel file.
    Uses os.O_EXCL which is atomic on every major filesystem (NTFS, ext4,
    APFS, SMB, NFS). Returns the lock file path on success.

    If another process holds the lock, polls every 50ms until released or
    timeout expires. Auto-breaks stale locks older than STALE_LOCK_SECONDS.
    """
    os.makedirs(REGISTRY_DIR, exist_ok=True)
    lock_path  = _lock_path(upc)
    started_at = time.time()

    host_id = f"{socket.gethostname()}:pid={os.getpid()}"

    while True:
        try:
            # Atomic exclusive create — fails if file exists
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, f"{host_id} acquired={time.time()}\n".encode())
            finally:
                os.close(fd)
            return lock_path

        except FileExistsError:
            # Someone else holds it — check if stale
            try:
                age = time.time() - os.path.getmtime(lock_path)
                if age > STALE_LOCK_SECONDS:
                    # Break the stale lock — owner likely crashed
                    try:
                        os.unlink(lock_path)
                    except OSError:
                        pass
                    continue
            except OSError:
                # Lock file vanished between checks — race, try again immediately
                continue

            if time.time() - started_at > timeout:
                raise FileLockTimeout(
                    f"Could not acquire lock for UPC {upc} within {timeout}s. "
                    f"Lock held by another process at {lock_path}."
                )
            time.sleep(0.05)


def _release_file_lock(lock_path: str) -> None:
    """Release a file lock by deleting the sentinel file. Idempotent."""
    try:
        os.unlink(lock_path)
    except OSError:
        pass


# ── Registry I/O ──────────────────────────────────────────────────────────────
def _read_last_end(upc: str) -> int | None:
    path = _registry_path(upc)
    if not os.path.isfile(path):
        return None
    last_end = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 4:
                try:
                    end = int(parts[3])
                    last_end = end if last_end is None else max(last_end, end)
                except ValueError:
                    pass
    return last_end


def _append_allocation(upc: str, job_number: str,
                       start: int, end: int, qty: int,
                       overage_note: str = "base") -> None:
    os.makedirs(REGISTRY_DIR, exist_ok=True)
    path  = _registry_path(upc)
    now   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    is_new = not os.path.isfile(path)
    with open(path, "a", encoding="utf-8") as f:
        if is_new:
            f.write(f"# UPC Serial Registry: {upc}\n")
            f.write(f"# Initialized: {now}\n")
            f.write(f"# Format: TIMESTAMP | JOB_NUMBER | START | END | QTY | NOTE\n")
            f.write("#\n")
        f.write(f"{now} | {job_number} | {start} | {end} | {qty} | {overage_note}\n")


# ── Public API ────────────────────────────────────────────────────────────────
def allocate_serials(upc: str, job_number: str, adjusted_qty: int,
                     overage_note: str = "base", min_start: int | None = None,
                     skip_reason: str = "start set by user") -> dict:
    """
    Atomically allocate `adjusted_qty` serial numbers for `upc`.
    Safe across threads, processes, and machines sharing the same registry.

    min_start: optional. Skip forward to this serial (for a UPC that ran before the
    registry existed). It can only move forward: a start below the next free serial
    raises ValueError, so used serials can never be handed out twice.

    Returns: {start, end, qty, upc, job_number, is_new_upc, skipped}
    Raises: ValueError on bad input, FileLockTimeout if peer locked >10s,
            RuntimeError on I/O errors.
    """
    upc = upc.strip()
    if not upc or not upc.isdigit() or len(upc) != 12:
        raise ValueError(f"UPC must be exactly 12 digits, got: {upc!r}")
    if adjusted_qty <= 0:
        raise ValueError(f"Quantity must be positive, got: {adjusted_qty}")

    # Layer 1: in-process thread lock (cheap, prevents same-process races)
    with _thread_lock_for(upc):
        # Layer 2: cross-process file lock (expensive, prevents cross-machine races)
        lock_path = _acquire_file_lock(upc)
        try:
            last_end = _read_last_end(upc)
            is_new   = (last_end is None)
            natural  = SERIAL_START_DEFAULT if is_new else (last_end + 1)
            start    = natural
            if min_start is not None:
                if min_start < natural:
                    raise ValueError(f"Start serial {min_start:,} would reuse serials already used for "
                                     f"this UPC. The next free serial is {natural:,}.")
                start = min_start
            end      = start + adjusted_qty - 1
            if end >= 1 << 38:
                raise ValueError("That range runs past the largest serial an EPC can hold.")
            if start > natural:
                overage_note = f"{overage_note}; {skip_reason}, skipped {start - natural:,}"

            try:
                _append_allocation(upc, job_number, start, end, adjusted_qty, overage_note)
            except Exception as e:
                raise RuntimeError(f"Failed to write serial registry for UPC {upc}: {e}") from e

            return {
                "start":      start,
                "end":        end,
                "qty":        adjusted_qty,
                "upc":        upc,
                "job_number": job_number,
                "is_new_upc": is_new,
                "skipped":    start - natural,
            }
        finally:
            _release_file_lock(lock_path)


def seed_last_used(upc: str, last_used: int, note: str = "ran before this app") -> dict:
    """Record that serials up to `last_used` were already used before the registry
    existed. Forward-only: does nothing unless it moves the next serial forward."""
    upc = upc.strip()
    if not upc.isdigit() or len(upc) != 12:
        raise ValueError(f"UPC must be exactly 12 digits, got: {upc!r}")
    with _thread_lock_for(upc):
        lock_path = _acquire_file_lock(upc)
        try:
            last_end = _read_last_end(upc)
            floor = SERIAL_START_DEFAULT - 1 if last_end is None else last_end
            if last_used <= floor:
                return {"changed": False, "next_start": floor + 1}
            _append_allocation(upc, "HISTORY", floor + 1, last_used, 0, note)
            return {"changed": True, "next_start": last_used + 1}
        finally:
            _release_file_lock(lock_path)


def peek_next_start(upc: str) -> int:
    """Read-only preview — no locks needed since no write occurs."""
    upc = upc.strip()
    last_end = _read_last_end(upc)
    return SERIAL_START_DEFAULT if last_end is None else (last_end + 1)


def get_registry_summary() -> list[dict]:
    """Summary of every UPC for the /api/serial-registry endpoint."""
    os.makedirs(REGISTRY_DIR, exist_ok=True)
    summary = []
    for fname in sorted(os.listdir(REGISTRY_DIR)):
        if not fname.endswith(".txt"):
            continue
        upc  = fname[:-4]
        path = os.path.join(REGISTRY_DIR, fname)
        records, total_qty, last_end, initialized = [], 0, None, None
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("# Initialized:"):
                    initialized = line.replace("# Initialized:", "").strip(); continue
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 5:
                    try:
                        rec = {"timestamp": parts[0], "job_number": parts[1],
                               "start": int(parts[2]), "end": int(parts[3]),
                               "qty": int(parts[4]),
                               "note": parts[5] if len(parts) > 5 else ""}
                        records.append(rec); total_qty += rec["qty"]
                        last_end = rec["end"] if last_end is None else max(last_end, rec["end"])
                    except (ValueError, IndexError):
                        pass
        summary.append({
            "upc": upc, "initialized": initialized,
            "allocations": sum(1 for r in records if r["job_number"] != "HISTORY"), "total_qty": total_qty,
            "last_end": last_end,
            "next_start": (last_end + 1) if last_end else SERIAL_START_DEFAULT,
            "records": records,
        })
    return summary


def read_records(upc: str) -> list[dict]:
    """All allocation lines for one UPC (reads only that UPC's file)."""
    path = _registry_path(upc.strip())
    out = []
    if not os.path.isfile(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 5:
                try:
                    out.append({"timestamp": parts[0], "job_number": parts[1], "start": int(parts[2]),
                                "end": int(parts[3]), "qty": int(parts[4]),
                                "note": parts[5] if len(parts) > 5 else ""})
                except ValueError:
                    pass
    return out


def append_records(upc: str, records: list[dict]) -> None:
    """Append existing records (keeping their original timestamps), under the locks.
    Used to repair the registry from the app database or to write ranges that were
    created while the registry was offline."""
    if not records:
        return
    upc = upc.strip()
    with _thread_lock_for(upc):
        lock_path = _acquire_file_lock(upc)
        try:
            os.makedirs(REGISTRY_DIR, exist_ok=True)
            path = _registry_path(upc)
            is_new = not os.path.isfile(path)
            with open(path, "a", encoding="utf-8") as f:
                if is_new:
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    f.write(f"# UPC Serial Registry: {upc}\n# Initialized: {now}\n"
                            "# Format: TIMESTAMP | JOB_NUMBER | START | END | QTY | NOTE\n#\n")
                for r in records:
                    note = str(r.get("note") or "").replace("|", "/")
                    f.write(f"{r['timestamp']} | {r['job_number']} | {r['start']} | {r['end']} | {r['qty']} | {note}\n")
        finally:
            _release_file_lock(lock_path)


def get_upc_history(upc: str) -> dict | None:
    recs = read_records(upc)
    if not recs and not os.path.isfile(_registry_path(upc.strip())):
        return None
    last_end = max((r["end"] for r in recs), default=None)
    return {"upc": upc.strip(), "allocations": sum(1 for r in recs if r["job_number"] not in ("HISTORY", "RESERVED")),
            "total_qty": sum(r["qty"] for r in recs), "last_end": last_end,
            "next_start": (last_end + 1) if last_end else SERIAL_START_DEFAULT, "records": recs}
