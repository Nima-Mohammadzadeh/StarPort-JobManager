# Floor Page for Zebra RFID Handhelds Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `/floor` page where encoders on Zebra RFID handhelds scan a tag, see its job and roll, and start, pause, complete or reopen that roll.

**Architecture:** A new single-file page (`templates/floor.html`, built like `templates/rewind.html`) that reuses the existing read-only tag lookup and roll endpoints. The backend adds one setting (`encoders`), one config endpoint, one page route and a link/QR function that works for both `/rewind` and `/floor`. The "one tag at a time" rules live in a small pure-JS scan collector inside the page, tested with Node.

**Tech Stack:** Flask (`app.py`), vanilla JS/HTML, pytest (`tests/`, `jm` fixture), Node 22 (`node --test`), Playwright for the manual UI checks.

**Spec:** `docs/superpowers/specs/2026-11-12-floor-handheld-page-design.md`

## Global Constraints

- Setup for tests: `pip install -r requirements.txt pytest`, then `python -m pytest -q` from the repo root.
- `BURST_MS = 400` and `COOLDOWN_MS = 1500`, constants at the top of `floor.html`'s script.
- Two or more different tags in one burst: red message exactly "More than one tag read. Move closer to the one you want and pull again." and none are used.
- Encoder names: one per line, at most 40 names, each at most 40 characters, setting key `encoders`, device key `jm_floor_operator` in `localStorage`.
- An empty encoder list never falls back to free-text names.
- Every write sends `operator: <name>`. Roll states and rules are unchanged; no new roll logic.
- Complete and Reopen need a second confirming tap ("Complete roll 3 as Sam?").
- Offline: action buttons disabled with the reason; nothing queued.
- Scan field uses `inputmode="none"`; Enter ends a scan. `/rewind` behaviour must not change.
- No database migration. Do not touch serial logic, registry, backups or restore.
- Test in the demo sandbox (`python mock_demo/run_mock.py`, port 5051), never the live database.

## Review Focus

1. Empty encoder list: the page must say so and offer no way to type a name. Pinned in Task 4.
2. Scan formatting variants (lowercase, `0x` prefix, trailing space or Tab, the same tag twice): treated as one tag. Pinned in Task 3.
3. Server refuses a roll action (for example the roll is already done): its message is shown and the card is unchanged. Pinned in Task 5.
4. A scan arriving while a confirm or printer dialog is open: ignored, never acts on the roll. Pinned in Task 5.
5. Connection drops mid-shift: Offline state, buttons disabled, no queued actions. Pinned in Task 5.

---

## File Structure

| File | Responsibility |
|---|---|
| `app.py` (modify) | `clean_encoders`, `encoders` setting, `/api/floor/config`, `/floor`, generalised `rewind_link(path)`, `/api/floor/link`, `/floor/qr.svg`, `/floor/qr`, `printer` added to rewind job rolls |
| `templates/floor.html` (create) | The encoder page: name picker, scan, roll card, job list, scan test |
| `templates/index.html` (modify) | Settings: Encoders box and Floor page QR panel |
| `tests/test_floor_page.py` (create) | Backend tests |
| `tests/floor_scan_test.js` (create), `tests/test_floor_scan.py` (create) | Node tests for the scan collector, run from pytest |
| `mock_demo/seed_demo.py`, `.claude/skills/encoding-floor-sandbox/SKILL.md`, `README.md` (modify) | Demo encoders, test notes, docs |
| `docs/superpowers/specs/2026-11-12-floor-handheld-page-design.md` (modify) | Add the Zebra device checklist result when run |

---

### Task 1: Encoder list setting and config endpoint

**Files:**
- Modify: `app.py` (near `clean_printers` ~line 150; `get_settings` ~line 166; `api_settings_save` ~line 2758; new route near `/api/printers` ~line 1217)
- Test: `tests/test_floor_page.py`

**Interfaces:**
- Produces: `clean_encoders(raw) -> str`; `get_settings()["encoders"] -> str` (default `""`); `GET /api/floor/config -> {"encoders": [str], "printers": [str]}`.

