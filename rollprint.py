"""
rollprint.py — print the roll tracking sheet on the host PC, with no dialog
===========================================================================
A browser can only print without a dialog when it is started in a special way, which has to be set up on
every PC. Here the host PC does it instead, the same way as the proof:

  1. turn the job's roll tracking sheet (the HTML file in the job folder) into a PDF with Microsoft Edge
     or Chrome running headless (no window), saved as <job folder>\\Print\\Roll sheet.pdf,
  2. print that PDF on the host PC's default printer (proofs.print_pdf: SumatraPDF, Adobe or Windows).

Sheets are printed one after another by a background worker, so creating a job never waits for a printer.
A failure is written to the job's activity log, never raised: the job and its files are already saved.
"""
import os
import queue
import pathlib
import shutil
import tempfile
import threading
import subprocess

import proofs
from config import SYSTEM, ROLL_SHEET_PRINT, PRINT_BROWSER

_q = queue.Queue()
_worker = None
_guard = threading.Lock()


def find_browser():
    """Edge or Chrome on this PC (config.ini [printing] browser wins), or None."""
    if PRINT_BROWSER:
        return PRINT_BROWSER if os.path.isfile(PRINT_BROWSER) else None
    if SYSTEM != "Windows":
        return None
    roots = [os.environ.get(k) for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    for root in (r for r in roots if r):
        for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
            exe = os.path.join(root, rel)
            if os.path.isfile(exe):
                return exe
    return None


def host_prints():
    """True when new roll sheets are printed here (the browser must then not print them again)."""
    return ROLL_SHEET_PRINT == "host" and find_browser() is not None


def to_pdf(html_path, pdf_path, timeout=90):
    """Render the sheet to a PDF with headless Edge/Chrome. Returns an error message or None."""
    exe = find_browser()
    if not exe:
        return "Edge or Chrome was not found on this PC, so the roll sheet could not be turned into a PDF."
    profile = tempfile.mkdtemp(prefix="jm_print_")             # its own profile: works while Edge is open
    tmp = pdf_path + ".part"
    try:
        cmd = [exe, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
               f"--user-data-dir={profile}", "--no-pdf-header-footer", "--print-to-pdf-no-header",
               f"--print-to-pdf={tmp}", pathlib.Path(os.path.abspath(html_path)).as_uri()]
        subprocess.run(cmd, timeout=timeout, capture_output=True)
        if not (os.path.isfile(tmp) and os.path.getsize(tmp) > 100):
            return "The browser did not produce a PDF of the roll sheet."
        with open(tmp, "rb") as fh:
            if fh.read(5) != b"%PDF-":
                return "The browser's output for the roll sheet was not a PDF."
        os.replace(tmp, pdf_path)
        return None
    except subprocess.TimeoutExpired:
        return f"Making the roll sheet PDF took longer than {timeout} seconds."
    except Exception as e:
        return f"The roll sheet PDF could not be made: {e}"
    finally:
        shutil.rmtree(profile, ignore_errors=True)
        try: os.remove(tmp)
        except OSError: pass


def _run(item):
    html_path, job_dir, label, note = item
    pdf = os.path.join(job_dir, "Print", "Roll sheet.pdf")
    os.makedirs(os.path.dirname(pdf), exist_ok=True)
    err = to_pdf(html_path, pdf) or proofs.print_pdf(pdf)
    note(f"Roll sheet for {label} " + (f"was not printed: {err}" if err else "sent to the printer"), bool(err))


def _loop():
    while True:
        item = _q.get()
        try:
            _run(item)
        except Exception as e:                                  # the worker must outlive any one sheet
            try: item[3](f"Roll sheet for {item[2]} was not printed: {e}", True)
            except Exception: pass
        finally:
            _q.task_done()


def submit(html_path, job_dir, label, note):
    """Queue one sheet. note(message, failed) is called when it has been handled."""
    global _worker
    with _guard:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_loop, name="roll-sheet-printer", daemon=True)
            _worker.start()
    _q.put((html_path, job_dir, label, note))


def wait_idle():
    """Block until everything queued has been handled (tests)."""
    _q.join()
