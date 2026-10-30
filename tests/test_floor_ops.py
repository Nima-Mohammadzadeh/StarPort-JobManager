"""
Encoding floor: BarTender print requests, tag checks and verified rolls, next roll on the same printer,
pace, reprints, the shift summary and "new job like this".
"""
import os
from datetime import datetime, timedelta


def client(jm, who="Brett"):
    c = jm.app.app.test_client()
    c.environ_base["HTTP_X_OPERATOR"] = who
    return c


def db(jm, sql, args=()):
    conn = jm.app.get_db()
    conn.execute(sql, args)
    conn.commit(); conn.close()


def one(jm, sql, args=()):
    conn = jm.app.get_db()
    try:
        r = conn.execute(sql, args).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def detail(c, jid):
    return c.get(f"/api/jobs/{jid}").get_json()


def setup_bt(jm, tmp_path, ack=True):
    trig, acks = tmp_path / "BarTender Triggers", tmp_path / "BarTender Acknowledgements"
    trig.mkdir(); acks.mkdir()
    r = client(jm).post("/api/settings", json={"bt_enabled": True, "bt_trigger_dir": str(trig),
                                                "bt_ack_dir": str(acks) if ack else "",
                                                "bt_printer_map": "Printer 1 = ZDesigner ZT411-203dpi ZPL\njunk line"})
    assert r.status_code == 200, r.get_json()
    s = r.get_json()["settings"]
    assert s["bt_printer_map"] == "Printer 1 = ZDesigner ZT411-203dpi ZPL"
    return trig, acks


def job_with_template(jm, n, **kw):
    res = jm.job(n, **kw)
    j = res["job"]
    tpl = os.path.join(res["folder_path"], "Print", f"{res['job_number']} - Acme 2x1.btw")
    db(jm, "UPDATE jobs SET template_path=? WHERE id=?", (tpl, j["id"]))
    return j


# ── BarTender ─────────────────────────────────────────────────────────────────
def test_send_a_roll_and_follow_it_to_printed(jm, tmp_path):
    trig, acks = setup_bt(jm, tmp_path)
    j = job_with_template(jm, 1, qty=500)
    c = client(jm, "Dana")
    c.post(f"/api/jobs/{j['id']}/rolls/2/printer", json={"printer": "Printer 1"})
    r = c.post(f"/api/jobs/{j['id']}/rolls/2/print", json={})
    assert r.status_code == 200, r.get_json()
    req = r.get_json()["requests"][0]
    assert req["state"] == "sent" and req["rec_from"] == 51 and req["rec_to"] == 100
    files = os.listdir(trig)
    assert files == [req["req_id"] + ".btxml"]
    text = (trig / files[0]).read_text()
    assert "<RecordRange>51-100</RecordRange>" in text and "<Printer>ZDesigner ZT411-203dpi ZPL</Printer>" in text
    assert "Acme 2x1.btw</Format>" in text and "<UseDatabase>true</UseDatabase>" in text

    again = c.post(f"/api/jobs/{j['id']}/rolls/2/print", json={})
    assert again.status_code == 409 and again.get_json()["waiting"]

    os.remove(trig / files[0])                                    # BarTender takes the file
    jm.app.check_print_requests()
    assert one(jm, "SELECT state FROM print_requests")["state"] == "picked_up"
    (acks / f"{req['req_id']}.xml").write_text(
        '<XMLScript Version="2.0"><Response><Message Severity="Information"><Text>Job sent</Text></Message></Response></XMLScript>')
    jm.app.check_print_requests()
    assert one(jm, "SELECT state FROM print_requests")["state"] == "printed"

    r = c.post(f"/api/jobs/{j['id']}/rolls/2/print", json={})
    assert r.status_code == 409 and r.get_json()["needs_again"]
    assert c.post(f"/api/jobs/{j['id']}/rolls/2/print", json={"again": True}).status_code == 200
    d = detail(c, j["id"])
    assert len(d["prints"]) == 2 and d["bt"]["enabled"]
    msgs = [a["message"] for a in c.get("/api/activity").get_json()["items"]]
    assert any("printed by BarTender" in m for m in msgs) and any("sent to BarTender (records 51–100) on Printer 1" in m for m in msgs)