- [ ] **Step 1: Write the failing tests** in `tests/test_floor_page.py` (use the `jm` fixture and `jm.app.app.test_client()`):
  - `test_clean_encoders_trims_dedupes_and_caps`: `clean_encoders(" Sam, sam\nAnn;;  Lee   Ross \n")` equals `"Sam\nAnn\nLee Ross"`; 50 distinct names give 40 lines; a 60-character name is cut to 40.
  - `test_encoders_default_empty_and_saved_through_settings`: default `get_settings()["encoders"] == ""`; after `POST /api/settings {"encoders": "Sam\nAnn"}` it is `"Sam\nAnn"`.
  - `test_floor_config_lists_encoders_and_printers`: with encoders `Sam\nAnn` the response JSON has `encoders == ["Sam", "Ann"]` and `printers == ["Printer 1","Printer 2","Printer 3","Printer 4"]`; with no encoders set, `encoders == []`.
- [ ] **Step 2: Run** `python -m pytest tests/test_floor_page.py -q`. Expected: FAIL (names not defined / 404).
- [ ] **Step 3: Implement** `clean_encoders(raw) -> str` beside `clean_printers` (same splitting and case-insensitive de-duplication; 40 names, 40 characters). Add `"encoders": s.get("encoders", "")` to `get_settings`, add `encoders` to the allowed keys in `api_settings_save` and clean it there like `printers`, and add `@app.route("/api/floor/config")` returning both lists (printers split from `get_settings()["printers"]` as `api_printers` does).
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `git add app.py tests/test_floor_page.py && git commit -m "Floor page: encoder list setting and config endpoint"`

---

### Task 2: Link and QR for both pages, and the `/floor` route

**Files:**
- Modify: `app.py` (`rewind_link` ~line 1457, QR routes ~lines 1487-1523)
- Create: `templates/floor.html` (skeleton only: doctype, viewport meta, `<title>Floor</title>`, an `<input id="scan" inputmode="none">`, nothing else yet)
- Test: `tests/test_floor_page.py`

**Interfaces:**
- Consumes: Task 1 not required.
- Produces: `rewind_link(path="/rewind") -> (url, source, shareable)` (default keeps every current caller working); `GET /floor`; `GET /api/floor/link` (same JSON shape as `/api/rewind/link`); `GET /floor/qr.svg`; `GET /floor/qr` (printable sign titled FLOOR).

- [ ] **Step 1: Write the failing tests:**
  - `test_rewind_link_unchanged`: with `rewind_base_url` set to `http://10.0.0.5:5050` through `/api/settings`, `/api/rewind/link` returns `url == "http://10.0.0.5:5050/rewind"`.
  - `test_floor_link_uses_same_base`: same setting gives `/api/floor/link` `url == "http://10.0.0.5:5050/floor"`.
  - `test_floor_page_served`: `GET /floor` is 200, `text/html`, body contains `id="scan"`.
  - `test_floor_qr_routes`: `/floor/qr.svg` is 200 with `image/svg+xml`; `/floor/qr` is 200 and its body contains `FLOOR` and the floor URL.
- [ ] **Step 2: Run** `python -m pytest tests/test_floor_page.py -q`. Expected: the four new tests FAIL.
- [ ] **Step 3: Implement.** Give `rewind_link` a `path` parameter used in all four return branches. Add the four routes; share the QR sign HTML with the rewind sign through one helper `qr_sign_html(title, subtitle, link)` so `/rewind/qr` output stays the same apart from using the helper. Create the skeleton `floor.html`.
- [ ] **Step 4: Run** the full file plus `python -m pytest -q`. Expected: PASS, nothing else regresses.
- [ ] **Step 5: Commit** `git add app.py templates/floor.html tests/test_floor_page.py && git commit -m "Floor page: /floor route, link and QR sign"`

---

### Task 3: Scan collector (one tag at a time)

**Files:**
- Modify: `templates/floor.html` (script block between the markers `/* scan-collector:start */` and `/* scan-collector:end */`, plus the `BURST_MS` and `COOLDOWN_MS` constants just above them)
- Create: `tests/floor_scan_test.js`, `tests/test_floor_scan.py`

