"""
Reliability fixes ported from the earlier RFID Encoding Manager:
settings are checked before they are saved, the pages every browser polls never wait on a share,
a second copy can't start on a port that's in use, server errors are logged with a reference the
browser shows, and a page left open from before an update can't change data until it reloads.
"""
import os
import socket
import threading
import time


def client(jm, who="Brett", build=None):
    c = jm.app.app.test_client()
    c.environ_base["HTTP_X_OPERATOR"] = who
    if build:
        c.environ_base["HTTP_X_APP_BUILD"] = build
    return c


def raw_setting(jm, key):
    conn = jm.app.get_db()
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None


# ── settings ──────────────────────────────────────────────────────────────────
def test_a_bad_number_is_refused_and_nothing_is_saved(jm):
    c = client(jm)
    before = raw_setting(jm, "backup_keep")
    for bad in ("1.5", "abc", "0", "500", ""):
        r = c.post("/api/settings", json={"backup_interval_hours": bad, "backup_keep": "30"})
        assert r.status_code == 400, bad
        assert "Backup every (hours) must be a whole number from 1 to 168" in r.get_json()["error"]
    assert raw_setting(jm, "backup_interval_hours") is None and raw_setting(jm, "backup_keep") == before
    for path in ("/api/stamp", "/api/meta", "/api/backups", "/api/data/status"):
        assert c.get(path).status_code == 200, path

    r = c.post("/api/settings", json={"backup_interval_hours": "12", "default_overage_pct": "2.5", "backup_keep": 30})
    assert r.status_code == 200
    s = r.get_json()["settings"]
    assert s["backup_interval_hours"] == 12 and s["default_overage_pct"] == "2.5" and s["backup_keep"] == 30
    assert c.post("/api/settings", json={"default_overage_pct": "101"}).status_code == 400


def test_a_bad_value_already_in_the_database_cant_stop_the_app(jm):
    conn = jm.app.get_db()
    for k, v in (("backup_interval_hours", "1.5"), ("backup_keep", "lots"), ("auto_archive_hours", "-4"),
                 ("default_overage_pct", "x")):
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (k, v))
    conn.commit(); conn.close()
    s = jm.app.get_settings()
    assert (s["backup_interval_hours"], s["backup_keep"], s["auto_archive_hours"], s["default_overage_pct"]) == (6, 60, 32, "0")
    c = client(jm)
    for path in ("/api/stamp", "/api/meta", "/api/backups", "/api/data/status"):
        assert c.get(path).status_code == 200, path


def test_settings_changes_go_in_the_activity_log(jm):
    c = client(jm, who="Dana")
    c.post("/api/settings", json={"backup_interval_hours": 12, "printers": "Printer 1\nPrinter 9"})
    c.post("/api/settings", json={"backup_interval_hours": 12})               # no change: nothing logged
    conn = jm.app.get_db()
    rows = [dict(r) for r in conn.execute("SELECT * FROM activity WHERE kind='settings' ORDER BY id")]
    conn.close()
    assert len(rows) == 1 and rows[0]["actor"] == "Dana"
    assert "Backup every (hours) 6 → 12" in rows[0]["message"] and "printer list" in rows[0]["message"]


# ── shares ────────────────────────────────────────────────────────────────────
def test_polled_pages_never_wait_on_a_slow_share(jm, monkeypatch):
    c = client(jm)
    jm.app.check_shares()                       # what the background thread does every 15 s
    shares = (jm.config.SERIAL_REGISTRY_DIR, jm.backups, jm.customers)
    touched, real_isdir = [], os.path.isdir

    def slow_isdir(p):
        if any(str(p).startswith(s) for s in shares):
            touched.append(str(p)); time.sleep(2)
        return real_isdir(p)
    monkeypatch.setattr(os.path, "isdir", slow_isdir)
    jm.SS._health["t"] = 0.0                     # the old 10-second cache has run out

    times = []
    def poll():
        t = time.perf_counter(); r = c.get("/api/stamp"); times.append(time.perf_counter() - t); assert r.status_code == 200
    th = [threading.Thread(target=poll) for _ in range(8)]
    [t.start() for t in th]; [t.join() for t in th]
    assert max(times) < 1.0, times
    for path in ("/api/meta", "/api/serials/health", "/api/data/status"):
        t = time.perf_counter(); assert c.get(path).status_code == 200; assert time.perf_counter() - t < 1.0, path
    assert touched == []


def test_a_share_that_stops_answering_is_reported_not_waited_on(jm):
    c = client(jm)
    jm.app.check_shares()
    jm.SS._shares["checking_since"] = time.time() - 45       # a background check stuck on the share
    jm.app._share["checking_since"] = time.time() - 45
    try:
        h = c.get("/api/stamp").get_json()["serials"]
        assert h["registry_ok"] is False and "isn't answering" in h["registry_message"]
        assert c.get("/api/meta").get_json()["z_reachable"] is False
    finally:
        jm.SS._shares["checking_since"] = None
        jm.app._share["checking_since"] = None
    assert c.get("/api/stamp").get_json()["serials"]["registry_ok"] is True


# ── second copy ───────────────────────────────────────────────────────────────
def test_a_port_in_use_is_noticed(jm):
    app = jm.app
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1)
    port = s.getsockname()[1]
    try:
        assert app.port_in_use("127.0.0.1", port) and app.port_in_use("0.0.0.0", port)
    finally:
        s.close()
    assert not app.port_in_use("127.0.0.1", port)


# ── error log ─────────────────────────────────────────────────────────────────
def test_server_errors_are_logged_with_the_reference_the_page_shows(jm, monkeypatch, tmp_path):
    jm.app.setup_log(str(tmp_path / "logs"))
    c = client(jm, who="Priya")
    def broken():
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(jm.app, "_recent_names", broken)
    r = c.get("/api/meta")
    assert r.status_code == 500
    ref = r.get_json()["ref"]
    assert ref and r.headers["X-Request-ID"] == ref and f"(ref {ref})" in r.get_json()["error"]
    text = open(tmp_path / "logs" / "jobmanager.log", encoding="utf-8").read()
    assert f"ref={ref}" in text and "GET /api/meta" in text and "'Priya'" in text
    assert "RuntimeError: disk on fire" in text and "Traceback" in text

    monkeypatch.setattr(jm.app, "SLOW_REQUEST_SECONDS", 0.0)
    r = c.get("/api/stamp")
    assert f"slow ref={r.headers['X-Request-ID']}" in open(tmp_path / "logs" / "jobmanager.log", encoding="utf-8").read()
    jm.app.setup_log(str(tmp_path / "logs2"))      # let go of the first file


# ── pages left open across an update ──────────────────────────────────────────
def test_a_page_from_before_an_update_cant_change_data(jm):
    jid = jm.job(1)["job"]["id"]
    build = jm.app.BUILD
    assert build in jm.app.app.test_client().get("/").get_data(as_text=True)
    assert client(jm).get("/api/stamp").get_json()["build"] == build

    old = client(jm, build="0000000000")
    r = old.patch(f"/api/jobs/{jid}", json={"po_number": "PO-OLD"})
    assert r.status_code == 409 and r.get_json()["reload"] is True
    assert old.get(f"/api/jobs/{jid}").status_code == 200                  # reading still works
    assert client(jm).get(f"/api/jobs/{jid}").get_json()["job"]["po_number"] is None

    assert client(jm, build=build).patch(f"/api/jobs/{jid}", json={"po_number": "PO-NEW"}).status_code == 200
    assert client(jm).patch(f"/api/jobs/{jid}", json={"part_number": "P-1"}).status_code == 200   # no build sent: allowed
