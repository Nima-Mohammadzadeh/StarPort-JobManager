"""Floor page for Zebra handhelds: encoder names, config, links."""


def client(jm):
    return jm.app.app.test_client()


def save(jm, **settings):
    r = client(jm).post("/api/settings", json=settings)
    assert r.status_code == 200
    return r.get_json()


# ── Task 1: encoder list and config ───────────────────────────────────────────
def test_clean_encoders_trims_dedupes_and_caps(jm):
    clean = jm.app.clean_encoders
    assert clean(" Sam, sam\nAnn;;  Lee   Ross \n") == "Sam\nAnn\nLee Ross"
    assert len(clean("\n".join(f"Name{i}" for i in range(50))).split("\n")) == 40
    assert clean("x" * 60) == "x" * 40


def test_encoders_default_empty_and_saved_through_settings(jm):
    assert jm.app.get_settings()["encoders"] == ""
    save(jm, encoders="Sam\nAnn")
    assert jm.app.get_settings()["encoders"] == "Sam\nAnn"


def test_floor_config_lists_encoders_and_printers(jm):
    cfg = client(jm).get("/api/floor/config").get_json()
    assert cfg["encoders"] == []
    save(jm, encoders="Sam\nAnn")
    cfg = client(jm).get("/api/floor/config").get_json()
    assert cfg["encoders"] == ["Sam", "Ann"]
    assert cfg["printers"] == ["Printer 1", "Printer 2", "Printer 3", "Printer 4"]


# ── Task 2: link, QR and the /floor route ─────────────────────────────────────
def test_rewind_link_unchanged(jm):
    save(jm, rewind_base_url="http://10.0.0.5:5050")
    assert client(jm).get("/api/rewind/link").get_json()["url"] == "http://10.0.0.5:5050/rewind"


def test_floor_link_uses_same_base(jm):
    save(jm, rewind_base_url="http://10.0.0.5:5050")
    assert client(jm).get("/api/floor/link").get_json()["url"] == "http://10.0.0.5:5050/floor"


def test_floor_page_served(jm):
    r = client(jm).get("/floor")
    assert r.status_code == 200
    assert r.mimetype == "text/html"
    assert b'id="scan"' in r.data


def test_floor_qr_routes(jm):
    save(jm, rewind_base_url="http://10.0.0.5:5050")
    svg = client(jm).get("/floor/qr.svg")
    assert svg.status_code == 200 and svg.mimetype == "image/svg+xml"
    sign = client(jm).get("/floor/qr")
    assert sign.status_code == 200
    assert b"FLOOR" in sign.data and b"http://10.0.0.5:5050/floor" in sign.data


# ── Task 5: the roll card needs to know each roll's printer ───────────────────
def test_rewind_job_rolls_include_printer(jm):
    job = jm.job(1, qty=3000)
    jid = job["job"]["id"]
    r = client(jm).post(f"/api/jobs/{jid}/rolls/1/printer", json={"printer": "Printer 2", "operator": "Sam"})
    assert r.status_code == 200
    rolls = client(jm).get(f"/api/rewind/jobs/{jid}").get_json()["rolls"]
    assert rolls[0]["printer"] == "Printer 2"
    assert rolls[1]["printer"] is None


# ── Scan or type a job ticket (2026-11-16) ────────────────────────────────────
def lookup(jm, raw):
    return client(jm).post("/api/rewind/lookup", json={"raw": raw, "job_id": ""}).get_json()


def test_lookup_ticket_opens_job(jm):
    a = jm.job(1, ticket="40133")["job"]
    b = jm.job(2, ticket="AB-77")["job"]
    for raw in ("40133", " 40133 "):
        r = lookup(jm, raw)
        assert r["result"] == "job" and r["job_id"] == a["id"] and r["job_ticket"] == "40133"
        assert r["customer"] == "Acme" and r["message"] == ""
    assert lookup(jm, "ab-77")["job_id"] == b["id"]


def test_lookup_ticket_completed_job_opens(jm):
    a = jm.job(1, ticket="40133")["job"]
    assert client(jm).patch(f"/api/jobs/{a['id']}", json={"status": "Complete"}).status_code == 200
    r = lookup(jm, "40133")
    assert r["result"] == "job" and r["status"] == "Complete"


def test_lookup_ticket_archived(jm):
    a = jm.job(1, ticket="40133")["job"]
    assert client(jm).post(f"/api/jobs/{a['id']}/archive", json={}).status_code == 200
    r = lookup(jm, "40133")
    assert r["result"] == "job_archived"
    assert r["message"] == "Ticket 40133 is archived. Restore it in Job Manager first."


def test_lookup_ticket_ignores_cancelled(jm):
    old = jm.job(1, ticket="40133")["job"]
    assert client(jm).patch(f"/api/jobs/{old['id']}", json={"status": "Cancelled"}).status_code == 200
    new = jm.job(2, ticket="40133")["job"]
    assert lookup(jm, "40133")["job_id"] == new["id"]


def test_lookup_unknown_ticket_keeps_tag_message(jm):
    r = lookup(jm, "4013")
    assert r["result"] == "invalid"
    assert r["message"].startswith("No job with ticket 4013. If that was a tag: That is 4 characters")


def test_lookup_tag_unchanged(jm):
    a = jm.job(1, qty=200)["job"]
    roll = client(jm).get(f"/api/rewind/jobs/{a['id']}").get_json()["rolls"][0]
    r = lookup(jm, roll["epc_start"])
    assert r["result"] == "found" and r["roll"]["roll_number"] == 1
    jm.job(3, ticket=roll["epc_start"])                       # a ticket that happens to equal a tag's text
    assert lookup(jm, roll["epc_start"])["result"] == "found"


def test_lookup_text_that_cannot_be_a_tag_says_only_no_job(jm):
    r = lookup(jm, "acme")
    assert r["result"] == "invalid" and r["message"] == "No job with ticket acme."