def test_a_pending_roll_is_started_and_unmapped_printer_warns(jm, tmp_path):
    setup_bt(jm, tmp_path)
    j = job_with_template(jm, 2, qty=200)
    r = client(jm).post(f"/api/jobs/{j['id']}/rolls/1/print", json={"printer": "Printer 7"})
    assert r.status_code == 200
    assert any("Printer 7 has no Windows printer name" in w for w in r.get_json()["warnings"])
    roll = one(jm, "SELECT * FROM job_rolls WHERE job_id=? AND roll_number=1", (j["id"],))
    assert roll["status"] == "running" and roll["printer"] == "Printer 7" and roll["started_by"] == "Brett"
    assert one(jm, "SELECT status FROM jobs WHERE id=?", (j["id"],))["status"] == "In Progress"


def test_a_failure_reply_reaches_needs_attention(jm, tmp_path):
    trig, acks = setup_bt(jm, tmp_path)
    j = job_with_template(jm, 3, qty=200)
    c = client(jm)
    req = c.post(f"/api/jobs/{j['id']}/rolls/1/print", json={}).get_json()["requests"][0]
    os.remove(trig / (req["req_id"] + ".btxml"))
    (acks / f"Response_{req['req_id']}.xml").write_text(
        '<XMLScript><Response><Message ID="3203" Severity="Error"><Text>Printer ZT411 is offline.</Text></Message></Response></XMLScript>')
    jm.app.check_print_requests()
    row = one(jm, "SELECT state, message FROM print_requests")
    assert row == {"state": "failed", "message": "Printer ZT411 is offline."}
    att = c.get("/api/attention").get_json()
    mine = [it for it in att["items"] if it["job"]["id"] == j["id"]][0]
    assert any(i["kind"] == "print_failed" and "offline" in i["detail"] for i in mine["issues"])
    # A failed request doesn't block sending it again.
    assert c.post(f"/api/jobs/{j['id']}/rolls/1/print", json={}).status_code == 200


def test_reply_found_by_its_contents_and_without_ack_folder(jm, tmp_path):
    trig, acks = setup_bt(jm, tmp_path)
    j = job_with_template(jm, 4, qty=200)
    c = client(jm)
    req = c.post(f"/api/jobs/{j['id']}/rolls/1/print", json={}).get_json()["requests"][0]
    os.remove(trig / (req["req_id"] + ".btxml"))
    (acks / "response_0001.xml").write_text(f'<XMLScript ID="{req["req_id"]}"><Response/></XMLScript>')
    jm.app.check_print_requests()
    assert one(jm, "SELECT state FROM print_requests")["state"] == "printed"
    # No acknowledgement folder: picked up is as far as it can go.
    assert c.post("/api/settings", json={"bt_ack_dir": ""}).get_json()["settings"]["bt_ack_dir"] == ""
    req2 = c.post(f"/api/jobs/{j['id']}/rolls/2/print", json={}).get_json()["requests"][0]
    os.remove(trig / (req2["req_id"] + ".btxml"))
    jm.app.check_print_requests()
    assert one(jm, "SELECT state FROM print_requests WHERE req_id=?", (req2["req_id"],))["state"] == "taken"


def test_a_roll_across_two_database_files_is_two_requests(jm, tmp_path):
    trig, _ = setup_bt(jm, tmp_path)
    res, code = jm.app.create_job({"customer": "Acme", "title": "Split", "job_ticket": "T5", "upc": jm.upc(5),
                                   "quantity": 300, "lpr": 50, "qty_db": 120, "overage_pct": 0})
    assert code == 201, res
    db(jm, "UPDATE jobs SET template_path='X.btw' WHERE id=?", (res["job"]["id"],))
    r = client(jm).post(f"/api/jobs/{res['job']['id']}/rolls/3/print", json={}).get_json()
    reqs = r["requests"]
    assert [(q["part"], q["rec_from"], q["rec_to"]) for q in reqs] == [(1, 101, 120), (2, 1, 30)]
    assert reqs[0]["database"].endswith(f"{jm.upc(5)}_DB1.xlsx") and reqs[1]["database"].endswith(f"{jm.upc(5)}_DB2.xlsx")
    assert len(os.listdir(trig)) == 2 and any("split into files" in w for w in r["warnings"])


