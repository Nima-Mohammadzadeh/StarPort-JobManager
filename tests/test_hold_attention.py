"""
Hold reasons and the needs-attention list.

A job only goes on hold with a reason (category + what it is waiting for); the reason is shown
wherever the job is, can be changed while the job is held, and is cleared (but kept in the
activity log) when the job resumes. The needs-attention list collects open jobs that are overdue,
on hold, finished-but-not-marked, left with rolls in progress, missing a folder or template, or
caught in a serial conflict. Folder checks happen in the background, never inside a request.
"""
import os
import shutil
import time
from datetime import date, datetime, timedelta

HOLD = {"status": "On Hold", "hold_reason": "Material", "hold_note": "Waiting on Monza R6 inlays"}


def client(jm, who="Brett"):
    c = jm.app.app.test_client()
    c.environ_base["HTTP_X_OPERATOR"] = who
    return c


def job_row(jm, job_id):
    conn = jm.app.get_db()
    row = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    conn.close()
    return row


def activity(jm, job_id):
    conn = jm.app.get_db()
    rows = [dict(r) for r in conn.execute("SELECT * FROM activity WHERE job_id=? ORDER BY id", (job_id,))]
    conn.close()
    return rows


def kinds(att, job_id):
    for it in att["items"]:
        if it["job"]["id"] == job_id:
            return [i["kind"] for i in it["issues"]]
    return []


def issue(att, job_id, kind):
    for it in att["items"]:
        if it["job"]["id"] == job_id:
            for i in it["issues"]:
                if i["kind"] == kind:
                    return i
    return None


# ── hold reasons ──────────────────────────────────────────────────────────────
def test_putting_a_job_on_hold_needs_a_reason_and_a_note(jm):
    c = client(jm)
    jid = jm.job(1)["job"]["id"]

    r = c.patch(f"/api/jobs/{jid}", json={"status": "On Hold"})
    assert r.status_code == 400 and "why" in r.get_json()["error"]
    r = c.patch(f"/api/jobs/{jid}", json={"status": "On Hold", "hold_reason": "Lunch", "hold_note": "back soon"})
    assert r.status_code == 400
    r = c.patch(f"/api/jobs/{jid}", json={"status": "On Hold", "hold_reason": "Material", "hold_note": "x"})
    assert r.status_code == 400 and "waiting for" in r.get_json()["error"]
    assert job_row(jm, jid)["status"] == "Pending"          # nothing changed by the refused attempts

    r = c.patch(f"/api/jobs/{jid}", json={**HOLD, "hold_reason": "material", "hold_note": "  Waiting on   Monza R6 inlays "})
    assert r.status_code == 200
    j = job_row(jm, jid)
    assert j["status"] == "On Hold" and j["hold_reason"] == "Material"
    assert j["hold_note"] == "Waiting on Monza R6 inlays" and j["hold_by"] == "Brett" and j["hold_since"]
    last = activity(jm, jid)[-1]
    assert last["kind"] == "status" and "On Hold: Material, Waiting on Monza R6 inlays" in last["message"]
    assert last["actor"] == "Brett"


def test_hold_reason_can_change_while_held_and_is_cleared_on_resume(jm):
    c = client(jm)
    jid = jm.job(2)["job"]["id"]
    assert c.patch(f"/api/jobs/{jid}", json=HOLD).status_code == 200
    since = job_row(jm, jid)["hold_since"]

    r = c.patch(f"/api/jobs/{jid}", json={"hold_reason": "Customer", "hold_note": "Waiting on art approval"})
    assert r.status_code == 200
    j = job_row(jm, jid)
    assert (j["hold_reason"], j["hold_note"], j["hold_since"]) == ("Customer", "Waiting on art approval", since)
    assert activity(jm, jid)[-1]["kind"] == "hold"

    # Editing something else while held doesn't ask for the reason again.
    assert c.patch(f"/api/jobs/{jid}", json={"po_number": "PO-9"}).status_code == 200
    assert job_row(jm, jid)["hold_reason"] == "Customer"

    r = c.patch(f"/api/jobs/{jid}", json={"status": "In Progress"})
    assert r.status_code == 200
    j = job_row(jm, jid)
    assert j["status"] == "In Progress" and j["hold_reason"] is None and j["hold_note"] is None and j["hold_since"] is None
    assert "was on hold" in activity(jm, jid)[-1]["message"] and "for Customer" in activity(jm, jid)[-1]["message"]

    r = c.patch(f"/api/jobs/{jid}", json={"hold_reason": "Material", "hold_note": "nope"})
    assert r.status_code == 400 and "isn't on hold" in r.get_json()["error"]


