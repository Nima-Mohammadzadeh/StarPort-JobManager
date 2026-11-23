# Add data to an existing job — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An operator can add more labels to an existing job; the app allocates new never-reused serials, new rolls and new data files, and every serial safety net knows about them.

**Architecture:** Additions live in a new `job_additions` table; `jobs.quantity/start_serial/overage_pct` stay as the original order. Serial allocation reuses `SS.reserve` (standard), `SHEETS_SEQ.reserve` (CDO) and `go2_random_epcs` (GO2). A single `add_data(job_id, data)` function in `app.py` (next to `create_job`) does the work; a thin route calls it.

**Tech Stack:** Python 3 / Flask / SQLite / openpyxl, pytest (`tests/conftest.py` `jm` fixture), single-file UI `templates/index.html`.

**Spec:** `docs/superpowers/specs/2026-11-04-add-data-to-job-design.md`

## Global Constraints

- Original `jobs.quantity`, `start_serial`, `overage_pct` are never modified by an addition.
- Serials are never reused: a failure after serials are taken keeps them recorded (standard: stay in registry/mirror/journal; CDO: `SHEETS_SEQ.void`).
- Job types: standard (UPC), CDO (`data_sheet == "cdo_seq"`), GO2 (`data_sheet == "go2_rand"`). Special jobs with no `upc` and no `data_sheet` are refused.
- Archived jobs refused with HTTP 409. Completed jobs become `In Progress` (status strings in `STATUSES`, `app.py:48`), `completed_at` cleared.
- Quantity is an exact integer 1 to 1,000,000; no overage on additions.
- New data files are named `_ADD<seq>`; existing files are never rewritten.
- `${{` must never appear in JS template literals in `templates/index.html` (Jinja).
- Test only against the `jm` fixture and the `mock_demo/` sandbox (port 5051), never live data.

## Review Focus

- Serial range for an addition is non-contiguous with the original (another job took the numbers after it): ranges and next-new-job start stay correct.
- Registry file deleted after an addition: `_jobs_ranges` fallback still blocks reuse of added serials.
- Disk/file write fails after serials taken: addition row not saved, no half-written files, serials not released.
- Two adds on the same job at once: distinct ranges, distinct roll numbers.
- Completed job gets an addition: reopens, new rolls pending, old rolls still complete.

---

### Task 1: Schema, ranges helper, safety nets

**Files:**
- Modify: `app.py` (`init_db`, near line 125-170), `serial_store.py:135-175`
- Create: `tests/test_additions.py`

**Interfaces:**
- Produces: table `job_additions(id, job_id, seq, quantity, serial_start, serial_end, first_roll, last_roll, files, reason, added_by, created_at)`, `UNIQUE(job_id, seq)`; `job_rolls.addition_id INTEGER`; `app.job_total_qty(conn, job: dict) -> int` (original production qty + sum of additions); `app.job_serial_ranges(conn, job: dict) -> list[dict]` (`{"start","end","label"}`, original first, then additions; `[]` if no serials).

- [ ] **Step 1: Write failing tests** in `tests/test_additions.py`: `test_schema_exists` (table and `job_rolls.addition_id` present after `jm.load()`); `test_serial_store_safety_nets_include_additions` — create `jm.job(1)`, insert a `job_additions` row by SQL for that job's UPC with range 5,000–5,999 and no registry/mirror row, assert `jm.SS._jobs_last_end(conn, upc) == 5999` and `jm.SS._jobs_ranges` contains `{"start":5000,"end":5999}`; `test_total_and_ranges_helpers` (original 102 labels with overage 2 + addition of 1,000 gives 1,102 and two ranges).
- [ ] **Step 2: Run** `pytest tests/test_additions.py -v` — Expected: FAIL (no table/helpers).
- [ ] **Step 3: Implement** the table in `init_db` (CREATE TABLE IF NOT EXISTS + `add_col("job_rolls","addition_id","addition_id INTEGER")`), the two helpers in `app.py`, and extend `serial_store._jobs_ranges` / `_jobs_last_end` with a `SELECT j.job_number, a.serial_start, a.serial_end FROM job_additions a JOIN jobs j ON j.id=a.job_id WHERE j.upc=? AND a.serial_start IS NOT NULL`.
- [ ] **Step 4: Run** `pytest tests -q` — Expected: all PASS (existing tests unaffected).
- [ ] **Step 5: Commit** `feat: job_additions table and serial safety nets`.

### Task 2: add_data for standard (UPC) jobs

