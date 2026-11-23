"""Roll tracking sheets print by themselves when a job is made.

The browser does the printing (it is the only thing that can reach the printer of the PC the
person is sitting at). The server's part: tell the page which new jobs have rolls, and hand
out the per-PC shortcut that opens Job Manager with silent printing switched on.
"""
import base64
import re


def _row(n, **kw):
    r = {"customer": "Acme", "title": f"Item {n}", "job_ticket": f"AP{n}", "quantity": 100, "lpr": 50}
    r.update(kw)
    return r


def test_single_create_reports_roll_count(jm):
    res = jm.job(1, qty=100)
    assert res["rolls"] >= 2 and res["job"]["id"]


def test_batch_results_carry_job_id_and_rolls(jm):
    rows = [_row(1, upc=jm.upc(1)), _row(2), _row(3, quantity="", lpr="")]
    out = jm.app.app.test_client().post("/api/jobs/batch", json={"jobs": rows}).get_json()
    assert out["created"] == 3, out
    a, b, c = out["results"]
    assert a["job_id"] and a["rolls"] >= 2          # standard job: serial rolls
    assert b["job_id"] and b["rolls"] >= 2          # special with qty + LPR: rolls by quantity
    assert c["job_id"] and c["rolls"] == 0          # nothing to track: nothing to print


def test_batch_failed_row_has_no_job_id(jm):
    out = jm.app.app.test_client().post("/api/jobs/batch", json={"jobs": [_row(4), "junk"]}).get_json()
    assert out["results"][1]["ok"] is False
    assert out["results"][1]["job_id"] is None and out["results"][1]["rolls"] == 0


def _shortcut(jm, host="10.44.4.20:5050"):
    res = jm.app.app.test_client().get("/helper/print-shortcut.cmd", headers={"Host": host})
    assert res.status_code == 200
    assert "attachment" in res.headers["Content-Disposition"]
    body = res.get_data(as_text=True)
    b64 = re.search(r"-EncodedCommand (\S+)", body).group(1)
    return body, base64.b64decode(b64).decode("utf-16-le")


def test_shortcut_opens_this_address_with_silent_printing(jm):
    body, ps = _shortcut(jm)
    assert body.startswith("@echo off") and "\r\n" in body
    assert "--kiosk-printing" in ps
    assert "$url = 'http://10.44.4.20:5050/?silentprint=1'" in ps and "--app=$url" in ps
    assert "--user-data-dir=" in ps          # its own window, so the flag works while Edge is open
    assert "msedge.exe" in ps and "chrome.exe" in ps


def test_shortcut_refuses_odd_host_characters(jm):
    _, ps = _shortcut(jm, host="evil';calc;'x")
    assert "calc" not in ps
    assert "$url = 'http://localhost:" in ps