def test_hold_reason_reaches_the_floor_and_rewind_pages(jm):
    c = client(jm)
    jid = jm.job(3)["job"]["id"]
    c.patch(f"/api/jobs/{jid}", json=HOLD)
    assert "Material" in c.get("/api/meta").get_json()["hold_reasons"]

    floor = {j["id"]: j for j in c.get("/api/floor").get_json()["jobs"]}
    assert floor[jid]["hold_reason"] == "Material" and floor[jid]["hold_note"] == HOLD["hold_note"]
    rew = {j["id"]: j for j in c.get("/api/rewind/jobs?view=open").get_json()["jobs"]}
    assert rew[jid]["hold_reason"] == "Material"
    one = c.get(f"/api/rewind/jobs/{jid}").get_json()["job"]
    assert one["hold_note"] == HOLD["hold_note"] and one["hold_by"] == "Brett"
    detail = c.get(f"/api/jobs/{jid}").get_json()["job"]
    assert detail["hold_reason"] == "Material"


def test_jobs_already_on_hold_before_the_upgrade_keep_working(jm):
    c = client(jm)
    jid = jm.job(4)["job"]["id"]
    conn = jm.app.get_db()          # on hold the old way: no reason, just the status and a log line
    conn.execute("UPDATE jobs SET status='On Hold' WHERE id=?", (jid,))
    conn.execute("INSERT INTO activity (ts, job_id, kind, actor, message) VALUES "
                 "('2026-10-01 09:30:00', ?, 'status', 'Old', 'Acme T4 set to On Hold')", (jid,))
    conn.commit(); conn.close()
    jm.app.init_db()

    j = job_row(jm, jid)
    assert j["hold_since"] == "2026-10-01 09:30:00" and j["hold_reason"] is None
    att = c.get("/api/attention").get_json()
    assert issue(att, jid, "on_hold")["detail"].startswith("No reason recorded (")
    assert c.patch(f"/api/jobs/{jid}", json={"part_number": "P-1"}).status_code == 200
    assert c.patch(f"/api/jobs/{jid}", json={"hold_reason": "Printer", "hold_note": "Printer 3 down"}).status_code == 200
    assert job_row(jm, jid)["hold_reason"] == "Printer"


# ── needs attention ───────────────────────────────────────────────────────────
def test_attention_lists_what_needs_someone_and_nothing_else(jm):
    c = client(jm)
    today = date.today()
    yesterday, tomorrow = today - timedelta(days=1), today + timedelta(days=1)
    late = jm.job(10)["job"]["id"]
    c.patch(f"/api/jobs/{late}", json={"due_date": yesterday.isoformat()})
    soon = jm.job(11)["job"]["id"]
    c.patch(f"/api/jobs/{soon}", json={"due_date": tomorrow.strftime("%m/%d/%y")})        # typed MM/DD/YY
    later = jm.job(12)["job"]["id"]
    c.patch(f"/api/jobs/{later}", json={"due_date": (today + timedelta(days=9)).strftime("%m/%d/%Y")})
    held = jm.job(13)["job"]["id"]
    c.patch(f"/api/jobs/{held}", json=HOLD)
    done = jm.job(14)["job"]["id"]
    c.post(f"/api/jobs/{done}/rolls/bulk", json={"roll_numbers": [1, 2, 3], "status": "complete", "operator": "Ann"})
    early = jm.job(15)["job"]["id"]
    c.patch(f"/api/jobs/{early}", json={"status": "Complete", "operator": "Ann"})
    stale = jm.job(16)["job"]["id"]
    c.post(f"/api/jobs/{stale}/rolls/1/printer", json={"printer": "Printer 2", "operator": "Ann"})
    c.post(f"/api/jobs/{stale}/rolls/2/printer", json={"printer": "Printer 3", "operator": "Ann"})
    old = (datetime.now() - timedelta(hours=13)).strftime("%Y-%m-%d %H:%M:%S")
    conn = jm.app.get_db()
    conn.execute("UPDATE job_rolls SET started_at=? WHERE job_id=? AND roll_number=1", (old, stale))
    conn.commit(); conn.close()
    cancelled = jm.job(17)["job"]["id"]
    c.patch(f"/api/jobs/{cancelled}", json={"due_date": yesterday.isoformat(), "status": "Cancelled"})
    archived = jm.job(18)["job"]["id"]
    c.patch(f"/api/jobs/{archived}", json={"due_date": yesterday.isoformat()})
    c.post(f"/api/jobs/{archived}/archive", json={})
    fine = jm.job(19)["job"]["id"]

    att = c.get("/api/attention").get_json()
    assert kinds(att, late) == ["overdue"] and "1 day late" in issue(att, late, "overdue")["detail"]
    assert kinds(att, soon) == ["due_soon"] and "tomorrow" in issue(att, soon, "due_soon")["detail"]
    assert kinds(att, held) == ["on_hold"]
    assert issue(att, held, "on_hold")["detail"] == "Material: Waiting on Monza R6 inlays (under a minute)"
    assert kinds(att, done) == ["rolls_done"]
    assert kinds(att, early) == ["complete_open_rolls"] and "3 of 3" in issue(att, early, "complete_open_rolls")["detail"]
    st = issue(att, stale, "stale_rolls")
    assert st and "Roll 1" in st["detail"] and "Printer 2" in st["detail"] and "13 h" in st["detail"]
    assert "more" not in st["detail"]                       # roll 2 was started just now
    for jid in (later, cancelled, archived, fine):
        assert kinds(att, jid) == []
    levels = [it["level"] for it in att["items"]]
    assert levels == sorted(levels, key=lambda lv: lv != "bad")      # problems first, then things to look at
    assert att["counts"]["overdue"] == 1 and att["bad"] == 2


