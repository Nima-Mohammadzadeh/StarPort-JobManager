"""The host PC prints a new job's roll tracking sheet silently: headless browser -> PDF -> default printer."""
import os

import pytest


@pytest.fixture
def host(jm, monkeypatch, tmp_path):
    import rollprint, proofs
    printed, made = [], []
    monkeypatch.setattr(rollprint, "ROLL_SHEET_PRINT", "host")
    monkeypatch.setattr(rollprint, "find_browser", lambda: "edge.exe")
    def fake_pdf(html, pdf, timeout=90):
        made.append(html); open(pdf, "wb").write(b"%PDF-1.4 sheet"); return None
    monkeypatch.setattr(rollprint, "to_pdf", fake_pdf)
    monkeypatch.setattr(proofs, "print_pdf", lambda p: printed.append(p))
    monkeypatch.setattr(proofs, "PROOF_REGISTRY", str(tmp_path / "none.xlsx"))
    class H: pass
    h = H(); h.printed, h.made, h.rollprint = printed, made, rollprint
    return h


def _activity(jm, job_id):
    with jm.db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT kind, message FROM activity WHERE job_id=? ORDER BY id", (job_id,))]


def test_new_job_prints_its_roll_sheet_on_the_host(jm, host):
    res = jm.job(1, qty=200)
    host.rollprint.wait_idle()
    pdf = os.path.join(res["folder_path"], "Print", "Roll sheet.pdf")
    assert host.printed == [pdf] and os.path.isfile(pdf)
    assert host.made and host.made[0].endswith("-QualityControl.html") and os.path.isfile(host.made[0])
    assert any(a["kind"] == "print" and "sent to the printer" in a["message"] for a in _activity(jm, res["job"]["id"]))


def test_job_without_rolls_prints_nothing(jm, host):
    res, code = jm.app.create_job({"customer": "Acme", "title": "No rolls", "job_ticket": "NR1", "job_type": "special"})
    host.rollprint.wait_idle()
    assert code == 201 and host.printed == []


def test_browser_mode_leaves_it_to_the_browser(jm, host, monkeypatch):
    monkeypatch.setattr(host.rollprint, "ROLL_SHEET_PRINT", "browser")
    jm.job(2, qty=200); host.rollprint.wait_idle()
    assert host.printed == [] and jm.app.app.test_client().get("/api/meta").get_json()["roll_print_host"] is False


def test_no_browser_on_the_host_leaves_it_to_the_browser(jm, host, monkeypatch):
    monkeypatch.setattr(host.rollprint, "find_browser", lambda: None)
    jm.job(3, qty=200); host.rollprint.wait_idle()
    assert host.printed == [] and jm.app.app.test_client().get("/api/meta").get_json()["roll_print_host"] is False


def test_meta_says_the_host_prints(jm, host):
    assert jm.app.app.test_client().get("/api/meta").get_json()["roll_print_host"] is True


def test_pdf_failure_is_logged_and_never_breaks_the_job(jm, host, monkeypatch):
    monkeypatch.setattr(host.rollprint, "to_pdf", lambda *a, **k: "the browser did not produce a PDF")
    res = jm.job(4, qty=200); host.rollprint.wait_idle()
    assert host.printed == []
    assert any(a["kind"] == "print_failed" and "not printed" in a["message"] for a in _activity(jm, res["job"]["id"]))


def test_printer_failure_is_logged(jm, host, monkeypatch):
    import proofs
    monkeypatch.setattr(proofs, "print_pdf", lambda p: "no printer")
    res = jm.job(5, qty=200); host.rollprint.wait_idle()
    assert any(a["kind"] == "print_failed" and "no printer" in a["message"] for a in _activity(jm, res["job"]["id"]))


def test_sheets_print_one_after_another(jm, host):
    for n in (6, 7, 8):
        jm.job(n, qty=100)
    host.rollprint.wait_idle()
    assert len(host.printed) == 3 and len(set(host.printed)) == 3


def test_to_pdf_runs_headless_with_its_own_profile(monkeypatch, tmp_path):
    import rollprint
    seen = {}
    monkeypatch.setattr(rollprint, "find_browser", lambda: "edge.exe")
    html = tmp_path / "s.html"; html.write_text("<p>x</p>")
    def fake_run(cmd, **k):
        seen["cmd"] = cmd
        open(next(c for c in cmd if c.startswith("--print-to-pdf=")).split("=", 1)[1], "wb").write(b"%PDF-1.4" + b"0" * 200)
    monkeypatch.setattr(rollprint.subprocess, "run", fake_run)
    out = tmp_path / "Roll sheet.pdf"
    assert rollprint.to_pdf(str(html), str(out)) is None and out.read_bytes().startswith(b"%PDF-")
    cmd = seen["cmd"]
    assert "--headless=new" in cmd and any(c.startswith("--user-data-dir=") for c in cmd) and cmd[-1].startswith("file://")
    assert not (tmp_path / "Roll sheet.pdf.part").exists()


def test_create_from_another_pc_is_printed_by_the_host_and_says_so(jm, host):
    """The server runs on the host, so a job made from any PC prints there; the answer tells that PC's page not to print it."""
    c = jm.app.app.test_client()
    row = {"customer": "Acme", "title": "Remote", "job_ticket": "RM1", "upc": jm.upc(20), "quantity": 100, "lpr": 50}
    out = c.post("/api/jobs/batch", json={"jobs": [row]}, environ_overrides={"REMOTE_ADDR": "10.1.2.3"}).get_json()
    host.rollprint.wait_idle()
    assert out["results"][0]["roll_sheet_printing"] == "host" and len(host.printed) == 1


def test_browser_mode_answer_does_not_claim_host_printing(jm, host, monkeypatch):
    monkeypatch.setattr(host.rollprint, "ROLL_SHEET_PRINT", "browser")
    out = jm.app.app.test_client().post("/api/jobs/batch", json={"jobs": [
        {"customer": "Acme", "title": "B", "job_ticket": "RM2", "upc": jm.upc(21), "quantity": 100, "lpr": 50}]}).get_json()
    assert out["results"][0]["roll_sheet_printing"] is None


def test_page_is_never_cached(jm):
    assert jm.app.app.test_client().get("/").headers["Cache-Control"] == "no-store"