**Files:**
- Modify: `app.py` (new `add_data` after `create_job`), `generators.py:114` (`generate_db_files` gains `tag: str = ""` producing `{upc}{tag}{suffix}.xlsx`), `generators.py:74` (`roll_plan` gains `first_roll: int = 1`)
- Test: `tests/test_additions.py`

**Interfaces:**
- Consumes: Task 1 helpers; `SS.reserve(upc, job_number, qty, note, who=..., offline_start=...)`.
- Produces: `app.add_data(job_id: int, data: dict) -> tuple[dict, int]`. `data`: `quantity`, `reason`, `added_by`, `preview` (bool), `offline_start`, `offline_confirm`. Success body: `{"success": True, "addition": {...row...}, "job": {...}, "rolls_added": int, "files": [names], "warnings": [...]}`, 201; preview returns 200 with `{"preview": True, "serial_start","serial_end","first_roll","last_roll","files","new_total"}` and reserves nothing. Errors: 400 bad quantity, 404, 409 archived, 423 registry offline (same shape as `create_job`), 503 folder unreachable.

- [ ] **Step 1: Write failing tests**: `test_standard_addition_contiguous` (job 1 qty 100/lpr 50 overage 2 → 102 labels, 3 rolls; add 1,000 → addition starts at original end+1, rolls 4–23 with `addition_id` set, total 1,102, file `<UPC>_ADD1.xlsx` has 1,000 rows with right serial/EPC, original files untouched); `test_non_contiguous_addition` (create job A, create job B same UPC, add to A: start is B's end+1, no overlap with any range via `SS.range_conflicts`, next new job starts after it); `test_addition_recorded_in_registry_mirror_journal`; `test_preview_reserves_nothing` (next_start unchanged, no rows, no files); `test_completed_job_reopens` (all rolls complete, status Complete → add → status `In Progress`, `completed_at` NULL, old rolls complete, new pending); `test_archived_refused_409`; `test_bad_quantity_400` (0, -5, "abc", 1_000_001); `test_last_original_roll_not_topped_up` (original last roll partial: its quantity/serials unchanged); `test_failed_file_write_keeps_serials_used` (monkeypatch `generate_db_files` to raise: response is an error, no `job_additions` row, no new rolls, no `_ADD` files, and `SS.next_start` is past the reserved range); `test_unreachable_folder_503` (nothing reserved).
- [ ] **Step 2: Run** `pytest tests/test_additions.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** `add_data` standard path: validate → pre-flight folder exists (503) → per-job lock (`threading.Lock` dict keyed by job id) → compute `seq`, next roll number (`MAX(roll_number)+1`) → preview return if requested → `SS.reserve(note=f"addition #{seq}")` (map `SerialsOffline`→423, `ValueError`→400) → `roll_plan(upc, start, qty, lpr, first_roll)` → `generate_db_files(folder, upc, start, qty, qty_db, tag=f"_ADD{seq}")` (on exception: return 500, no DB changes) → one transaction: insert `job_additions`, `_insert_rolls` with `addition_id`, reopen status if `Complete`, `log_activity(conn,"data_added",...)`. Use the spec's activity message. `lpr` falls back to the quantity itself when the job has none.
- [ ] **Step 4: Run** `pytest tests -q` — Expected: PASS.
- [ ] **Step 5: Commit** `feat: add data to standard jobs`.

### Task 3: CDO and GO2 additions

**Files:**
- Modify: `app.py` (`add_data`), `generators.py` (`generate_cdo_files` and `generate_go2_files` gain `tag: str = ""` for file names; add `read_go2_epcs(job_dir) -> set[str]`)
- Test: `tests/test_additions.py`

**Interfaces:**
- Consumes: `SHEETS_SEQ.reserve("cdo_seq", job_number, qty, who=..., note=...)` / `SHEETS_SEQ.void(alloc, reason)`, `roll_plan_seq(start, qty, lpr)` (needs `first_roll` param like Task 2), `roll_plan_plain` (same), `go2_random_epcs(count, exclude=)`.
- Produces: `add_data` handles `data_sheet in ("cdo_seq","go2_rand")`.

- [ ] **Step 1: Write failing tests** (reuse `_cdo` helper style from `tests/test_cdo.py`): `test_cdo_addition_continues_series` (job 1 → add 200 → new job continues after the addition, no number repeats across all three, EPC = `%08X`); `test_cdo_failed_write_voids` (patch `generate_cdo_files` to raise → numbers voided, no addition row, next CDO job starts after them); `test_go2_addition_unique_epcs` (original + addition EPCs all unique, all start `E2004`, no serials on addition row); `test_special_job_without_data_refused` (400).
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement** both branches; GO2 reads existing EPCs from `Data/*.xlsx` via `read_go2_epcs` (best-effort, warning if unreadable) and passes them as `exclude`.
- [ ] **Step 4: Run** `pytest tests -q` — Expected: PASS.
- [ ] **Step 5: Commit** `feat: add data to CDO and GO2 jobs`.

### Task 4: API route, job detail, concurrency

**Files:**
- Modify: `app.py` (`api_job_detail` ~line 833, new route after it)
- Test: `tests/test_additions.py`

**Interfaces:**
- Produces: `POST /api/jobs/<id>/additions` → `add_data`; `GET /api/jobs/<id>` adds `"additions": [rows]`, `"serial_ranges": job_serial_ranges(...)`, and `total_qty` / `end_serial` now cover original plus additions (`end_serial` = last range end).

- [ ] **Step 1: Write failing tests** with `jm.app.app.test_client()`: `test_route_201_and_detail_lists_addition`; `test_route_preview`; `test_concurrent_adds_distinct` (two threads call `add_data` on one job: two additions, ranges disjoint, roll numbers `1..N` unique with no gaps).
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement** route (JSON body via `request.get_json(silent=True)`, `who` from `actor()` when `added_by` missing) and detail changes.
- [ ] **Step 4: Run** `pytest tests -q` — Expected: PASS.
- [ ] **Step 5: Commit** `feat: additions API and job detail`.

### Task 5: Regenerate, checklist, QC sheet, backup/restore

**Files:**
- Modify: `app.py:1432` (`api_regenerate`), `checklist_generator.py:23-33,297`, `generators.py:412` (`qc_html`), `backup.py` / `restore_backup.py` only if the serial history check reads `jobs` ranges
- Test: `tests/test_additions.py`, `tests/test_restore.py` style

**Interfaces:**
- Consumes: `job_serial_ranges`.

- [ ] **Step 1: Write failing tests**: `test_regenerate_includes_additions` (QC html contains the added roll and an "Added" marker; checklist shows both ranges; missing DB rebuild writes original range only plus keeps the `_ADD1` file untouched); `test_backup_restore_keeps_additions` (make_backup → restore into new root → `job_additions` rows and added serials still conflict).
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement**: pass `job["additions"]` / ranges into the generators; mark added rolls (`addition_id` not NULL) in `qc_html`; check `restore_backup.py` serial history verification and extend it if it recomputes from `start_serial + quantity`.
- [ ] **Step 4: Run** `pytest tests -q` — Expected: PASS.
- [ ] **Step 5: Commit** `feat: regenerate, QC and restore aware of additions`.

### Task 6: UI

**Files:**
- Modify: `templates/index.html` (`renderDrawer` ~1471; roll rows `rollRowHtml`; add dialog markup + `openAddData()` / `submitAddData()` JS)

**Interfaces:**
- Consumes: `POST /api/jobs/<id>/additions` (with `preview:true` first), `CURRENT.serial_ranges`, `CURRENT.additions`, roll `addition_id`.

- [ ] **Step 1:** Add an **Add data** button in `.d-actions` (hidden if archived, or special job with no `data_sheet`); dialog with quantity, reason, preview (range, rolls, files, new total) then Confirm; asks "who" via existing `askName`.
- [ ] **Step 2:** Quantity tile shows `original + N added`; Serial range tile lists each range; added rolls get a small "added" tag; GO2/CDO show it too.
- [ ] **Step 3:** Verify the page still renders: `python -c "import jinja2..."` style check by loading `/` in the sandbox (no `${{`).
- [ ] **Step 4: Commit** `feat: Add data dialog and job views`.

### Task 7: Sandbox verification

**Files:**
- Modify: `mock_demo/seed_demo.py` (add a CDO and a GO2 demo job if missing)

- [ ] **Step 1:** `python mock_demo/run_mock.py --reset`, drive with Playwright at desktop and ~820px: add 10k to an in-progress standard job, to a completed one (reopens), to a CDO job and a GO2 job; check serial tile, roll list marker, tag check on a new-roll EPC, `/rewind`, console errors clean.
- [ ] **Step 2:** Run the full `pytest tests -q`; report what was and was not exercised; commit and push.

---

## Self-review

- Spec coverage: data model (T1), standard/CDO/GO2 allocation (T2–3), safety nets (T1), API/preview/concurrency (T4), regeneration/QC/restore (T5), UI (T6), sandbox (T7). Reopen/archived/limits in T2.
- Types: `add_data`, `job_serial_ranges`, `job_total_qty`, `first_roll`, `tag` are defined in T1–T3 and reused unchanged.