**Interfaces:**
- Produces, in `floor.html`: `createScanCollector({burstMs = BURST_MS, cooldownMs = COOLDOWN_MS}) -> {push(raw: string, now: number): boolean, due(now: number): null | {kind: "one", epc: string} | {kind: "many", epcs: string[]}}`.
  - `push` normalises the raw text (remove whitespace, `:`, `-`, `_`; uppercase; drop a leading `0X`), ignores empty results and anything arriving before `cooldownUntil`, adds the rest to the current burst (distinct, in order) and returns whether it was taken.
  - `due(now)` closes the burst once `now - lastPushAt >= burstMs`: one distinct tag returns `{kind:"one"}` and sets `cooldownUntil = now + cooldownMs`; two or more returns `{kind:"many"}` and sets no cooldown; otherwise `null`.
  - The code between the markers has no DOM access, so Node can evaluate it alone.

- [ ] **Step 1: Write the failing tests** in `tests/floor_scan_test.js` (`node --test`; load the block by reading `templates/floor.html` and evaluating the text between the markers with `new Function`). Test names and assertions:
  - `one tag after the burst window`: push `"300000000000000000000001"` at t=0; `due(399)` is `null`; `due(400)` is `{kind:"one", epc:"300000000000000000000001"}`.
  - `same tag repeated is one tag`: pushes at t=0, 100, 200 of the same tag give `{kind:"one"}`.
  - `formatting variants are the same tag`: `"0x3000...01"`, `"3000...01 "` (trailing space), lowercase and `"30:00:..."` all collapse to one tag.
  - `two different tags are rejected, none used`: two tags within 400 ms give `{kind:"many", epcs:[a, b]}`.
  - `cooldown after an accepted scan`: after a `one` result at t=400, `push(tag, 1000)` returns `false`; `push(tag, 1900)` returns `true`.
  - `no cooldown after a rejected burst`: after `many` at t=400, `push(tag, 450)` returns `true`.
  - `empty input is ignored`: `push("  ", 0)` returns `false` and `due(1000)` is `null`.
  - In `tests/test_floor_scan.py`: `test_scan_collector_node_tests` runs `node --test tests/floor_scan_test.js` and asserts exit code 0; skipped with `pytest.skip` if `node` is not on the PATH.
- [ ] **Step 2: Run** `node --test tests/floor_scan_test.js`. Expected: FAIL (markers or function missing).
- [ ] **Step 3: Implement** `createScanCollector` between the markers as specified above.
- [ ] **Step 4: Run** `node --test tests/floor_scan_test.js` then `python -m pytest tests/test_floor_scan.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `git add templates/floor.html tests/floor_scan_test.js tests/test_floor_scan.py && git commit -m "Floor page: scan collector, one tag at a time"`

---

### Task 4: Page shell: name picker, scan field, scan results, scan test

**Files:**
- Modify: `templates/floor.html`

**Interfaces:**
- Consumes: `GET /api/floor/config` (Task 1), `createScanCollector` (Task 3), `POST /api/rewind/lookup` (existing; body `{raw, job_id}`; results `found`, `other_job`, `wrong_product`, `not_issued`, `ambiguous`, `invalid`, `tid`).
- Produces: JS `state.operator: string`, `setOperator(name)`, `onScanResult(lookupJson)`, and hash routes `#/` (home), `#/job/<id>`, `#/job/<id>/roll/<n>`, `#/test`. Task 5 fills the job list and roll card behind these routes.

