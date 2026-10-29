"""
proofing — the RFID Proofing Utility, inside Job Manager
========================================================
Everyone opens it from Job Manager at http://<host>:5050/proofing (or from a job's drawer:
"Start proof" / "Open proof"). It is the proofing app's Lean Core (Beta 21.4): same proof
records, same validation, same editable customer PDF and package, same approval lock.

    proofing/core/      the proofing app's app/ package, unchanged except: the data folder
                        comes from config.ini [proofing] data_dir (settings.py), ProofJob
                        carries jm_job_id / jm_job_number (models.py), and those two stay out
                        of customer packages (package_export.py)
    proofing/routes.py  its server (was support/runtime/web_server.py) as a Flask blueprint
    proofing/jm_link.py which proof belongs to which job (job_proofs table)
    proofing/static/    its page (index.html, app.js, styles.css), adapted for /proofing
    proofing/assets/    logos printed on the proof

app.py calls init_schema() from init_db() and init_app() once at import. If Pillow or
reportlab is missing, or the data folder can't be reached, Job Manager still runs and
/proofing explains what to fix.
"""
import os

from config import PROOFING_DATA_DIR, PROOFING_IS_DEFAULT

# Must be set before proofing.core is imported: core/settings.py reads it once.
os.environ["JM_PROOFING_DATA"] = PROOFING_DATA_DIR

from .jm_link import init_schema, PROOF_SUMMARY_SQL  # noqa: E402,F401  (no Pillow needed)

STATE = {"available": False, "error": None}


def init_app(app, **hooks):
    """Register /proofing on the Flask app. hooks: get_db, log_activity, job_label, now_str,
    match_templates, jm_settings, z_drive_base."""
    try:
        from . import routes
    except Exception as e:                    # missing Pillow / reportlab, or data folder unreachable
        STATE.update(available=False, error=f"{type(e).__name__}: {e}")
        _register_fallback(app)
        print(f"  [PRF] Proofing is OFF: {STATE['error']}")
        return False
    routes.HOOKS.update(hooks)
    app.register_blueprint(routes.bp)
    STATE.update(available=True, error=None)
    return True


def start(app=None):
    """Background work after startup: cache cleanup and rebuilding the proof index."""
    if STATE["available"]:
        from . import routes
        routes.start_background()
        print(f"  [PRF] proofing on /proofing, data in {PROOFING_DATA_DIR}"
              + ("  (next to the app: set [proofing] data_dir for a shared folder)" if PROOFING_IS_DEFAULT else ""))


def _register_fallback(app):
    from flask import Blueprint, jsonify
    from html import escape
    bp = Blueprint("proofing", __name__, url_prefix="/proofing")
    msg = STATE["error"] or "unknown error"
    fix = ("Close Job Manager and start it again with run.bat; it installs Pillow and reportlab."
           if "No module named" in msg else
           f"Check that the proofing data folder can be reached from the host PC: {PROOFING_DATA_DIR}")

    @bp.route("/")
    def page():
        return (f"""<!doctype html><meta charset="utf-8"><title>Proofing unavailable</title>
<body style="font-family:Segoe UI,Arial,sans-serif;max-width:640px;margin:60px auto;color:#17202a">
<h1 style="font-size:22px">Proofing isn't available right now</h1>
<p>{escape(fix)}</p><p style="color:#65717e;font-size:13px">{escape(msg)}</p>
<p><a href="/">Back to Job Manager</a></p></body>""", 503)

    @bp.route("/api/<path:_rest>", methods=["GET", "POST"])
    def api(_rest):
        return jsonify({"ok": False, "error": f"Proofing isn't available: {msg}. {fix}"}), 503

    app.register_blueprint(bp)


def data_store():
    """Row for Settings > Data and backups."""
    from pathlib import Path
    root = Path(PROOFING_DATA_DIR)
    jobs = root / "jobs"
    count = lambda d: sum(1 for f in d.glob("*.rfidproof.json")) if d.is_dir() else 0
    reachable = root.is_dir()
    active, approved, archived = count(jobs), count(jobs / "approved"), count(jobs / "archive")
    return {"key": "proofing", "name": "Proofing data", "path": str(root),
            "level": ("ok" if not PROOFING_IS_DEFAULT else "warn") if reachable and STATE["available"] else "bad",
            "holds": "Saved proofs, proof history, proofing settings, the inlay library and preview images",
            "status": (("Shared" if not PROOFING_IS_DEFAULT else "This PC only") if reachable else "Not reachable")
                      + ("" if STATE["available"] else f" (proofing off: {STATE['error']})"),
            "detail": f"{active:,} active, {approved:,} approved, {archived:,} archived proofs",
            "backup": "Yes, in every backup (proof PDFs are in the job folders)"}


def backup_sources():
    """(folder, name in the zip, keep(path) -> bool) for backup.write_snapshot. Proof
    records, history, settings and inlay images; not cache, previews or generated PDFs."""
    from pathlib import Path
    root = Path(PROOFING_DATA_DIR)
    keep_jobs = lambda p: p.endswith((".rfidproof.json", ".snapshot.json"))
    return [(str(root / "config"), "proofing/config", lambda p: True),
            (str(root / "jobs"), "proofing/jobs", keep_jobs),
            (str(root / "logs"), "proofing/logs", lambda p: p.endswith(".csv"))]
