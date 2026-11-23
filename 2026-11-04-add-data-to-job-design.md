# Add data to an existing job — design

Date: 2026-11-04 · Status: awaiting review

## Purpose

An operator is partway through a job and finds they need more labels than the job was made for
(for example 10,000 more). They open the job in Job Manager and add that quantity. The app must then
treat the extra data like the original: new serials that are never reused, new rolls to track, new
encoding data files, and every serial safety net aware of the extra range.

## Decisions (agreed)

- **Job types:** standard (UPC + serials), CDO sequential sheet, GO2 random-EPC sheet. Special jobs with no
  data (no UPC, no sheet) have nothing to add and do not show the button.
- **Job states:** any non-archived job. Archived jobs are refused ("Restore it first").
- **Completed jobs reopen** as *In progress*; the new rolls start *not started*. The activity log records it.
- **Quantity** is the exact number of extra labels to encode. No overage percentage on additions.

## The problem this solves

A job stores one `start_serial` and one `quantity`, and several serial safety nets compute the job's range
as `start_serial + production_qty - 1` (`serial_store._jobs_ranges`, `_jobs_last_end`, plus
`app.api_job_detail`, `api_regenerate`, `checklist_generator`, and the Jobs/Rolls UI tiles). That assumes one
contiguous block. Extra serials usually cannot be contiguous: other jobs for the same UPC will have taken the
numbers that follow. Raising `quantity` would make the app believe the job owns serials it never had
(false conflicts, wrong ranges, and a wrong "end serial").

So `jobs.quantity`, `jobs.start_serial` and `jobs.overage_pct` stay as the **original order** and are never
changed. Additions are stored separately and every consumer is taught to read both.

## Data model

New table, created in `init_db()`:

```
job_additions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,              -- 1, 2, 3 ... per job
  quantity INTEGER NOT NULL,
  serial_start INTEGER, serial_end INTEGER,   -- NULL for GO2 (no serials)
  first_roll INTEGER NOT NULL, last_roll INTEGER NOT NULL,
  files TEXT,                        -- JSON list of data file names written
  reason TEXT, added_by TEXT, created_at TEXT NOT NULL,
  UNIQUE(job_id, seq))
```

`job_rolls` gets a nullable `addition_id` column (via `add_col`) so a roll knows which addition created it.
A job's total labels = original production quantity + sum of its additions.

Helper in `app.py`: `job_ranges(conn, job)` returns the job's serial ranges, original first, then each addition.
All consumers below use it instead of recomputing from `start_serial`.

## Allocation, per job type

All steps run inside the same flow as `create_job`: folders/files checked first, serials taken, files written,
database saved last. A failure after serials are taken leaves them recorded (marked gap), never released.

**Standard (UPC).**
1. `SS.range_conflicts` is not needed for the default path; `SS.reserve(upc, job_number, qty, note="addition #N")`
   hands out the next free range exactly as for a new job (registry, database mirror, journal; offline rules
   unchanged, including `SerialsOffline` needing a confirmed start).
2. The range does not have to touch the original block.
3. Rolls: `roll_plan(upc, start, qty, job.lpr)`, with `roll_number` offset to continue after the job's current
   highest roll number.
4. Files: `Data/<UPC>_ADD<N>.xlsx` (or `_ADD<N>_DB<k>` when `qty_db` splits it). Existing files are never rewritten.

**CDO.** `SHEETS_SEQ.reserve("cdo_seq", job_number, qty, ...)` continues the shared series. Rolls via
`roll_plan_seq`. Files via `generate_cdo_files` with the `_ADD<N>` name. If the file write fails the numbers are
`void()`ed (kept as a marked gap).

**GO2.** No serials. `go2_random_epcs(qty, exclude=<EPCs already in this job's files>)` so nothing repeats; rolls
via `roll_plan_plain`. Files verified with `verify_go2_file` as today. Reading the existing EPCs reuses the
files in `Data/` (skipped with a warning if unreadable, since the 19-hex random space makes a clash
vanishingly unlikely, but the check is attempted).

