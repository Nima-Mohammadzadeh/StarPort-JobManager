"""
Safe encoding files, the serial and database check, the host PC check, and database upkeep
(ported ideas from the earlier RFID Encoding Manager's V0.80.2 / V1.7 / V1.8.2 / V1.9 releases).
"""
import os
import time


def client(jm, who="Brett"):
    c = jm.app.app.test_client()
    c.environ_base["HTTP_X_OPERATOR"] = who
    return c


def db(jm, sql, args=()):
    conn = jm.app.get_db()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(sql, args)
    conn.commit(); conn.close()


def kinds(r):
    return sorted({p["kind"] for p in r["problems"]})


def run_check(jm):
    conn = jm.app.get_db()
    try:
        return jm.app.integrity.run(conn)
    finally:
        conn.close()


# ── encoding files ────────────────────────────────────────────────────────────
def test_encoding_files_are_checked_before_they_get_their_names(jm, tmp_path):
    G, upc = jm.app.__dict__["generate_db_files"].__globals__, jm.upc(1)
    r = G["generate_db_files"](str(tmp_path / "job"), upc, 26_000_000_001, 2_500, 1_000)
    data = tmp_path / "job" / "Data"
    assert sorted(os.listdir(data)) == [f"{upc}_DB1.xlsx", f"{upc}_DB2.xlsx", f"{upc}_DB3.xlsx"]
    G["verify_db_file"](r["files"][2], upc, 26_000_002_001, 500)


def test_an_existing_file_is_never_overwritten(jm, tmp_path):
    G, upc = jm.app.generate_db_files.__globals__, jm.upc(2)
    G["generate_db_files"](str(tmp_path / "job"), upc, 1_000, 100, None)
    path = tmp_path / "job" / "Data" / f"{upc}.xlsx"
    before = path.read_bytes()
    try:
        G["generate_db_files"](str(tmp_path / "job"), upc, 5_000, 100, None)
        raise AssertionError("should have refused")
    except FileExistsError as e:
        assert "left as it is" in str(e)
    assert path.read_bytes() == before and os.listdir(tmp_path / "job" / "Data") == [f"{upc}.xlsx"]


def test_a_file_that_fails_its_check_leaves_nothing_behind(jm, tmp_path, monkeypatch):
    G, upc = jm.app.generate_db_files.__globals__, jm.upc(3)
    real = G["verify_db_file"]
    calls = []

    def flaky(path, *a, **k):
        calls.append(path)
        if len(calls) == 2:                                 # the second file comes back damaged
            raise ValueError("DB2: row 7 is wrong")
        return real(path, *a, **k)
    monkeypatch.setitem(G, "verify_db_file", flaky)
    try:
        G["generate_db_files"](str(tmp_path / "job"), upc, 1, 300, 100)
        raise AssertionError("should have failed")
    except ValueError:
        pass
    assert os.listdir(tmp_path / "job" / "Data") == []     # not even the first, good file
    assert all(os.path.basename(p).startswith(".writing-") for p in calls)


def test_a_damaged_file_is_caught(jm, tmp_path):
    G, upc = jm.app.generate_db_files.__globals__, jm.upc(4)
    recs = [(s, upc, G["generate_epc"](upc, s)) for s in range(100, 600)]
    recs[321] = (recs[321][0], upc, "30340000000000000000ABCD")
    G["_write_xlsx"](str(tmp_path / "x.xlsx"), recs, "Database")
    try:
        G["verify_db_file"](str(tmp_path / "x.xlsx"), upc, 100, 500)
        raise AssertionError("should have failed")
    except ValueError as e:
        assert "row 323" in str(e)


def test_a_new_job_still_gets_its_database(jm):
    res = jm.job(5, qty=1000)
    data = os.path.join(res["folder_path"], "Data")
    assert [f for f in os.listdir(data) if f.endswith(".xlsx")] and not [f for f in os.listdir(data) if f.startswith(".")]
    assert not [w for w in res["warnings"] if "database" in w.lower()]