def test_cancel_and_the_rules_for_sending(jm, tmp_path):
    c = client(jm)
    j = job_with_template(jm, 6, qty=200)
    assert "turned off" in c.post(f"/api/jobs/{j['id']}/rolls/1/print", json={}).get_json()["error"]
    trig, _ = setup_bt(jm, tmp_path)
    req = c.post(f"/api/jobs/{j['id']}/rolls/1/print", json={}).get_json()["requests"][0]
    assert c.post(f"/api/print-requests/{req['id']}/cancel").status_code == 200
    assert os.listdir(trig) == [] and one(jm, "SELECT state FROM print_requests")["state"] == "cancelled"
    c.post(f"/api/jobs/{j['id']}/rolls/1", json={"status": "complete"})
    assert "done" in c.post(f"/api/jobs/{j['id']}/rolls/1/print", json={}).get_json()["error"]
    db(jm, "UPDATE jobs SET template_path=NULL WHERE id=?", (j["id"],))
    assert "no BarTender template" in c.post(f"/api/jobs/{j['id']}/rolls/2/print", json={}).get_json()["error"]


def test_btxml_for_a_text_database_names_the_file():
    import floor_ops as F
    x = F.btxml("JM-1", r"Z:\a & b.btw", 1, 10, None, r"Z:\Data\x.csv")
    assert "<FileName>Z:\\Data\\x.csv</FileName>" in x and "&amp; b.btw" in x and "<Printer>" not in x


# ── tag checks ────────────────────────────────────────────────────────────────
def scan(c, j, epc, roll=None, source="rewind"):
    return c.post("/api/rewind/lookup", json={"raw": epc, "job_id": j["id"], "roll_number": roll, "source": source}).get_json()


def test_first_and_last_tag_verify_a_done_roll(jm):
    j = jm.job(10, qty=200)["job"]
    c = client(jm, "Rae")
    rolls = detail(c, j["id"])["rolls"]
    r1 = rolls[0]
    c.post(f"/api/jobs/{j['id']}/rolls/1", json={"status": "complete"})
    a = scan(c, j, r1["epc_start"], 1)
    assert a["result"] == "found" and a["check"]["result"] == "pass" and a["check"]["first_ok"] and not a["check"]["verified"]
    b = scan(c, j, r1["epc_end"], 1)
    assert b["check"]["verified"] and b["check"]["roll_status"] == "verified"
    roll = one(jm, "SELECT * FROM job_rolls WHERE job_id=? AND roll_number=1", (j["id"],))
    assert roll["status"] == "verified" and roll["verified_by"] == "Rae"
    # Still counts as done everywhere.
    assert c.get("/api/jobs").status_code == 200
    assert detail(c, j["id"])["checks"][0]["result"] == "pass"
    # Completing it again changes nothing; reopening clears verification.
    assert c.post(f"/api/jobs/{j['id']}/rolls/1", json={"status": "complete"}).get_json()["changed"] == []
    c.post(f"/api/jobs/{j['id']}/rolls/1", json={"status": "running"})
    roll = one(jm, "SELECT * FROM job_rolls WHERE job_id=? AND roll_number=1", (j["id"],))
    assert roll["status"] == "running" and roll["verified_at"] is None and roll["first_ok_at"] is None


