# Floor Page: Scan Is the Front Door Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On the floor page, a scan shows its roll in place on the home screen, acts from there, and the next scan replaces it, with auto-submit and a card that fits a 360x640 handheld.

**Architecture:** Only `templates/floor.html` and its Node tests change. The scan collector gains a per-tag cooldown and a pure auto-submit helper (both Node-tested). The page's state becomes one "current card" (`S.cur`) on the home route `#/`; browsing jobs is a side route that ends by setting the current card.

**Tech Stack:** Vanilla JS/HTML, Node 22 (`node --test`), pytest (runs the Node tests), Playwright for browser checks against the demo sandbox.

**Spec:** `docs/superpowers/specs/2026-11-13-floor-scan-first-design.md` (builds on `2026-11-12-floor-handheld-page-design.md`).

## Global Constraints

- No backend, endpoint, migration or Settings change. No third-party libraries; one self-contained page.
- `AUTO_MS = 150`, `BURST_MS = 400`, `COOLDOWN_MS = 1500`, all constants inside the `scan-collector` markers.
- Auto-submit: exactly 24 hex characters in the scan field, then 150 ms of quiet. Enter still works. Anything else needs Enter.
- Cooldown is per tag: the same tag is ignored for `COOLDOWN_MS` after use; a different tag is accepted at once.
- Lookups are sent with `job_id: ''` (there is no "current job" any more).
- A rejected scan (unknown, wrong product, TID, ambiguous, refused burst, invalid) clears the card and shows a full-width message with no action buttons.
- A dialog acts on the `{job, roll}` it was opened for and closes whenever the card changes.
- Card text at least 16px (serials excepted), touch targets at least 56px high. Fit checks at 360x640 and 320x533.
- Kept unchanged: encoder names, burst rule, offline state, scan-ignored-while-dialog, Scan test, server messages.
- Verify in the demo sandbox (`python mock_demo/run_mock.py --reset`, port 5051). Flask caches templates, so restart the sandbox after each edit.

## Review Focus

1. Auto-submit and Enter both arrive for the same tag: one lookup, not two. Pinned in Tasks 1 and 2.
2. Two scans in quick succession where the first lookup is slower: the later scan's card wins. Pinned in Task 2.
3. A scan is rejected after a card was showing: the old card's buttons are gone. Pinned in Task 2.
4. A dialog is open when the card changes: it closes and never acts on the new roll. Pinned in Task 2.
5. The main action is hidden below the fold on a short screen, in every roll state (not started, running, paused, done). Pinned in Task 3.

---

### Task 1: Collector: per-tag cooldown and auto-submit helper

**Files:**
- Modify: `templates/floor.html` (the block between `/* scan-collector:start */` and `/* scan-collector:end */`)
- Modify: `tests/floor_scan_test.js` (loader now returns both functions)

**Interfaces:**
- Produces: `createScanCollector` (existing signature) now ignores only the same tag during cooldown; new `createAutoSubmit({idleMs = AUTO_MS} = {}) -> {input(value: string, now: number): void, due(now: number): string | null}`; constant `AUTO_MS = 150`.
  - `input` records the field's current value and time when it is exactly 24 hex characters (case-insensitive, trimmed), otherwise forgets any pending value. `due` returns that value once, when `now - at >= idleMs`, and returns `null` after that until `input` sees a new matching value.

- [ ] **Step 1: Change the loader line in `tests/floor_scan_test.js`** to `const {createScanCollector: create, createAutoSubmit} = new Function(m[1] + '; return {createScanCollector, createAutoSubmit};')();` and add these tests (A and B are the existing tag constants):
  - `different tag is accepted straight after a used scan`: `push(A,0)`, `due(400).kind === 'one'`, then `push(B,500) === true` and `due(900)` equals `{kind:'one', epc:B}`.
  - `same tag is still ignored during cooldown and accepted after`: after the used A at 400, `push(A,1000) === false` and `push(A,1900) === true` (the existing test already covers this; keep it).
  - `auto: 24 hex then 150 ms of quiet submits once`: `input(A,0)`; `due(149) === null`; `due(150) === A`; `due(151) === null`.
  - `auto: a partial value never submits`: `input(A.slice(0,12),0)`; `due(5000) === null`.
  - `auto: typing resets the timer`: `input(A,0)`, `input(A,100)`; `due(249) === null`; `due(250) === A`.
  - `auto: lowercase hex counts, 25 characters and non-hex do not`: `input(A.toLowerCase(),0)` then `due(150)` returns the lowercase value; `input(A+'0',0)` and `input('Z'.repeat(24),0)` give `due(1000) === null`.
  - `auto: a value that stops matching is forgotten`: `input(A,0)`, `input('',50)`; `due(1000) === null`.