- [ ] **Step 1: Build the shell**, copying the styling approach of `templates/rewind.html` (header, `#banner`, sticky scan bar, `#toast`, 16px minimum text, touch targets of at least 48px).
  - **Name picker:** big buttons from `/api/floor/config`; choice saved to `localStorage["jm_floor_operator"]`; header shows the name large, tapping it reopens the picker. An empty list shows "No encoder names are set up. Ask for names to be added in Settings." and no input of any kind. A saved name no longer in the list is kept.
  - **Scan field:** always focused (refocus after any tap that is not on another input), `inputmode="none"`; on Enter its value goes to `collector.push(value, Date.now())` and the field clears; a 50 ms interval calls `collector.due(Date.now())`.
  - **Results:** `one` calls `/api/rewind/lookup` with the open job id if any. `found` navigates to that roll's route; every other result shows a full-width coloured message with the server's `message` and no buttons; `many` shows the exact rejection text from Global Constraints. Feedback: vibration and a short beep like `/rewind`'s `feedback()`.
  - **Scan test (`#/test`):** shows the last raw text received (visibly marking Tab, Enter and spaces), its length, the verdict message from the lookup, and a "No scan yet. Pull the trigger, or open Scan test" line under the scan field after 20 seconds without a scan.
  - **Offline:** header dot and banner as in `/rewind`, driven by failed fetches.
- [ ] **Step 2: Verify in the sandbox.** Run `python mock_demo/run_mock.py`, open `http://127.0.0.1:5051/floor` with Playwright at 390x800 (script in the scratchpad, not the repo). Assert:
  1. With no encoders set, the page shows the empty-list message and the page contains no text input other than `#scan` (Review Focus 1).
  2. After saving encoders `Sam` and `Ann` through `POST /api/settings` and reloading, name buttons appear; tapping `Sam` shows `Sam` in the header and survives a reload.
  3. Type a valid EPC from demo job 40121 into `#scan` plus Enter: the page navigates to that roll's route. Type two different valid EPCs 100 ms apart: the red rejection text appears and the route does not change. Type a random 24-hex string: the server's "not made by Job Manager" message appears.
  4. `#/test` shows the raw text, its length and the verdict.
  5. No console errors.
- [ ] **Step 3: Commit** `git add templates/floor.html && git commit -m "Floor page: name picker, scan field, results, scan test"`

---

### Task 5: Job list and roll card with actions

**Files:**
- Modify: `templates/floor.html`, `app.py` (add `"printer"` to the `keep` tuple in `api_rewind_job`, ~line 1559)
- Test: `tests/test_floor_page.py`

**Interfaces:**
- Consumes: `state.operator` (Task 4); existing endpoints `GET /api/rewind/jobs`, `GET /api/rewind/jobs/<id>`, `POST /api/jobs/<id>/rolls/<n>` (`{status, operator, note}`), `POST /api/jobs/<id>/rolls/<n>/printer` (`{printer, operator}`), `GET /api/printers`.
- Produces: `GET /api/rewind/jobs/<id>` rolls now include `printer`.

- [ ] **Step 1: Write the failing test** `test_rewind_job_rolls_include_printer`: create `jm.job(1)`, put roll 1 on "Printer 2" through `/api/jobs/<id>/rolls/1/printer`, then `GET /api/rewind/jobs/<id>`: `rolls[0]["printer"] == "Printer 2"` and `rolls[1]["printer"]` is `None`. Run it; expected FAIL (key missing).
- [ ] **Step 2: Add `"printer"` to `keep`**; run the test; expected PASS.
- [ ] **Step 3: Build the job list and roll card** in `floor.html`.
  - **Job list (`#/`):** search box and the open jobs from `/api/rewind/jobs`, refreshing every 8 seconds like `/rewind`'s `poll()`; tapping a job opens its roll list; tapping a roll opens its card.
  - **Roll card:** job, ticket, customer, roll number, label range, state and who has it, printer if any; for a scanned tag also the label number and FIRST/LAST/middle. Buttons per the spec table: Start (printer picker, busy printers marked from `/api/printers`), Change printer, Pause (optional note), Resume, Complete, Reopen. Complete and Reopen show "Complete roll N as <name>?" with Confirm and Cancel. Every request sends `operator: state.operator`.
  - **After each action** reload the job from `/api/rewind/jobs/<id>` and redraw; on a server error show its message and leave the card unchanged.
  - **Dialogs are modal:** while the printer picker or a confirm is open, scans are dropped and the scan field is not refocused.
  - **Offline:** all action buttons disabled, with the reason "No connection to the server".