def test_scans_before_done_verify_when_it_is_completed(jm):
    j = jm.job(11, qty=200)["job"]
    c = client(jm)
    r2 = detail(c, j["id"])["rolls"][1]
    c.post(f"/api/jobs/{j['id']}/rolls/2", json={"status": "running"})
    scan(c, j, r2["epc_start"], 2, "desktop"); scan(c, j, r2["epc_end"], 2, "desktop")
    assert one(jm, "SELECT status FROM job_rolls WHERE job_id=? AND roll_number=2", (j["id"],))["status"] == "running"
    c.post(f"/api/jobs/{j['id']}/rolls/2", json={"status": "complete"})
    assert one(jm, "SELECT status FROM job_rolls WHERE job_id=? AND roll_number=2", (j["id"],))["status"] == "verified"


def test_a_wrong_tag_is_a_failed_check_in_needs_attention(jm):
    j = jm.job(12, qty=200)["job"]
    other = jm.job(13, qty=200)["job"]
    c = client(jm)
    rolls = detail(c, j["id"])["rolls"]
    bad = scan(c, j, rolls[2]["epc_start"], 1)                          # roll 3's tag on roll 1
    assert bad["check"]["result"] == "fail" and bad["check"]["roll_number"] == 1
    assert "roll 3, not roll 1" in one(jm, "SELECT message FROM roll_checks")["message"]
    wrong = scan(c, j, detail(c, other["id"])["rolls"][0]["epc_start"], 2)
    assert wrong["result"] == "other_job" and wrong["check"]["result"] == "fail"
    att = c.get("/api/attention").get_json()
    mine = [it for it in att["items"] if it["job"]["id"] == j["id"]][0]
    assert sum(i["kind"] == "tag_check" for i in mine["issues"]) == 2
    # Scanner setup problems aren't kept; a later pass clears roll 1's failure.
    assert "check" not in scan(c, j, "hello", 1) or scan(c, j, "hello", 1)["check"] is None
    scan(c, j, rolls[0]["epc_start"], 1)
    mine = [it for it in c.get("/api/attention").get_json()["items"] if it["job"]["id"] == j["id"]][0]
    assert [i["detail"][:6] for i in mine["issues"] if i["kind"] == "tag_check"] == ["Roll 2"]


def test_lookups_without_a_job_record_nothing(jm):
    j = jm.job(14, qty=200)["job"]
    c = client(jm)
    epc = detail(c, j["id"])["rolls"][0]["epc_start"]
    assert c.post("/api/rewind/lookup", json={"raw": epc}).get_json()["result"] == "found"
    c.post("/api/rewind/lookup", json={"raw": epc, "job_id": j["id"], "record": False})
    assert one(jm, "SELECT COUNT(*) n FROM roll_checks")["n"] == 0


def test_reset_clears_checks(jm):
    j = jm.job(15, qty=200)["job"]
    c = client(jm)
    r1 = detail(c, j["id"])["rolls"][0]
    scan(c, j, r1["epc_start"], 1)
    c.post(f"/api/jobs/{j['id']}/rolls/1", json={"status": "pending"})
    assert c.post(f"/api/jobs/{j['id']}/rolls/1", json={"status": "running"}).status_code == 200
    c.post(f"/api/jobs/{j['id']}/rolls/1", json={"status": "pending"})
    assert one(jm, "SELECT first_ok_at FROM job_rolls WHERE job_id=? AND roll_number=1", (j["id"],))["first_ok_at"] is None


# ── next roll on the same printer ─────────────────────────────────────────────
def test_done_offers_the_next_roll_on_the_same_printer(jm):
    j = jm.job(20, qty=200)["job"]
    c = client(jm)
    c.post(f"/api/jobs/{j['id']}/rolls/1/printer", json={"printer": "Printer 2"})
    r = c.post(f"/api/jobs/{j['id']}/rolls/1", json={"status": "complete"}).get_json()
    assert r["next"] == {"roll_number": 2, "quantity": 50, "printer": "Printer 2", "can_print": False}
    # Printer already running something else: nothing to offer. No printer: nothing either.
    c.post(f"/api/jobs/{j['id']}/rolls/2/printer", json={"printer": "Printer 2"})
    c.post(f"/api/jobs/{j['id']}/rolls/3/printer", json={"printer": "Printer 2"})
    assert c.post(f"/api/jobs/{j['id']}/rolls/2", json={"status": "complete"}).get_json()["next"] is None
    assert c.post(f"/api/jobs/{j['id']}/rolls/4", json={"status": "complete"}).get_json()["next"] is None