- [ ] **Step 2: Run** `node --test tests/floor_scan_test.js`. Expected: the new tests FAIL (`createAutoSubmit` missing; the cross-tag test fails because cooldown blocks everything).
- [ ] **Step 3: Implement.** Replace the single `cooldownUntil` in `createScanCollector` with `{tag, until}` for the last used tag so `push` ignores only a matching tag before `until`; add `AUTO_MS` to the constants line and `createAutoSubmit` after the collector, inside the markers.
- [ ] **Step 4: Run** `node --test tests/floor_scan_test.js` and `python -m pytest -q tests/test_floor_scan.py`. Expected: all pass.
- [ ] **Step 5: Commit** `git add templates/floor.html tests/floor_scan_test.js && git commit -m "Floor page: per-tag cooldown and auto-submit helper"`

---

### Task 2: Scan screen is home: one current card, browse as a side route

**Files:**
- Modify: `templates/floor.html`

**Interfaces:**
- Consumes: `createScanCollector`, `createAutoSubmit`, `AUTO_MS` (Task 1); existing endpoints `/api/rewind/lookup`, `/api/rewind/jobs`, `/api/rewind/jobs/<id>`, roll and printer endpoints.
- Produces, in the page:
  - `S.cur: null | {job: number, roll: number, scan: object | null}`, `S.flash: null | {kind: 'ok' | 'bad', text: string}`, `S.seq: number` (lookup sequence).
  - `route(): {view: 'scan' | 'browse' | 'job' | 'test', job: number | null}` for `#/`, `#/browse`, `#/browse/<jobId>`, `#/test`.
  - `showCard(job: number, roll: number, scan: object | null): void` (sets `S.cur`, clears `S.flash`, closes any dialog, loads the job, goes to `#/`); `rejectScan(text: string, extra?: string): void` (clears `S.cur`, shows the full-width message); `flash(kind, text): void` (message above the card, card kept).
  - Dialog helpers keep their signatures but take their target from `S.cur` at open time.

- [ ] **Step 1: Write the browser check** `t2.js` in the scratchpad (not the repo) using Playwright at 360x640 against the sandbox, signed in as Sam (`localStorage['jm_floor_operator']`). It collects EPCs from `GET /api/rewind/jobs/<id>` for job 40140 (rolls 2 and 3 are not started) and job 40121 (roll 4 running). Assertions, each printed PASS or FAIL:
  1. Fresh `#/`: text "Scan a tag" and a "Browse jobs" button are visible; no `.card` job list.
  2. Type a valid EPC into `#scan` with no Enter: within 1 s `#rview` shows its roll and `location.hash` is still `#/`.
  3. Immediately (under 1.5 s later) type a different valid EPC with no Enter: the card changes to the second roll.
  4. Type the same valid EPC then press Enter right away: the number of `/api/rewind/lookup` requests (counted with `page.on('request')`) increases by exactly 1.
  5. Type a valid-format EPC that no job owns (`3074257BF7194E4000000999`): a full-width message appears, `#rview` has no buttons, and the earlier card is gone (Review Focus 3).
  6. Race (Review Focus 2): with `page.route` delaying the first lookup by 800 ms and not the second, type tag X then tag Y 100 ms apart: the final card is Y's roll.
  7. Open the Complete confirm, type another roll's EPC: nothing changes and the dialog is still open; then call `showCard` for another roll through `page.evaluate`: the dialog is closed and the original roll's status is unchanged on the server (Review Focus 4).
  8. Browse: tap "Browse jobs", tap job 40140, tap roll 3: the card for roll 3 shows on `#/`; Back from `#/browse` returns to `#/`.
  9. The earlier roll-action checks still pass on the new screen: Start with a printer, Pause with a note, Resume, Complete with confirm, Reopen, a server refusal (roll completed elsewhere, then Change printer) shows the server's message and the card redraws, and offline disables every action button.
  10. `#/test`: scanning a good tag stays on `#/test` and shows "Found".
  11. No console errors other than the 400 from the deliberate refusal.