# ── serial and database check ─────────────────────────────────────────────────
def test_clean_data_adds_up(jm):
    for n in range(10, 14):
        jm.job(n, qty=1000)
    r = run_check(jm)
    assert r["ok"], r["problems"]
    assert r["jobs_checked"] == 4 and r["ranges_checked"] >= 4


def test_a_roll_that_repeats_serials_is_a_problem(jm):
    j = jm.job(20, qty=1000)["job"]
    db(jm, "UPDATE job_rolls SET serial_start=serial_start-10, serial_end=serial_end-10 WHERE job_id=? AND roll_number=2", (j["id"],))
    r = run_check(jm)
    p = [p for p in r["problems"] if p["job_id"] == j["id"]][0]
    assert p["level"] == "bad" and "repeats serials" in p["detail"] and "EPCs don't match" in p["detail"]


def test_missing_rolls_and_missing_ranges_are_reported_not_passed(jm):
    a = jm.job(21, qty=1000)["job"]
    db(jm, "DELETE FROM job_rolls WHERE job_id=? AND roll_number=(SELECT MAX(roll_number) FROM job_rolls WHERE job_id=?)", (a["id"], a["id"]))
    b = jm.job(22, qty=1000)["job"]
    db(jm, "DELETE FROM serial_allocations WHERE job_number=?", (b["job_number"],))
    c = jm.job(23, qty=1000)["job"]
    db(jm, "UPDATE jobs SET start_serial=NULL WHERE id=?", (c["id"],))
    r = run_check(jm)
    by = {p["job_id"]: p for p in r["problems"]}
    assert by[a["id"]]["level"] == "warn" and "labels have no roll" in by[a["id"]]["detail"]
    assert by[b["id"]]["level"] == "warn" and "no serial range is recorded" in by[b["id"]]["detail"]
    assert by[c["id"]]["level"] == "warn" and "can't be checked" in by[c["id"]]["detail"]
    assert not r["ok"]


def test_overlapping_ranges_are_found(jm):
    a = jm.job(30, qty=1000)
    upc = a["job"]["upc"]
    b = jm.job(31, qty=1000, upc=upc)
    # Pretend b was given a's serials (as if a registry had been rolled back).
    db(jm, 'UPDATE serial_allocations SET start=?, "end"=? WHERE job_number=?',
       (a["job"]["start_serial"], a["job"]["start_serial"] + 999, b["job_number"]))
    db(jm, "UPDATE jobs SET start_serial=? WHERE id=?", (a["job"]["start_serial"], b["job"]["id"]))
    r = run_check(jm)
    ov = [p for p in r["problems"] if p["kind"] == "overlap"]
    assert ov and ov[0]["level"] == "bad" and a["job_number"] in ov[0]["detail"] and b["job_number"] in ov[0]["detail"]
    assert r["problems"][0]["kind"] == "overlap"                 # worst first


def test_a_reviewed_conflict_is_not_reported_again(jm):
    a = jm.job(32, qty=1000)
    upc = a["job"]["upc"]
    db(jm, """INSERT INTO serial_allocations (upc, job_number, start, "end", qty, note, created_at, registry_state)
              VALUES (?, 'RESERVED', ?, ?, 10, 'held; conflict reviewed by Dana: checked, never printed', '2026-10-01', 1)""",
       (upc, a["job"]["start_serial"], a["job"]["start_serial"] + 9))
    assert "overlap" not in kinds(run_check(jm))


def test_orphans_and_problems_reach_needs_attention(jm):
    j = jm.job(40, qty=1000)["job"]
    db(jm, "INSERT INTO job_rolls (job_id, roll_number, quantity, status, created_at) VALUES (99999, 1, 10, 'pending', '2026-10-01')")
    db(jm, "UPDATE job_rolls SET epc_end='3034DEADBEEF' WHERE job_id=? AND roll_number=1", (j["id"],))
    c = client(jm)
    assert c.get("/api/integrity").get_json()["result"] is None
    r = c.post("/api/integrity").get_json()["result"]
    assert "database" in kinds(r) and "serial" in kinds(r)
    att = c.get("/api/attention").get_json()
    mine = [it for it in att["items"] if it["job"]["id"] == j["id"]][0]
    assert mine["level"] == "bad" and any(i["kind"] == "serial_check" for i in mine["issues"])
    assert any(g["kind"] == "data_check" and "belong to no job" in g["detail"] for g in att["global"])
    conn = jm.app.get_db()
    assert conn.execute("SELECT COUNT(*) FROM activity WHERE kind='check'").fetchone()[0] == 1
    conn.close()


