"""
proofs.py — find the customer's approved proof for a UPC, copy it into the job, print it
=======================================================================================
The proof library keeps one master workbook (Proof_Registry.xlsx, sheet "Proof Registry"). Each row is
one approved proof PDF: customer, "UPC / Item Number", label size, "PDF Path", PDF SHA-256 ...

When a job with a UPC is created:
  1. find the proof whose "UPC / Item Number" is that UPC (leading zeros ignored),
  2. copy its PDF into the job's Proof folder and check the copy against the registry's SHA-256,
  3. print the copy on the host PC's default printer.

Nothing here can stop a job being made: the registry is only ever read (never written), and every
problem comes back as a plain message for the job's warnings. No registry file, no proof, no message.
"""
import os
import re
import shutil
import hashlib
import threading
import subprocess

from config import PROOF_REGISTRY, PROOF_AUTO_PRINT, PROOF_PRINT_COMMAND, SYSTEM

SHEET = "Proof Registry"
HEADER = {"id": "Proof ID", "customer": "Customer", "kind": "UPC / Non-UPC", "item": "UPC / Item Number",
          "size": "Label Size", "date": "Date Last Modified", "pdf": "PDF Path", "src": "Source Path",
          "sha": "PDF SHA-256", "desc": "Item Description"}

_lock = threading.Lock()
_cache = {"key": None, "by_upc": {}}


def _key(value):
    """The comparable form of a UPC / item number: digits only, no leading zeros, or None when it isn't a number."""
    s = str(value or "").strip()
    return s.lstrip("0") or "0" if s.isdigit() else None


def _load(path):
    """{upc key: [proof rows, newest first]}. Re-read only when the workbook changed."""
    st = os.stat(path)
    sig = (path, st.st_mtime_ns, st.st_size)
    with _lock:
        if _cache["key"] == sig:
            return _cache["by_upc"]
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        if SHEET not in wb.sheetnames:
            raise ValueError(f'the workbook has no "{SHEET}" sheet')
        rows = wb[SHEET].iter_rows(values_only=True)
        cols = None
        for r in rows:                                       # the header row is not always row 1
            if r and "Proof ID" in r and "UPC / Item Number" in r:
                cols = {h: r.index(name) for h, name in HEADER.items() if name in r}
                break
        if not cols or "item" not in cols or "pdf" not in cols:
            raise ValueError("the Proof Registry sheet is missing its UPC / Item Number or PDF Path column")
        by_upc = {}
        for r in rows:
            def get(k):
                i = cols.get(k)
                v = r[i] if i is not None and i < len(r) else None
                return "" if v is None else str(v).strip()
            k = _key(get("item"))
            if k and get("id").startswith("PF-"):
                by_upc.setdefault(k, []).append({h: get(h) for h in HEADER})
    finally:
        wb.close()
    for lst in by_upc.values():
        lst.sort(key=lambda p: p["date"], reverse=True)       # dates are ISO text: newest first
    with _lock:
        _cache.update(key=sig, by_upc=by_upc)
    return by_upc


def find(upc, customer=None):
    """The proof for this UPC: {"proof": row | None, "others": n, "note": text}. Raises nothing the caller must handle
    except OSError / ValueError, which mean the registry could not be read."""
    if not PROOF_REGISTRY or not os.path.isfile(PROOF_REGISTRY):
        return {"proof": None, "others": 0, "note": ""}      # not set up on this PC: quietly nothing
    k = _key(upc)
    hits = _load(PROOF_REGISTRY).get(k, []) if k else []
    if not hits:
        return {"proof": None, "others": 0, "note": f"No proof for UPC {upc} in the proof registry."}
    same = [p for p in hits if customer and p["customer"].lower() == str(customer).strip().lower()]
    pick = (same or hits)[0]
    note = ""
    if len(hits) > 1:
        note = (f"The proof registry has {len(hits)} proofs for UPC {upc}; used the newest "
                f"({pick['id']}, {pick['date']}). Check it is the right one.")
    return {"proof": pick, "others": len(hits) - 1, "note": note}


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_into(proof, job_dir):
    """Copy the proof PDF into <job_dir>/Proof. Returns (dest path or None, warning or None)."""
    src = next((p for p in (proof["pdf"], proof["src"]) if p and os.path.isfile(p)), None)
    if not src:
        return None, f"Proof {proof['id']} is in the registry but its PDF can't be found at {proof['pdf']}."
    if not src.lower().endswith(".pdf"):
        return None, f"Proof {proof['id']} is not a PDF ({os.path.basename(src)}), so it was not copied."
    folder = os.path.join(job_dir, "Proof")
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, os.path.basename(src))
    tmp = dest + ".part"
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
    except OSError as e:
        try: os.remove(tmp)
        except OSError: pass
        return None, f"The proof PDF could not be copied: {e}"
    warn = None
    want = (proof.get("sha") or "").lower()
    if want and _sha256(dest) != want:
        warn = (f"Proof {proof['id']} was copied, but the file no longer matches the registry's checksum. "
                "It may have been edited after it was registered. Check it before it is used.")
    return dest, warn


def print_pdf(path):
    """Send the PDF to the host PC's default printer. Returns an error message, or None when it was handed to the printer."""
    try:
        if PROOF_PRINT_COMMAND:
            cmd = PROOF_PRINT_COMMAND.replace("{file}", path)
            subprocess.Popen(cmd, shell=(SYSTEM == "Windows"))
        elif SYSTEM == "Windows":
            os.startfile(path, "print")                       # the PDF app's own Print verb: default printer
        else:
            subprocess.Popen(["lp", path])
    except Exception as e:
        return f"The proof was copied but could not be sent to the printer: {e}"
    return None


def attach(upc, customer, job_dir, print_it=None, printer=None):
    """Everything above for one new job. Returns {"found", "proof_id", "file", "printed", "warnings"} and never raises."""
    out = {"found": False, "proof_id": None, "file": None, "printed": False, "warnings": []}
    try:
        r = find(upc, customer)
        if r["note"]:
            out["warnings"].append(r["note"])
        p = r["proof"]
        if not p:
            return out
        out.update(found=True, proof_id=p["id"])
        dest, warn = copy_into(p, job_dir)
        if warn:
            out["warnings"].append(warn)
        if not dest:
            return out
        out["file"] = dest
        if PROOF_AUTO_PRINT if print_it is None else print_it:
            err = (printer or print_pdf)(dest)
            if err:
                out["warnings"].append(err)
            else:
                out["printed"] = True
    except Exception as e:
        out["warnings"].append(f"The proof registry could not be used: {e}")
    return out