- [ ] **Step 4: Verify in the sandbox** (Playwright, 390x800, restart the sandbox first so it picks up `app.py`). Assert:
  1. Scan a tag from a not-started roll: card shows Start; Start then pick "Printer 1" makes the roll "In progress" and the desktop API `GET /api/jobs/<id>` shows `started_by` equals the chosen name.
  2. Pause with a note, Resume, then Complete: Complete needs the second tap, and the activity feed (`GET /api/activity`) shows the name against each action.
  3. Reopen a completed roll after its confirm: back to running.
  4. Review Focus 3: Complete a roll in a second browser context first, then press Change printer on the stale card in the first one: the server's "This roll is done. Reopen it before changing its printer." text is shown and the card still redraws to the true state.
  5. Review Focus 4: open the Complete confirm and type a valid EPC of another roll into `#scan` plus Enter: nothing changes and the route stays on the same roll.
  6. Review Focus 5: set the browser context offline: banner shows Offline, every action button is disabled; back online, buttons return without a reload.
  7. Without a scan: pick a job from the list, then a roll; same card appears.
  8. No console errors; no horizontal scroll at 390px.
- [ ] **Step 5: Run** `python -m pytest -q`. Expected: PASS.
- [ ] **Step 6: Commit** `git add templates/floor.html app.py tests/test_floor_page.py && git commit -m "Floor page: job list, roll card and roll actions"`

---

### Task 6: Settings panels: Encoders and Floor page link

**Files:**
- Modify: `templates/index.html` (Settings section ~lines 1240-1270; JS `savePrinters` ~line 2614 and `loadRewind` ~line 3349 as models)

**Interfaces:**
- Consumes: `encoders` setting (Task 1), `GET /api/floor/link`, `/floor/qr.svg`, `/floor/qr` (Task 2).

- [ ] **Step 1: Add an "Encoders" panel** after the Printers panel: a textarea `#s-encoders` ("One name per line. Encoders pick their name on the floor page."), a Save button calling a new `saveEncoders()` that posts `{encoders}` to `/api/settings`, reloads `META`, shows a toast, and fills the textarea from `META.settings.encoders` when Settings opens.
- [ ] **Step 2: Add a "Floor page for handhelds" panel** next to the Rewind panel (same section tab, `data-sec="tablets"`): QR image, URL, note, "Print QR sign" and "Open the page" links, filled by a `loadFloorLink()` modelled on `loadRewind()`. It uses the same "Address the tablets use" value, so no second address box; say so in a one-line hint.
- [ ] **Step 3: Verify in the sandbox:** open Settings, enter `Sam` and `Ann` in Encoders, Save, reload: names persist and `/api/floor/config` lists them; the Floor panel shows a QR image and a URL ending `/floor`; "Print QR sign" opens a page that reads FLOOR. No console errors.
- [ ] **Step 4: Commit** `git add templates/index.html && git commit -m "Settings: encoder names and floor page QR"`

---

### Task 7: Sandbox, docs and the device checklist

**Files:**
- Modify: `mock_demo/seed_demo.py`, `.claude/skills/encoding-floor-sandbox/SKILL.md`, `README.md`

- [ ] **Step 1:** In `seed_demo.py` save demo encoders (`Sam`, `Ann`, `Lee`) through `POST /api/settings`.
- [ ] **Step 2:** In `SKILL.md` add a "Floor page (Zebra handhelds)" test pass listing the checks from Tasks 4 and 5, and a line under the Rewind note that `/floor` shares the lookup and roll endpoints.
- [ ] **Step 3:** In `README.md` add a "Floor page (Zebra handhelds)" bullet under "What it does" (URL, QR in Settings, names list, one tag at a time, DataWedge profile settings from the spec's open items) and `templates/floor.html` to the Files table.
- [ ] **Step 4:** Reset and restart the sandbox (`python mock_demo/run_mock.py --reset`), confirm `/floor` shows the three names; run `python -m pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit** `git add mock_demo/seed_demo.py .claude/skills/encoding-floor-sandbox/SKILL.md README.md && git commit -m "Floor page: demo encoders and docs"`
- [ ] **Step 6 (on a real Zebra, by a person):** run the four-step device checklist in the spec's Testing section, tune `BURST_MS` and `COOLDOWN_MS` if scans are rejected or stack, and note the result in the spec.