# ── host PC check ─────────────────────────────────────────────────────────────
def test_host_check_from_the_tools_page(jm):
    r = client(jm).get("/api/tools/host-check").get_json()
    st = {x["name"]: x["status"] for x in r["results"]}
    assert st["App database"] == "PASS" and st["Job folders (Z:)"] == "PASS" and st["Serial registry"] == "PASS"
    assert st["Serial and database check"] == "PASS" and st["Job Manager running"] == "PASS"
    assert st["Master Templates"] == "FAIL"                     # the test install has no Z: drive
    assert r["status"] == "FAIL" and r["critical_failures"] >= 1
    text = open(r["saved_to"], encoding="utf-8").read()
    assert "Overall: FAIL" in text and "[FAIL] Master Templates" in text
    assert os.path.isfile(r["saved_to"][:-4] + ".json")


def test_a_check_that_hangs_is_cut_off(jm, monkeypatch):
    H = jm.app.H
    monkeypatch.setattr(H, "AUTOMATIC", [H.check_config, lambda: time.sleep(5)])
    t = time.time()
    out = H.run_automatic(include_server=False, timeout=1)
    assert time.time() - t < 3
    assert out[0]["status"] in ("PASS", "WARN") and out[1]["status"] == "FAIL" and "Didn't finish" in out[1]["detail"]


def test_unknown_is_not_a_pass(jm):
    H = jm.app.H
    assert H.overall([H._res("a", "PASS", ""), H._res("b", "SKIP", "")]) == "INCOMPLETE"
    assert H.overall([H._res("a", "PASS", ""), H._res("b", "SKIP", "", critical=False)]) == "PASS"
    assert H.overall([H._res("a", "FAIL", ""), H._res("b", "SKIP", "")]) == "FAIL"


def test_a_database_in_onedrive_is_flagged(jm, monkeypatch, tmp_path):
    H = jm.app.H
    od = tmp_path / "OneDrive - Multi-Color Corporation" / "Desktop"
    od.mkdir(parents=True)
    import shutil
    shutil.copy(jm.config.DB_PATH, od / "local.db")
    monkeypatch.setattr(H.C, "DB_PATH", str(od / "local.db"))
    r = H.check_database()
    assert r["status"] == "FAIL" and "OneDrive" in r["detail"]


def test_jm_tools_check_writes_a_report(jm, capsys):
    import sys
    tools = sys.modules.get("jm_tools")
    if tools is None:
        sys.path.insert(0, jm.root)
        try:
            import jm_tools as tools
        finally:
            sys.path.remove(jm.root)
    before = set(os.listdir(jm.app.H.REPORT_DIR)) if os.path.isdir(jm.app.H.REPORT_DIR) else set()
    code = tools.cmd_check(auto=True)
    assert code == 1                                            # no Z: drive and no running server in the test
    new = set(os.listdir(jm.app.H.REPORT_DIR)) - before
    assert any(f.endswith(".txt") for f in new) and "Overall: FAIL" in capsys.readouterr().out


# ── database upkeep ───────────────────────────────────────────────────────────
def test_upkeep_runs_after_a_good_backup_and_from_tools(jm):
    jm.job(50, qty=500)
    assert jm.app.make_backup()["success"]
    last = jm.app._upkeep_state["last"]
    assert last and last["optimized"] and "checkpoint" in last
    r = client(jm).post("/api/tools/db-upkeep").get_json()
    assert r["optimized"] and r["checkpoint"]["busy"] in (0, 1)
