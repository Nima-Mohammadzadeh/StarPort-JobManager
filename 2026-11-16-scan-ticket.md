# Scan or Type a Job Ticket Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A job ticket scanned on the Zebra floor page, or typed on the rewind page, opens that job; tag scans keep working exactly as before.

**Architecture:** The shared lookup `_lookup(conn, raw, job_id)` in `app.py` (behind `POST /api/rewind/lookup`) gains a ticket branch that runs only when the value is not a valid 24-hex EPC. The floor page and the rewind page each handle two new results.

**Tech Stack:** Flask, SQLite, vanilla JS; pytest; Playwright browser checks against the demo sandbox.

**Spec:** `docs/superpowers/specs/2026-11-16-scan-ticket-design.md`

## Global Constraints

- A valid 24-hex EPC is looked up exactly as today; the ticket branch never runs for one.
- Ticket match: trimmed, case ignored, cancelled jobs excluded (as `_same_ticket` does).
- Result `job`: keys `job_id`, `job_ticket`, `job_number`, `customer`, `title`, `status`, plus `message: ""`.
- Result `job_archived`: message exactly `Ticket <ticket> is archived. Restore it in Job Manager first.`
- No match and the value is not a tag: today's message, prefixed `No job with ticket <value>. If that was a tag: `.
- Floor: `job` clears the roll card and opens `#/browse/<id>` with the line `Scan a tag or tap a roll`.
- Rewind: Enter in the job-list search box asks the lookup when the value is not a tag; typing without Enter only filters.
- README Zebra note: Barcode Input on in the DataWedge profile for traveler barcodes.

## Review Focus

1. A ticket that is all hex digits (for example `40133`) must not be treated as a broken tag before the ticket match. Pinned in Task 1.
2. Two jobs share a ticket (only possible with a cancelled one): the non-cancelled job opens. Pinned in Task 1.
3. A ticket scanned on the floor while a roll card is open: the card clears so no button acts on the old roll. Pinned in Task 2.
4. The rewind search box filtering must not change while typing; only Enter looks up. Pinned in Task 3.
5. A scan of a ticket inside a rewind job view for a different job opens the other job instead of showing "wrong product". Pinned in Task 3.

---

### Task 1: Lookup answers tickets

**Files:** Modify `app.py` (`_lookup`, about line 1690); Test `tests/test_floor_page.py`.

**Interfaces:** Produces the `job` and `job_archived` results above from `POST /api/rewind/lookup`.

- [ ] **Step 1: Write the failing tests** (use `jm.job(n, ticket=...)` and `client(jm)`):
  - `test_lookup_ticket_opens_job`: job with ticket `40133`; lookups of `40133`, ` 40133 ` and (for ticket `AB-77`) `ab-77` return `result == "job"` with the right `job_id`, `job_ticket`, `customer`.
  - `test_lookup_ticket_completed_job_opens`: the job's status set to `Complete` through `PATCH /api/jobs/<id>` still returns `job`.
  - `test_lookup_ticket_archived`: after `POST /api/jobs/<id>/archive`, result `job_archived` and message `Ticket 40133 is archived. Restore it in Job Manager first.`
  - `test_lookup_ticket_ignores_cancelled`: two jobs, the older set to `Cancelled` then a new one created with the same ticket; the lookup returns the non-cancelled job's id.
  - `test_lookup_unknown_ticket_keeps_tag_message`: `4013` returns `result == "invalid"` and the message starts with `No job with ticket 4013. If that was a tag: That is 4 characters`.
  - `test_lookup_tag_unchanged`: a roll's `epc_start` still returns `found`, and with a ticket equal to that EPC's text nothing changes.
- [ ] **Step 2: Run** `python -m pytest -q tests/test_floor_page.py -p no:cacheprovider -o tmp_path_retention_policy=none`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** in `_lookup`: after cleaning, if the value is not exactly 24 hex characters, call `_same_ticket(conn, raw)`; pick the newest non-archived job, else report the archived one; with no job, fall through to the existing checks and prefix their message as above.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `app.py` and the test file: "Lookup: a job ticket opens its job".

### Task 2: Floor page opens a job from a ticket

**Files:** Modify `templates/floor.html`.

**Interfaces:** Consumes Task 1's results.

- [ ] **Step 1: Browser check** (scratchpad script, 360x640, sandbox): type `40133` and press Enter on the scan screen with a roll card open → hash is `#/browse/<id>`, the line `Scan a tag or tap a roll` shows, `S.cur` is null; type `40133` with no Enter → same after 1.5 s; `99999` + Enter → red `No job with ticket 99999`; an archived job's ticket → red archived message; a tag still opens its roll. Run it. Expected: FAIL.
- [ ] **Step 2: Implement** in `lookup()`: `job` → clear the card (as `rejectScan` does, without a message), set a one-line hint `Scan a tag or tap a roll` above the roll list, go to `#/browse/<job_id>`; `job_archived` → `rejectScan(message)`.
- [ ] **Step 3: Run** the check and the earlier scan-first checks. Expected: PASS.
- [ ] **Step 4: Commit** "Floor page: scan a traveler ticket to open the job".

### Task 3: Rewind page opens a job from a typed ticket; docs; screenshots

**Files:** Modify `templates/rewind.html`, `README.md`, `.claude/skills/encoding-floor-sandbox/SKILL.md`.

- [ ] **Step 1: Browser check** (tablet 800x1280): typing `401` in the list search filters without opening anything; `40133` + Enter opens `#/job/<id>`; `99999` + Enter shows the message under the box and the list stays; inside job 40121's view, typing `40133` + Enter in the scan box opens job 40133; a tag still works in both boxes. Run it. Expected: FAIL.
- [ ] **Step 2: Implement:** the search box's Enter handler calls the lookup with `job_id: ''` when the value is not a tag; the scan box's lookup handles `job` by going to that job; `job_archived` and no match show the message where messages already show.
- [ ] **Step 3: Docs:** README floor-page bullet: scanning the traveler barcode opens the job (Barcode Input must be on in DataWedge); rewind bullet: type a ticket and press Enter. Skill: add both checks.
- [ ] **Step 4: Run** the check, then the full suite with `-p no:cacheprovider -o tmp_path_retention_policy=none --basetemp=<scratch>`. Expected: PASS.
- [ ] **Step 5: Screenshots** for the user: floor ticket scan → job, unknown ticket, archived ticket; rewind typed ticket → job, unknown ticket.
- [ ] **Step 6: Commit** "Rewind page: type a ticket to open the job; docs".