def test_folder_issues_come_from_the_background_check_never_the_request(jm, monkeypatch):
    c = client(jm)
    gone = jm.job(20)
    has_tpl = jm.job(21)
    os.makedirs(os.path.join(has_tpl["folder_path"], "Print"), exist_ok=True)
    open(os.path.join(has_tpl["folder_path"], "Print", "Acme - label.btw"), "w").close()
    shutil.rmtree(gone["folder_path"])
    gid, tid = gone["job"]["id"], has_tpl["job"]["id"]

    touched = []
    real_isdir, real_scandir = os.path.isdir, os.scandir
    monkeypatch.setattr(os.path, "isdir", lambda p: touched.append(str(p)) or real_isdir(p))
    monkeypatch.setattr(os, "scandir", lambda p=".": touched.append(str(p)) or real_scandir(p))
    att = c.get("/api/attention").get_json()
    assert not [p for p in touched if p.startswith(jm.customers)], "the request looked at the share"
    assert kinds(att, gid) == [] and att["folders_checked_at"] is None   # not checked yet: nothing claimed

    assert jm.app.check_job_folders()
    att = c.get("/api/attention").get_json()
    assert "folder_missing" in kinds(att, gid)
    assert kinds(att, tid) == []                                          # template copied by hand counts
    other = jm.job(22)
    jm.app.check_job_folders()
    att = c.get("/api/attention").get_json()
    assert kinds(att, other["job"]["id"]) == ["no_template"]


def test_unreachable_share_is_one_issue_not_one_per_job(jm, monkeypatch):
    c = client(jm)
    for n in (30, 31, 32):
        jm.job(n)
    monkeypatch.setattr(jm.app, "Z_DRIVE_BASE", os.path.join(jm.root, "no-such-share"))
    jm.app.check_job_folders()
    att = c.get("/api/attention").get_json()
    assert [g["label"] for g in att["global"]] == ["Job folders not reachable"]
    assert not any(k in ("folder_missing", "no_template") for it in att["items"] for k in [i["kind"] for i in it["issues"]])


def test_serial_conflicts_are_listed(jm):
    c = client(jm)
    j = jm.job(40)
    conn = jm.app.get_db()
    conn.execute("""INSERT INTO serial_allocations (upc, job_number, start, "end", qty, created_at, registry_state)
                    VALUES (?, ?, 5000, 5099, 100, '2026-10-01 08:00:00', -1)""", (j["job"]["upc"], j["job_number"]))
    conn.execute("""INSERT INTO serial_allocations (upc, job_number, start, "end", qty, created_at, registry_state)
                    VALUES (?, 'RESERVED', 7000, 7009, 10, '2026-10-01 08:00:00', -1)""", (j["job"]["upc"],))
    conn.commit(); conn.close()
    att = c.get("/api/attention").get_json()
    assert "5,000–5,099" in issue(att, j["job"]["id"], "serial_conflict")["detail"]
    assert any(g["kind"] == "serial_conflict" and "RESERVED" in g["label"] for g in att["global"])
    assert att["items"][0]["job"]["id"] == j["job"]["id"]                # serial conflicts come first


def test_refresh_starts_a_folder_check_without_waiting_for_it(jm):
    c = client(jm)
    jm.job(50)
    assert c.get("/api/attention?refresh=1").status_code == 200
    for _ in range(50):
        if jm.app._folders["at"]:
            break
        time.sleep(0.05)
    assert jm.app._folders["at"] and jm.app._folders["base_ok"] is True
