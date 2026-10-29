"""RFID proofing inside Job Manager: start a proof from a job, save safely, generate into the
job's Proof folder, approve, back up, link old proofs, import from the standalone app."""
import io
import json
import os
import sqlite3
import zipfile

import pytest


@pytest.fixture
def pf(jm, tmp_path):
    """jm plus a Flask test client, a .btw template file and a preview image."""
    c = jm.app.app.test_client()
    tpl = tmp_path / "Master Templates" / "Acme - label.btw"
    tpl.parent.mkdir(parents=True)
    tpl.write_bytes(b"not really bartender")
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (600, 300), (240, 240, 240)).save(buf, "PNG")
    jm.client, jm.btw, jm.png = c, str(tpl), buf.getvalue()
    return jm


def post(c, url, body=None, who="Sam Rivera"):
    r = c.post(url, json=body or {}, headers={"X-Operator": who})
    return r, r.get_json()


def start_proof(pf, n=1, **job):
    j = pf.job(n, **job)["job"]
    r, d = post(pf.client, f"/proofing/api/jm/jobs/{j['id']}/open")
    assert r.status_code == 200, d
    return j, d


def ready(pf, proof):
    """Fill what a job can't know (font size, inlay, template, preview) so the proof validates."""
    proof = dict(proof, font_size_callout="8 pt", inlay="AD-237", chip="Impinj M730",
                 inlay_width_mm=10, inlay_height_mm=5, template_path=pf.btw)
    form = {"job": json.dumps(proof)}
    r = pf.client.post("/proofing/api/preview/upload",
                       data={**form, "file": (io.BytesIO(pf.png), "art.png")}, content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    return r.get_json()["job"]


def test_page_and_assets_are_served(pf):
    assert pf.app.proofing.STATE["available"], pf.app.proofing.STATE
    r = pf.client.get("/proofing/")
    assert r.status_code == 200 and b"/proofing/static/app.js" in r.data
    assert pf.client.get("/proofing/static/app.js").status_code == 200
    assert pf.client.get("/proofing/api/bootstrap").get_json()["integrated"] is True


def test_proof_started_from_a_job_matches_job_manager_encoding(pf):
    from generators import generate_epc
    j, d = start_proof(pf, 1)
    p = d["job"]
    assert d["created"] and os.path.isfile(d["path"])
    assert p["customer"] == "Acme" and p["job_number"] == "T1" and p["gtin"] == j["upc"]
    assert p["serial"] == j["start_serial"] and p["filter_value"] == 1
    assert p["epc_hex"] == generate_epc(j["upc"], j["start_serial"])          # the first tag in the database
    assert p["jm_job_number"] == j["job_number"] and p["operator_initials"] == "SR"
    assert "[[RFID_VARIABLE_SPEC_V1]]" in p["internal_comments"]
    assert d["jm_job"]["job_number"] == j["job_number"]
    # opening again gives the same proof, not a second one
    _, again = post(pf.client, f"/proofing/api/jm/jobs/{j['id']}/open")
    assert again["path"] == d["path"] and not again.get("created")
    # the Jobs list and the job's activity know about it
    row = next(x for x in pf.client.get("/api/jobs").get_json()["jobs"] if x["id"] == j["id"])
    assert row["proof_status"] == "Draft"
    acts = pf.client.get(f"/api/activity?job_id={j['id']}").get_json()["items"]
    assert any(a["kind"] == "proof" and "started" in a["message"] and a["actor"] == "Sam Rivera" for a in acts)


def test_second_save_from_a_stale_page_is_refused_until_confirmed(pf):
    _, d = start_proof(pf, 2)
    r1, a = post(pf.client, "/proofing/api/job/save", {"job": d["job"], "path": d["path"], "rev": d["rev"]})
    assert r1.status_code == 200
    r2, b = post(pf.client, "/proofing/api/job/save", {"job": d["job"], "path": d["path"], "rev": d["rev"]})
    assert r2.status_code == 409 and b["conflict"]
    r3, _ = post(pf.client, "/proofing/api/job/save", {"job": d["job"], "path": d["path"], "rev": d["rev"], "force": True})
    assert r3.status_code == 200


def test_new_proofs_never_overwrite_each_other(pf):
    body = {"job": {"customer": "Acme", "job_number": "X1", "proof_revision": "1"}}
    _, a = post(pf.client, "/proofing/api/job/save", body)
    _, b = post(pf.client, "/proofing/api/job/save", body)
    assert a["path"] != b["path"] and os.path.isfile(a["path"]) and os.path.isfile(b["path"])


@pytest.mark.parametrize("path", ["../../config.ini", "C:/Windows/win.ini.rfidproof.json", "/etc/passwd"])
def test_paths_outside_the_proofing_folder_are_refused(pf, path, tmp_path):
    r, d = post(pf.client, "/proofing/api/job/save", {"job": {"customer": "x"}, "path": path})
    assert r.status_code in (400, 403) and d["ok"] is False
    outside = tmp_path / "evil.rfidproof.json"
    outside.write_text(json.dumps({"job": {"customer": "x"}}))
    r, _ = post(pf.client, "/proofing/api/job/load", {"path": str(outside)})
    assert r.status_code == 403


def test_bartender_setting_only_accepts_bartend_exe(pf, tmp_path):
    evil = tmp_path / "calc.exe"; evil.write_bytes(b"x")
    r, d = post(pf.client, "/proofing/api/settings/save", {"settings": {"bartender_executable": str(evil)}})
    assert r.status_code == 400 and "bartend.exe" in d["error"]


def test_pdf_goes_to_the_job_proof_folder_and_approval_is_recorded(pf):
    j, d = start_proof(pf, 3)
    proof = ready(pf, d["job"])
    _, s = post(pf.client, "/proofing/api/job/save", {"job": proof, "path": d["path"], "rev": d["rev"]})
    assert s["ok"]
    v = post(pf.client, "/proofing/api/validate", {"job": s["job"]})[1]
    assert v["ready"], v["issues"]
    r, g = post(pf.client, "/proofing/api/proof/pdf", {"job": s["job"]})
    assert r.status_code == 200, g
    assert os.path.dirname(g["path"]) == os.path.join(j["folder_path"], "Proof")
    assert open(g["path"], "rb").read(5) == b"%PDF-"
    assert pf.client.get(g["job_file_url"]).status_code == 200                 # Job Manager's own file link
    _, s2 = post(pf.client, "/proofing/api/job/save", {"job": g["job"], "path": s["path"], "rev": s["rev"]})
    _, a = post(pf.client, "/proofing/api/approval/update",
                {"job": s2["job"], "path": s2["path"], "rev": s2["rev"], "status": "Approved", "operator": "SR"})
    assert a["ok"] and a["locked"] and os.sep + "approved" + os.sep in a["path"]
    proofs = pf.client.get(f"/proofing/api/jm/jobs/{j['id']}/proofs").get_json()["proofs"]
    assert proofs[0]["approval_status"] == "Approved" and proofs[0]["location"] == "approved"
    assert proofs[0]["output_url"].startswith(f"/api/jobs/{j['id']}/file/Proof/")
    row = next(x for x in pf.client.get("/api/jobs").get_json()["jobs"] if x["id"] == j["id"])
    assert row["proof_status"] == "Approved"


def test_epc_that_differs_from_job_manager_encoding_is_flagged(pf):
    _, d = start_proof(pf, 4)
    p = dict(d["job"], filter_value=3)
    p["epc_hex"] = post(pf.client, "/proofing/api/compute-epc", {"job": p})[1]["epc_hex"]   # the standalone default
    v = post(pf.client, "/proofing/api/validate", {"job": p})[1]
    hit = [i for i in v["issues"] if i["name"] == "EPC matches Job Manager encoding"]
    assert hit and hit[0]["severity"] == "warning" and "filter 1" in hit[0]["detail"]


def test_backup_carries_proof_records_and_restore_brings_back_a_lost_one(pf):
    pf.job(9)
    _, d = start_proof(pf, 5)
    r = pf.app.make_backup()
    assert r["success"], r
    names = zipfile.ZipFile(os.path.join(pf.backups, r["file"])).namelist()
    rel = "proofing/jobs/" + os.path.basename(d["path"])
    assert rel in names
    os.remove(d["path"])
    RB = pf.restore()
    RB.app_is_running = lambda: False
    out = RB.apply_restore(os.path.join(pf.backups, r["file"]), keep=True)
    assert out["proofing_files_added"] >= 1 and os.path.isfile(d["path"])


def test_unlinked_proof_is_linked_by_its_ticket(pf):
    j = pf.job(6, ticket="777001")["job"]
    _, s = post(pf.client, "/proofing/api/job/save", {"job": {"customer": "ACME", "job_number": "777001"}})
    assert not s["jm_job"]
    counts = post(pf.client, "/proofing/api/jm/relink")[1]
    assert counts["linked"] == 1
    _, loaded = post(pf.client, "/proofing/api/job/load", {"path": s["path"]})
    assert loaded["jm_job"]["id"] == j["id"]


def test_deleting_a_job_record_with_a_proof_still_works(pf):
    j, _ = start_proof(pf, 7)
    assert pf.client.delete(f"/api/jobs/{j['id']}").status_code == 200


def test_data_panel_lists_proofing(pf):
    stores = pf.client.get("/api/data/status").get_json()["stores"]
    assert any(s["key"] == "proofing" for s in stores)


def test_import_from_the_standalone_app(pf, tmp_path):
    old = tmp_path / "Proofing Test"
    (old / "jobs" / "approved").mkdir(parents=True)
    (old / "config" / "inlay_images").mkdir(parents=True)
    (old / "proofs").mkdir()
    j = pf.job(8, ticket="1987006")["job"]
    cache = r"C:\Users\x\AppData\Local\RFID_Proofing_Utility\cache\bartender\preview_1.png"
    pdf = old / "proofs" / "Acme_1987006_P1_RFID_Proof.pdf"
    pdf.write_bytes(b"%PDF-1.4 old")
    job = {"customer": "Acme", "job_number": "1987006", "proof_revision": "1", "approval_status": "Approved",
           "proof_id": "PRF-20260916-REV1-DAAD7FC2", "preview_path": cache, "preview_fingerprint": "abc",
           "last_generated_output_path": r"C:\Users\x\OneDrive\Desktop\Proofing Test\proofs\Acme_1987006_P1_RFID_Proof.pdf"}
    (old / "jobs" / "approved" / "Acme_1987006_P1.rfidproof.json").write_text(json.dumps({"job": job}))
    (old / "config" / "inlays.json").write_text(json.dumps([{"name": "DogBone", "chip": "M830"}]))
    import importlib, sys
    sys.path.insert(0, pf.root)
    try:
        IP = importlib.import_module("import_proofing")
    finally:
        sys.path.remove(pf.root)
    r = IP.import_proofs(str(old))
    assert r["proofs"] == 1 and r["outputs"] == 1 and r["inlays_added"] == 1 and r["linked"] == 1
    assert r["previews_dropped"] == 1
    dest = os.path.join(pf.config.PROOFING_DATA_DIR, "jobs", "approved", "Acme_1987006_P1.rfidproof.json")
    got = json.load(open(dest))["job"]
    assert got["preview_path"] == "" and got["jm_job_number"] == j["job_number"]
    assert os.path.isfile(got["last_generated_output_path"])
    assert IP.import_proofs(str(old))["proofs"] == 0                          # running again changes nothing


def test_proofing_off_page_explains_and_job_manager_keeps_working(pf):
    from flask import Flask
    pf.app.proofing.STATE.update(available=False, error="ModuleNotFoundError: No module named 'reportlab'")
    other = Flask("x")
    pf.app.proofing._register_fallback(other)
    c = other.test_client()
    assert c.get("/proofing/").status_code == 503 and b"run.bat" in c.get("/proofing/").data
    assert c.post("/proofing/api/job/save", json={}).status_code == 503


def test_two_people_starting_the_same_proof_get_one(pf):
    import threading
    j = pf.job(10)["job"]
    paths, errs = [], []

    def go():
        try:
            c = pf.app.app.test_client()
            paths.append(c.post(f"/proofing/api/jm/jobs/{j['id']}/open", json={}).get_json()["path"])
        except Exception as e:                     # pragma: no cover
            errs.append(e)
    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errs and len(set(paths)) == 1


# ── job ⇄ proof sync ─────────────────────────────────────────────────────────
def edit_job(pf, job_id, **fields):
    """Edit a job the way the drawer does, then run the proof sync it triggers (synchronously)."""
    r = pf.client.patch(f"/api/jobs/{job_id}", json=fields, headers={"X-Operator": "Sam Rivera"})
    assert r.status_code == 200, r.get_json()
    import sys
    sys.modules["proofing.routes"].reconcile(job_id)


def load(pf, path):
    r, d = post(pf.client, "/proofing/api/job/load", {"path": path})
    assert r.status_code == 200, d
    return d


def test_job_edits_reach_untouched_proof_fields(pf):
    j, d = start_proof(pf, 20)
    edit_job(pf, j["id"], part_number="106-STK052E", po_number="PO-778", label_size="1.85 x 0.91",
             job_ticket="T20-B")
    p = load(pf, d["path"])
    assert p["job"]["part_number"] == "106-STK052E" and p["job"]["job_number"] == "T20-B"
    assert (p["job"]["label_width"], p["job"]["label_height"]) == (1.85, 0.91)
    assert '"po_data_reference": "PO-778"' in p["job"]["internal_comments"]
    assert p["sync"]["state"] == "ok"
    acts = pf.client.get(f"/api/activity?job_id={j['id']}").get_json()["items"]
    assert any("updated from the job" in a["message"] and "Part #" in a["message"] for a in acts)


def test_a_value_typed_on_the_proof_is_kept_and_reported(pf):
    j, d = start_proof(pf, 21)
    edit_job(pf, j["id"], part_number="JOB-1")
    p = load(pf, d["path"])
    assert p["job"]["part_number"] == "JOB-1"
    _, s = post(pf.client, "/proofing/api/job/save",
                {"job": dict(p["job"], part_number="CUSTOMER-SAYS"), "path": p["path"], "rev": p["rev"]})
    assert s["sync"]["state"] == "differs"                       # typed over the job's value
    assert pf.client.get(f"/api/jobs/{j['id']}").get_json()["job"]["part_number"] == "JOB-1"   # job untouched
    edit_job(pf, j["id"], part_number="JOB-SAYS")
    p = load(pf, s["path"])
    assert p["job"]["part_number"] == "CUSTOMER-SAYS"
    assert p["sync"]["state"] == "differs" and p["sync"]["differs"][0]["job"] == "JOB-SAYS"
    row = next(x for x in pf.client.get("/api/jobs").get_json()["jobs"] if x["id"] == j["id"])
    assert row["proof_sync"] == "differs"
    _, u = post(pf.client, "/proofing/api/jm/use-job-values", {"path": p["path"], "rev": p["rev"], "fields": ["part_number"]})
    assert u["job"]["part_number"] == "JOB-SAYS" and u["sync"]["state"] == "ok"
    edit_job(pf, j["id"], part_number="JOB-SAYS-2")             # untouched again, so it follows the job
    assert load(pf, s["path"])["job"]["part_number"] == "JOB-SAYS-2"


def test_blank_job_fields_are_filled_from_the_proof(pf):
    j, d = start_proof(pf, 27)
    spec = d["job"]["internal_comments"].replace('"po_data_reference": ""', '"po_data_reference": "PO-FROM-PROOF"')
    _, s = post(pf.client, "/proofing/api/job/save", {"job": dict(d["job"], part_number="P-FROM-PROOF",
                internal_comments=spec), "path": d["path"], "rev": d["rev"]})
    assert {f["field"] for f in s["filled_job"]} == {"part_number", "po_number"}
    jj = pf.client.get(f"/api/jobs/{j['id']}").get_json()["job"]
    assert jj["part_number"] == "P-FROM-PROOF" and jj["po_number"] == "PO-FROM-PROOF"
    acts = pf.client.get(f"/api/activity?job_id={j['id']}").get_json()["items"]
    assert any("filled in from" in a["message"] for a in acts)


def test_an_approved_proof_is_never_changed_only_flagged(pf):
    j, d = start_proof(pf, 22)
    proof = ready(pf, d["job"])
    _, s = post(pf.client, "/proofing/api/job/save", {"job": proof, "path": d["path"], "rev": d["rev"]})
    _, g = post(pf.client, "/proofing/api/proof/pdf", {"job": s["job"]})
    _, s2 = post(pf.client, "/proofing/api/job/save", {"job": g["job"], "path": s["path"], "rev": s["rev"]})
    _, a = post(pf.client, "/proofing/api/approval/update",
                {"job": s2["job"], "path": s2["path"], "rev": s2["rev"], "status": "Approved", "operator": "SR"})
    assert a["locked"]
    before = open(a["path"], "rb").read()
    edit_job(pf, j["id"], part_number="CHANGED-AFTER-APPROVAL")
    assert open(a["path"], "rb").read() == before
    proofs = pf.client.get(f"/proofing/api/jm/jobs/{j['id']}/proofs").get_json()["proofs"]
    assert proofs[0]["sync_state"] == "approved_changed" and "Part #" in proofs[0]["sync_note"]
    acts = pf.client.get(f"/api/activity?job_id={j['id']}").get_json()["items"]
    assert any("start a new proof revision" in x["message"] for x in acts)
    needs = post(pf.client, "/proofing/api/jm/jobs", {"show": "needs"})[1]["jobs"]
    assert any(x["id"] == j["id"] for x in needs)                 # back on the to-do list


def test_editing_a_job_updates_its_proofs_without_waiting(pf):
    import time
    j, d = start_proof(pf, 23)
    pf.client.patch(f"/api/jobs/{j['id']}", json={"part_number": "FAST-1"})
    for _ in range(40):
        if json.load(open(d["path"]))["job"]["part_number"] == "FAST-1":
            break
        time.sleep(0.1)
    assert json.load(open(d["path"]))["job"]["part_number"] == "FAST-1"


def test_proofing_home_lists_jobs_that_need_a_proof(pf):
    a = pf.job(24)["job"]
    b, _ = start_proof(pf, 25)
    d = post(pf.client, "/proofing/api/jm/jobs", {"show": "needs"})[1]
    ids = [x["id"] for x in d["jobs"]]
    assert a["id"] in ids and b["id"] in ids and d["without_proof"] >= 1
    assert next(x for x in d["jobs"] if x["id"] == b["id"])["proof_status"] == "Draft"
    assert [x["id"] for x in post(pf.client, "/proofing/api/jm/jobs", {"q": "T24"})[1]["jobs"]] == [a["id"]]


def test_page_can_tell_when_its_proof_changed_on_the_server(pf):
    j, d = start_proof(pf, 26)
    r1 = post(pf.client, "/proofing/api/job/rev", {"path": d["path"]})[1]
    edit_job(pf, j["id"], part_number="P-26")
    r2 = post(pf.client, "/proofing/api/job/rev", {"path": d["path"]})[1]
    assert r1["rev"] != r2["rev"] and "Part #" in r2["last_sync"]["_changes"]
