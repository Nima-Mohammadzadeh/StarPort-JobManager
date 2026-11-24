"""Proof library: a new UPC job copies its approved proof into the job's Proof folder and prints it."""
import os
import hashlib

import pytest
from openpyxl import Workbook

HDR = ["Proof ID", "Customer", "UPC / Non-UPC", "UPC / Item Number", "Label Size", "Width", "Height", "Units",
       "Date Last Modified", "PDF Path", "Source Path", "Source Type", "PDF SHA-256"]


def _pdf(path, body=b"%PDF-1.4 proof"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").write(body)
    return hashlib.sha256(body).hexdigest()


def _registry(path, rows):
    wb = Workbook(); ws = wb.active; ws.title = "Proof Registry"
    ws.append(["PROOF REGISTRY"]); ws.append([]); ws.append([]); ws.append(HDR)        # header is on row 4, like the real one
    for r in rows:
        ws.append(r)
    wb.save(path)


def _row(pid, cust, item, pdf, sha="", date="2026-01-01"):
    return [pid, cust, "UPC", item, "2 x 1", 2, 1, "in", date, pdf, pdf, "PDF", sha]


@pytest.fixture
def lib(jm, tmp_path, monkeypatch):
    import proofs
    printed = []
    real_print = proofs.print_pdf
    reg = str(tmp_path / "Proof_Registry.xlsx")
    monkeypatch.setattr(proofs, "PROOF_REGISTRY", reg)
    monkeypatch.setattr(proofs, "print_pdf", lambda p: printed.append(p))
    monkeypatch.setattr(jm.app, "PROOF_AUTO_PRINT", True)
    class L: pass
    l = L(); l.reg, l.printed, l.dir, l.proofs, l.real_print = reg, printed, tmp_path, proofs, real_print
    return l


def _job(jm, n, upc, cust="Acme"):
    return jm.app.create_job({"customer": cust, "title": f"Item {n}", "job_ticket": f"P{n}", "upc": upc, "quantity": 100, "lpr": 50})


def test_proof_is_copied_and_printed(jm, lib):
    upc = jm.upc(1)
    pdf = str(lib.dir / "Z" / "Acme - PF-AAA - 2026-01-01.pdf"); sha = _pdf(pdf)
    _registry(lib.reg, [_row("PF-AAA", "Acme", upc, pdf, sha)])
    res, code = _job(jm, 1, upc)
    assert code == 201, res
    dest = os.path.join(res["folder_path"], "Proof", os.path.basename(pdf))
    assert open(dest, "rb").read() == open(pdf, "rb").read()
    assert lib.printed == [dest]
    assert res["proof"] == {"proof_id": "PF-AAA", "file": dest, "printed": True}
    assert not res["warnings"]


def test_no_proof_for_this_upc_still_makes_the_job(jm, lib):
    _registry(lib.reg, [_row("PF-AAA", "Acme", jm.upc(2), str(lib.dir / "x.pdf"))])
    res, code = _job(jm, 3, jm.upc(3))
    assert code == 201 and lib.printed == [] and "proof" not in res
    assert any("No proof for UPC" in w for w in res["warnings"])


def test_registry_not_reachable_is_silent(jm, lib):
    res, code = _job(jm, 4, jm.upc(4))               # the workbook was never written
    assert code == 201 and not res["warnings"] and lib.printed == []


def test_unreadable_registry_never_stops_the_job(jm, lib):
    open(lib.reg, "wb").write(b"not a workbook")
    res, code = _job(jm, 5, jm.upc(5))
    assert code == 201 and any("proof registry could not be used" in w for w in res["warnings"])


def test_leading_zeros_and_newest_for_same_customer(jm, lib):
    upc = jm.upc(6)
    old, new, other = (str(lib.dir / n) for n in ("old.pdf", "new.pdf", "other.pdf"))
    for p in (old, new, other):
        _pdf(p, p.encode())
    _registry(lib.reg, [_row("PF-OLD", "Acme", upc, old, date="2024-01-01"), _row("PF-NEW", "Acme", upc, new, date="2026-05-05"),
                        _row("PF-OTH", "Other", "0" + upc, other, date="2026-09-09")])
    res, code = _job(jm, 6, upc)
    assert code == 201 and res["proof"]["proof_id"] == "PF-NEW"           # this customer's newest, not Other's
    assert any("3 proofs" in w for w in res["warnings"])


def test_missing_pdf_and_changed_pdf_are_warnings(jm, lib):
    a, b = jm.upc(7), jm.upc(8)
    pdf = str(lib.dir / "b.pdf"); _pdf(pdf)
    _registry(lib.reg, [_row("PF-GONE", "Acme", a, str(lib.dir / "gone.pdf")), _row("PF-EDIT", "Acme", b, pdf, sha="0" * 64)])
    r1, c1 = _job(jm, 7, a)
    assert c1 == 201 and any("can't be found" in w for w in r1["warnings"]) and lib.printed == []
    r2, c2 = _job(jm, 8, b)
    assert c2 == 201 and any("checksum" in w for w in r2["warnings"]) and len(lib.printed) == 1


def test_printer_failure_is_a_warning(jm, lib, monkeypatch):
    upc = jm.upc(9); pdf = str(lib.dir / "c.pdf"); _pdf(pdf)
    _registry(lib.reg, [_row("PF-C", "Acme", upc, pdf)])
    monkeypatch.setattr(lib.proofs, "print_pdf", lambda p: "no printer")
    res, code = _job(jm, 9, upc)
    assert code == 201 and "no printer" in res["warnings"] and res["proof"]["printed"] is False
    assert os.path.isfile(res["proof"]["file"])


def test_auto_print_off_copies_but_does_not_print(jm, lib, monkeypatch):
    upc = jm.upc(10); pdf = str(lib.dir / "d.pdf"); _pdf(pdf)
    _registry(lib.reg, [_row("PF-D", "Acme", upc, pdf)])
    monkeypatch.setattr(jm.app, "PROOF_AUTO_PRINT", False)
    res, code = _job(jm, 10, upc)
    assert code == 201 and os.path.isfile(res["proof"]["file"]) and lib.printed == []


def test_special_job_has_no_lookup(jm, lib):
    pdf = str(lib.dir / "e.pdf"); _pdf(pdf)
    _registry(lib.reg, [_row("PF-E", "Acme", "12345", pdf)])
    res, code = jm.app.create_job({"customer": "Acme", "title": "S", "job_ticket": "PS1", "job_type": "special", "quantity": 10, "lpr": 5})
    assert code == 201 and "proof" not in res and lib.printed == []


def test_failed_job_prints_nothing(jm, lib):
    upc = jm.upc(11); pdf = str(lib.dir / "f.pdf"); _pdf(pdf)
    _registry(lib.reg, [_row("PF-F", "Acme", upc, pdf)])
    res, code = jm.app.create_job({"customer": "Acme", "title": "", "job_ticket": "PF1", "upc": upc, "quantity": 100, "lpr": 50})
    assert code == 400 and lib.printed == []


def _win(monkeypatch, proofs, startfile):
    monkeypatch.setattr(proofs, "SYSTEM", "Windows"); monkeypatch.setattr(proofs, "PROOF_PRINT_COMMAND", "")
    monkeypatch.setattr(proofs.os, "startfile", startfile, raising=False)


def test_installed_pdf_program_is_used_first_and_silently(lib, monkeypatch):
    p = lib.proofs; ran, verb = [], []
    _win(monkeypatch, p, lambda path, v: verb.append(path))
    monkeypatch.setattr(p, "_find_pdf_printer", lambda: (lambda f: ["Sumatra.exe", "-print-to-default", "-silent", f]))
    monkeypatch.setattr(p.subprocess, "Popen", lambda cmd, **k: ran.append(cmd))
    assert lib.real_print("a.pdf") is None
    assert ran == [["Sumatra.exe", "-print-to-default", "-silent", "a.pdf"]] and verb == []


def test_without_one_windows_own_print_is_used(lib, monkeypatch):
    p = lib.proofs; verb = []
    _win(monkeypatch, p, lambda path, v: verb.append((path, v)))
    monkeypatch.setattr(p, "_find_pdf_printer", lambda: None)
    assert lib.real_print("a.pdf") is None and verb == [("a.pdf", "print")]


def test_no_print_verb_and_no_pdf_program_says_what_to_install(lib, monkeypatch):
    p = lib.proofs
    def no_app(path, verb):
        e = OSError(); e.winerror = 1155; raise e
    _win(monkeypatch, p, no_app)
    monkeypatch.setattr(p, "_find_pdf_printer", lambda: None)
    assert "SumatraPDF" in lib.real_print("a.pdf")


def test_other_print_errors_are_reported_as_before(lib, monkeypatch):
    p = lib.proofs
    def boom(path, verb): raise OSError("offline")
    _win(monkeypatch, p, boom)
    monkeypatch.setattr(p, "_find_pdf_printer", lambda: None)
    assert "offline" in lib.real_print("a.pdf")