- [ ] **Step 2: Restart the sandbox and run it.** Expected: FAIL (steps 1 to 8 fail on the current routes).
- [ ] **Step 3: Implement the state and routes.** Replace the `#/job/<id>[/roll/<n>]` routes and `S.scan` with `S.cur`/`S.flash` and the four views above; the home view shows the card, a flash line, or the empty state ("Scan a tag" with a Browse jobs button); the browse and job views reuse `drawList`/`drawJob`, and tapping a roll calls `showCard(job, roll, null)`. Back goes `job → browse → scan`; the header scan field stays visible on every view except name picking.
- [ ] **Step 4: Implement scanning.** Feed `#scan` `input` events to `createAutoSubmit({idleMs: AUTO_MS})` and in the existing 50 ms timer, when `auto.due(now)` returns a value, clear the field, set `S.lastRaw` and `collector.push(value, now)`. Keep Enter (ignore an empty value). `lookup(raw)` sends `job_id: ''`, increments `S.seq` and discards its result if `S.seq` moved or a dialog is open; `found` calls `showCard(job_id, roll_number, r)`, anything else calls `rejectScan(r.message, extra)`; the `many` burst result also calls `rejectScan(REJECT)`. On `#/test` keep showing the verdict only.
- [ ] **Step 5: Implement dialogs against `S.cur`.** `here()` returns `{job: S.cur.job, roll: S.cur.roll}`; `showCard` and `rejectScan` call `closeDlg()`; `act()` refreshes the card from `/api/rewind/jobs/<job>` only if `S.cur` still points at the same roll; action errors use `flash('bad', e.message)` and the "Saved as <name>" toast stays.
- [ ] **Step 6: Run** the check from Step 1 again, then `python -m pytest -q`. Expected: all PASS (except the known 400 console line) and 322 or more tests passing.
- [ ] **Step 7: Commit** `git add templates/floor.html && git commit -m "Floor page: scan screen is home, one current card"`

---

### Task 3: Card layout for a 360x640 handheld, empty state, confirmation, docs

**Files:**
- Modify: `templates/floor.html`, `README.md`, `.claude/skills/encoding-floor-sandbox/SKILL.md`, `docs/superpowers/specs/2026-11-12-floor-handheld-page-design.md` (one line at the top: "The screen flow is superseded by `2026-11-13-floor-scan-first-design.md`.")

**Interfaces:**
- Consumes: `S.cur`, `S.flash`, `rollCard` markup from Task 2.
- Produces: `rollCardHtml(job, roll, scan): string` (the card, buttons included) used by the home view.

- [ ] **Step 1: Write the fit check** `t3.js` in the scratchpad: at 360x640 and 320x533, for a roll in each state (not started: job 40140 roll 2; running: job 40121 roll 4; paused: pause one through the API; done: job 40121 roll 1) scan its tag and assert:
  1. The primary button (`.acts .main`) is fully inside the viewport (bottom edge at most the viewport height) with no scrolling (Review Focus 5).
  2. Every card text element has a computed font size of at least 16px, except `.ser .v` which must be at least 13px.
  3. Every `.acts button` is at least 56px high.
  4. The roll number element's font size is at least 48px.
  5. No horizontal scroll.
  6. After a Complete, a green line reading `Completed roll N as Sam` is visible above the card; after Start, `Started roll N on <printer> as Sam`; after Pause `Paused`, after Resume `Resumed`, after Reopen `Reopened` in the same form.
  7. The empty state fits on screen with its Browse jobs button visible.
- [ ] **Step 2: Run it.** Expected: FAIL (the current card is taller and its text smaller).
- [ ] **Step 3: Implement the layout** in `rollCardHtml`: one line `ticket · customer` (ellipsis, no job title), the large `ROLL N` with `of M`, a coloured state band, `Label X of Y` with the FIRST/LAST badge, `printer · who`, the primary button (Start when not started, Complete when running, Resume when paused, Reopen when done) full width, other actions in a row below, serials last. Compact the header and scan bar (the hint line takes no space when empty). Add the success confirmation as `flash('ok', ...)` from `act()` and style `.flash.ok` green. Empty state: large "Scan a tag" with the Browse jobs button.
- [ ] **Step 4: Run** `t3.js` and the Task 2 check `t2.js` again, then `python -m pytest -q`. Expected: all PASS.
- [ ] **Step 5: Update docs.** In `README.md` change the Floor page bullet to describe the scan-first flow, auto-submit (Enter optional), the per-tag pause, Browse jobs, and the DataWedge note that "Append ENTER" is now optional; in `SKILL.md` replace the floor test pass with the checks above (scan-first, auto-submit without Enter, 360x640 fit).
- [ ] **Step 6: Take new screenshots** of the empty state, a running card, a not-started card and a rejected scan at 360x640 for the user, from the sandbox.
- [ ] **Step 7: Commit** `git add -A && git commit -m "Floor page: handheld card layout, confirmations, docs"`