# ── pace ──────────────────────────────────────────────────────────────────────
def test_pace_and_finish_estimate(jm):
    import floor_ops as F
    now = datetime(2026, 10, 30, 12, 0, 0)
    f = lambda m: (now - timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S")
    rolls = [
        {"roll_number": 1, "quantity": 500, "status": "verified", "started_at": f(100), "completed_at": f(80), "printer": "P1"},
        {"roll_number": 2, "quantity": 500, "status": "complete", "started_at": f(80), "completed_at": f(60), "printer": "P1"},
        {"roll_number": 3, "quantity": 500, "status": "complete", "started_at": f(60), "completed_at": f(30), "printer": "P2"},
        {"roll_number": 4, "quantity": 500, "status": "complete", "started_at": None, "completed_at": f(5)},       # bulk mark
        {"roll_number": 5, "quantity": 500, "status": "running", "started_at": f(10), "printer": "P1"},
        {"roll_number": 6, "quantity": 500, "status": "running", "started_at": f(5), "printer": "P2"},
        {"roll_number": 7, "quantity": 500, "status": "pending"},
        {"roll_number": 8, "quantity": 500, "status": "pending"},
    ]
    p = F.pace(rolls, now)
    assert p["minutes_per_roll"] == 20 and p["timed_rolls"] == 3 and p["labels_per_hour"] == 1285        # 1,500 labels in 70 minutes
    # left: 10 + 15 + 20 + 20 = 65 min over 2 printers = 32.5 min
    assert p["rolls_left"] == 4 and p["printers"] == 2 and p["finish_at"] == "2026-10-30 12:32"
    assert F.pace(rolls[:1], now) is None


def test_job_detail_has_pace(jm):
    j = jm.job(21, qty=300)["job"]
    for n, (a, b) in enumerate([(50, 35), (35, 20)], start=1):
        db(jm, "UPDATE job_rolls SET status='complete', started_at=datetime('now','localtime',?), completed_at=datetime('now','localtime',?) "
               "WHERE job_id=? AND roll_number=?", (f"-{a} minutes", f"-{b} minutes", j["id"], n))
    p = detail(client(jm), j["id"])["pace"]
    assert p["minutes_per_roll"] == 15 and p["rolls_left"] == 5 and p["finish_at"]


# ── reprints ──────────────────────────────────────────────────────────────────
def test_reprints_are_logged_checked_and_removed(jm):
    j = jm.job(30, qty=200)["job"]
    c = client(jm, "Lee")
    r1 = detail(c, j["id"])["rolls"][0]
    url = f"/api/jobs/{j['id']}/reprints"
    assert "reason" in c.post(url, json={"roll_number": 1, "qty": 3}).get_json()["error"]
    assert "Say what happened" in c.post(url, json={"roll_number": 1, "qty": 3, "reason": "Other"}).get_json()["error"]
    assert "1 to 50" in c.post(url, json={"roll_number": 1, "qty": 51, "reason": "Jam / torn"}).get_json()["error"]
    assert "aren't on roll 1" in c.post(url, json={"roll_number": 1, "serial_from": r1["serial_end"] + 1,
                                                   "reason": "Bad encode"}).get_json()["error"]
    assert c.post(url, json={"roll_number": 1, "qty": 3, "reason": "Jam / torn", "note": "splice"}).status_code == 200
    assert c.post(url, json={"roll_number": 1, "serial_from": r1["serial_start"] + 9, "serial_to": r1["serial_start"] + 4,
                             "reason": "Bad encode"}).status_code == 200
    d = detail(c, j["id"])
    assert d["reprinted_total"] == 9 and d["reprints"][0]["qty"] == 6 and d["reprints"][0]["serial_from"] == r1["serial_start"] + 4
    assert c.delete(f"/api/reprints/{d['reprints'][0]['id']}").status_code == 200
    assert detail(c, j["id"])["reprinted_total"] == 3


# ── shift summary ─────────────────────────────────────────────────────────────
def test_shift_window():
    import floor_ops as F
    s, e, _ = F.shift_window("06:00,14:00,22:00", "current", datetime(2026, 10, 30, 3, 15))
    assert (s, e) == (datetime(2026, 10, 29, 22, 0), datetime(2026, 10, 30, 6, 0))
    s, e, _ = F.shift_window("06:00,14:00,22:00", "previous", datetime(2026, 10, 30, 3, 15))
    assert (s, e) == (datetime(2026, 10, 29, 14, 0), datetime(2026, 10, 29, 22, 0))
    s, e, _ = F.shift_window("bad", "current", datetime(2026, 10, 30, 14, 0))
    assert s == datetime(2026, 10, 30, 14, 0)


def test_shift_summary_page(jm):
    j = jm.job(40, qty=300)["job"]
    c = client(jm, "Sam")
    c.post(f"/api/jobs/{j['id']}/rolls/1/printer", json={"printer": "Printer 3"})
    c.post(f"/api/jobs/{j['id']}/rolls/1", json={"status": "complete"})
    c.post(f"/api/jobs/{j['id']}/rolls/2", json={"status": "complete"})
    c.post(f"/api/jobs/{j['id']}/rolls/3", json={"status": "paused", "note": "ribbon out"})
    c.post(f"/api/jobs/{j['id']}/reprints", json={"roll_number": 1, "qty": 2, "reason": "Print defect"})
    db(jm, "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('shift_starts', '00:00')")
    d = c.get("/api/shift").get_json()
    assert d["rolls_done"] == 2 and d["labels_done"] == 100 and d["by_person"]["Sam"]["rolls"] == 2
    assert d["by_printer"]["Printer 3"]["rolls"] == 1 and d["reprinted_labels"] == 2
    assert d["open_rolls"][0]["roll_number"] == 3 and d["open_rolls"][0]["last_note"] == "ribbon out"
    html = c.get("/shift").get_data(as_text=True)
    assert "Shift summary" in html and "ribbon out" in html and "rolls 1–2" in html and "Print defect" in html
    assert "print()" in c.get("/shift?print=1").get_data(as_text=True)
    assert c.get("/shift?which=previous").status_code == 200


def test_shift_times_are_checked(jm):
    c = client(jm)
    assert "Nothing was saved" in c.post("/api/settings", json={"shift_starts": "6am"}).get_json()["error"]
    s = c.post("/api/settings", json={"shift_starts": "22:00, 6:00,14:00"}).get_json()["settings"]
    assert s["shift_starts"] == "06:00,14:00,22:00"


# ── new job like this ─────────────────────────────────────────────────────────
def test_repeat_job_copies_the_product_not_the_order(jm):
    j = jm.job(50, qty=1000)["job"]
    conn = jm.app.get_db()
    conn.execute("INSERT INTO btw_templates (name, file_path, active, created_at, updated_at) VALUES "
                 "('Acme 2x1', 'Z:/Master Templates/Acme/Acme 2x1.btw', 1, '2026-10-01', '2026-10-01')")
    conn.execute("UPDATE jobs SET template_path=?, label_size='2x1', po_number='PO9' WHERE id=?",
                 (f"Z:/Jobs/Print/{j['job_number']} - Acme 2x1.btw", j["id"]))
    conn.commit(); conn.close()
    r = client(jm).get(f"/api/jobs/{j['id']}/repeat").get_json()
    f = r["fields"]
    assert f["customer"] == "Acme" and f["upc"] == j["upc"] and f["lpr"] == 50 and f["label_size"] == "2x1"
    assert "job_ticket" not in f and "quantity" not in f and "po_number" not in f and "start_serial" not in f
    assert r["template"]["name"] == "Acme 2x1"
    db(jm, "UPDATE btw_templates SET active=0")
    r = client(jm).get(f"/api/jobs/{j['id']}/repeat").get_json()
    assert r["template"] is None and r["template_missing"] == "Acme 2x1.btw"