**Limits.** Quantity 1 to 1,000,000 per addition. The range may not run past `SS.MAX_SERIAL`. CDO stops at
`FFFFFFFF` as today.

## Safety nets

- `serial_store._jobs_ranges` and `_jobs_last_end` also read `job_additions` (serial rows only), so a lost
  registry or journal still cannot cause reuse of added serials.
- Backup/restore: `backup.py` and `restore_backup.py` copy the whole database, so the new table travels with it.
  The restore serial-history check is extended to include addition ranges. (Verify while implementing; add a test.)
- `serial_store.backfill_from_registry` / `registry_gaps` are unaffected: additions go through `reserve()` and
  appear in `serial_allocations` like any job.

## Status and activity

- Reopen: a *Completed* job becomes *In progress* (`status` as the app names it; check the exact string in
  `_set_rolls`/`api_job_edit`), `completed_at` cleared if the job stores one.
- `log_activity(conn, "data_added", job_id, "Added 10,000 labels (serials 120,001–130,000, rolls 13–22) — <reason>", who)`.
- A job note is not auto-created; the reason is on the activity entry and the addition row.

## API

`POST /api/jobs/<id>/additions` body `{quantity, reason, added_by, offline_start?, offline_confirm?, preview?}`

- `preview: true` runs validation and returns what would happen (serial range, roll numbers, file names,
  new total) **without reserving anything**. Serials are only taken on the real call.
- Success returns `{success, addition, job, rolls_added, warnings}` (201).
- Errors: 400 bad quantity or past max serial, 404 no job, 409 archived, 423 registry offline (same payload shape
  as job creation), 503 job folder unreachable ("Nothing was added").
- `GET /api/jobs/<id>` includes `additions` and the combined `total_qty` / serial ranges.
- Concurrency: the whole operation holds a per-job lock plus the serial store lock, so two simultaneous adds on
  one job get different ranges and roll numbers; a second "add" while the first runs waits.

## Regeneration and printouts

- `generate_qc_sheet` and `generate_checklist_html` are re-run with all rolls and show original vs added rolls
  ("Added 11/04"). The checklist shows each serial range.
- `api_regenerate` rebuilds a missing database from `job_ranges` (original plus additions), never allocates.
- Roll sheet, rewind page and tag check read `job_rolls`, so new rolls and their serials work unchanged;
  `_lookup` already finds owners by serial range per roll.

## UI (`templates/index.html`)

- **Add data** button in the job dialog (hidden for archived and no-data special jobs).
- Dialog: quantity, reason (free text, optional), who (uses the existing "who" prompt). Shows the preview
  (serial range, new rolls, files, new total) and a confirm button; nothing is reserved until confirmed.
- Serial range tile shows the original range and each addition. Quantity tile shows "50,000 + 10,000 added".
- Roll list marks added rolls with a small "added" tag. Jobs list progress uses the combined roll count.
- Beware the Jinja gotcha: no `${{` in JavaScript template literals.

## Testing

Pytest (`tests/test_additions.py`, using the existing `jm` fixture):
standard addition contiguous and non-contiguous (another job in between); serials never overlap and the next new
job starts after the addition; registry, mirror and journal all hold the range; `_jobs_ranges` fallback blocks
reuse when the registry file is deleted; CDO series continues and a failed file write voids rather than reuses;
GO2 EPCs unique across original and added; roll numbering continues and the last original roll is untouched;
completed job reopens; archived refused; preview reserves nothing; two concurrent adds get distinct ranges;
registry offline path; backup then restore keeps additions; `api_regenerate` includes them.

Sandbox (`mock_demo/`, port 5051, never live data): add 10k to a standard demo job (in progress, completed, on
hold), to a non-contiguous case, check the dialog, serial tile, roll list, tag check against a new-roll EPC,
`/rewind`, tablet width, console clean. Demo seed gains a CDO and a GO2 job if missing.

## Out of scope

Removing or reducing an addition; changing an addition's range after the fact; additions for archived jobs;
overage percentage on additions; editing the original quantity.

## Open questions

None blocking. Exact status strings and the restore-check extension are confirmed during implementation.
