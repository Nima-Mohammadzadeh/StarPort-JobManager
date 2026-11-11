# Data Sheets as Plug-ins Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A customer data sheet kind (GO2, CDO, future ones) becomes one self-contained definition in a `datasheets/` package, so a new kind is one file instead of edits to job creation, add data and the New job page.

**Architecture:** A `DataSheet` base class (identity text, optional number `Series`, `file_names`, `write`, `check`, `existing_values`, `roll_plan`) plus a registry. Taking, previewing and voiding counting-series numbers is written once in the base class on top of `sheet_serials.py`. GO2 and CDO wrap the existing functions in `generators.py`. `app.py` and `templates/index.html` stop naming them and read everything from the registry and from `/api/meta`.

**Tech Stack:** Python 3.10+, Flask, SQLite, openpyxl, pytest; vanilla JavaScript in `templates/index.html` (rendered by Jinja).

**Spec:** `docs/superpowers/specs/2026-11-11-data-sheet-plugins-design.md`

## Global Constraints

- Restructure only: GO2 and CDO behave exactly as today and no new sheet kind ships. Sheet keys stay `go2_rand` and `cdo_seq`. No database migration.
- The 59 recorded scenarios in `tests/golden/job_build.json` must pass with **no edits to the recorded results** (`git diff --exit-code tests/golden/` after every task). All 313 existing tests keep passing; only test *code* that replaces functions by name may change (Task 2).
- Serial rules never loosen: a number is never handed out twice; every file is re-read and checked after it is written; a file that fails the check is removed and the job is not created.
- GO2 and CDO **wrap** the existing functions in `generators.py` (`generate_go2_files`, `verify_go2_file`, `generate_cdo_files`, `verify_cdo_file`, `go2_file_names`, `read_go2_epcs`, `roll_plan_seq`, `roll_plan_plain`); they call them through the module (`import generators as G; G.generate_go2_files(...)`) so tests can still replace them. Do not move that code.
- `/api/meta` keeps the existing keys `sequential` (per sheet) and `cdo_next`.
- CDO's "ran past the largest number" error stays word for word: `That would run past {max:,}, the largest number an 8-digit hex EPC holds (next free is {start:,}).`
- `templates/index.html` is rendered by Jinja: JavaScript template literals must never contain `${{`.
- Work on branch `claude/laughing-wright-s1z34w`. End every commit message with these two lines (blank line before them):
  `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01UeGnj7W2gqXCqnaVVEHaVU`
- Do not open a pull request.

## Review Focus

Failure modes the spec implies that no feature test would otherwise reach, most likely first. Each has a test in the task named in brackets.

1. A customer typed as `  cDo ` or `go2` must get its sheet, not be refused. [Task 1: `test_customer_lookup_ignores_capitalization_and_spaces`]
2. A job whose stored `data_sheet` key is not registered (sheet removed later, or a typo) must be refused by add data with the existing message and still open on the Jobs page, not crash. [Task 2: `test_unregistered_stored_sheet_key_is_refused_not_a_crash`]
3. Two sheets registered with the same key or the same customer must fail loudly at import, not silently shadow each other. [Task 1: `test_duplicate_key_or_customer_is_refused_at_registration`]
4. `/api/meta` must still carry `sequential` and `cdo_next`, and `next_start` must move after a CDO job is made (the page refreshes it each time New job opens). [Task 2: `test_meta_keeps_old_keys_and_next_start_moves`]
5. A `write()` that fails after the first of several files must leave no files behind, for every sheet including one nobody has written yet. [Task 1: contract check; Task 3: fake third sheet]

---

### Task 1: The `datasheets` package, GO2 and CDO as sheets (app behaviour unchanged)

**Files:**
- Create: `datasheets/__init__.py`, `datasheets/base.py`, `datasheets/go2.py`, `datasheets/cdo.py`
- Create: `tests/test_datasheets.py`, `tests/sheet_contract.py`
- Modify: `sheet_serials.py` (replace the hard-coded `SEQ_SHEETS`, `next_start`, and the limit message in `reserve`), `tests/conftest.py` (`APP_MODULES`, `JM.load`), `app.py` (one import line), `README.md` (file table row)

**Interfaces:**
- Produces (`datasheets/base.py`):
  - `@dataclass(frozen=True) class Series: first: int; max: int; limit_text: str`
  - `class DataSheet` with class attributes `key, customer, label, detail, hint, summary_label, summary_text` (all `str`) and `series: Series | None = None`; property `counts -> bool` (`series is not None`).
  - Methods a sheet implements: `file_names(self, label: str, parts: int) -> list[str]`; `write(self, folder: str, label: str, qty: int, qty_db: int | None, start: int | None = None, existing=()) -> dict` (returns the generator's dict, at least `{"files": [...]}`; writes into `<folder>/Data`, verifies each file, removes everything it wrote and raises on any failure); `check(self, path: str, start: int | None, rows: int) -> None` (re-open one file, raise `ValueError` on any problem); `roll_plan(self, start: int | None, qty: int, lpr: int, first_roll: int = 1) -> list`; `existing_values(self, folder: str) -> set` (default `set()`).
  - Provided by the base class, not overridden: `next_start(self) -> int | None` (`None` without a series), `take_numbers(self, job_number: str, qty: int, who: str | None = None, note: str = "base") -> dict | None` (`None` without a series, else `{"sheet","start","end","qty","job_number"}`), `void(self, alloc: dict, reason: str) -> None`.
- Produces (`datasheets/__init__.py`): `SHEETS: dict[str, DataSheet]` (insertion order `go2_rand`, `cdo_seq`); `register(sheet: DataSheet) -> None` (raises `ValueError` containing "already" for a duplicate key or a duplicate customer, compared case-insensitively; registers the sheet's series with `sheet_serials`); `get(key: str | None) -> DataSheet | None`; `for_customer(name: str | None) -> DataSheet | None`; `canonical_customer(name: str | None) -> str | None`.
- Produces (`sheet_serials.py`): `SEQ_SHEETS: dict = {}` and `register_series(key: str, first: int, max: int, limit_text: str) -> None` (raises `ValueError` on a duplicate key). `reserve` and `next_start` read `SEQ_SHEETS[key]`; an unknown key still raises `SheetSerialError("Unknown sequential data sheet: <key>")`.
- Produces (`tests/sheet_contract.py`): `check_contract(jm, sheet) -> None`, used here and in Task 3.
- Produces (`tests/conftest.py`): `JM.generators`, `JM.DS` (the `datasheets` module) set in `load()`; `APP_MODULES` gains `"datasheets"`, `"datasheets.base"`, `"datasheets.go2"`, `"datasheets.cdo"`.

- [ ] **Step 1: Prepare the test harness.** In `tests/conftest.py` add the four `datasheets` names to `APP_MODULES`, and in `JM.load()` import `datasheets` right after `config` (so series are registered before `app` or `sheet_serials` are used) and set `self.generators` and `self.DS`.

- [ ] **Step 2: Write `tests/sheet_contract.py::check_contract(jm, sheet)`.** It uses `jm.customers` as a scratch folder root and asserts, with `from generators import db_file_split`:
  1. `write(folder, "T1-A", 25, 10, start=sheet.next_start())` returns `len(files) == 3 == len(db_file_split(25, 10))`; the basenames equal `sheet.file_names("T1-A", 3)`; every file exists; `sheet.check(path, start_i, rows_i)` passes for each (`start_i` = start plus rows before it for a counting sheet, else `None`; rows are `[10, 10, 5]`).
  2. Tamper: open the first file with `openpyxl.load_workbook`, call `ws.delete_rows(2)`, save; `sheet.check(...)` for that file raises `ValueError`.
  3. Partial write: in a fresh empty folder, `monkeypatch`-style replace `openpyxl.Workbook.save` so the **second** call raises `RuntimeError("disk full")`; `sheet.write(folder, "T1-B", 25, 10, start=...)` raises, and `os.listdir(<folder>/Data)` is empty afterwards (use `unittest.mock.patch.object`, since this helper has no `monkeypatch`).
  4. `existing_values(folder)` returns a `set`.
  5. `roll_plan(start, 25, 10)` has quantities `[10, 10, 5]`; `roll_plan(start, 25, 10, 4)` has roll numbers `[4, 5, 6]`; `roll_plan(start, 25, 0) == []`.
  6. Counting sheet only (`sheet.counts`): `a = take_numbers("JOB-1", 10)`, `b = take_numbers("JOB-2", 5)` give `b["start"] == a["end"] + 1` and `next_start() == b["end"] + 1`; `next_start()` called twice returns the same number (previewing reserves nothing); after `void(b, "test")` the next `take_numbers("JOB-3", 1)["start"] == b["end"] + 1` and `jm.SHEETS.history(sheet.key)` has a row whose note starts `VOID: test`; `take_numbers("JOB-9", sheet.series.max)` raises `jm.SHEETS.SheetSerialError` whose message contains `sheet.series.limit_text`.
  7. Non-counting sheet: `take_numbers("JOB-1", 10) is None` and `next_start() is None`.

- [ ] **Step 3: Write the failing tests in `tests/test_datasheets.py`.**
  - `test_registry_lists_go2_and_cdo_in_order`: `list(jm.DS.SHEETS) == ["go2_rand", "cdo_seq"]`; `jm.DS.get("cdo_seq").customer == "CDO"`; `jm.DS.get(None) is None`.
  - `test_customer_lookup_ignores_capitalization_and_spaces`: `jm.DS.canonical_customer("  cDo ") == "CDO"`; `jm.DS.for_customer("go2").key == "go2_rand"`; `jm.DS.canonical_customer("Acme") is None`.
  - `test_duplicate_key_or_customer_is_refused_at_registration`: registering a subclass instance with key `"go2_rand"`, and another with a new key but customer `"cdo"`, each raises `ValueError` matching `already`.
  - `test_texts_are_what_the_page_shows_today` (exact strings): GO2 `label == "GO2: random EPC (E2004 + 19 random hex)"`, `detail == "GO2 random EPC (E2004…)"`, `hint == "Every EPC starts with E2004 and is unique. The numbers are written as fixed values and never change."`, `summary_label == "EPCs"`, `summary_text == "{total} random, all E2004…"`; CDO `label == "CDO: serial + 8-digit hex EPC, counts up, never repeats"`, `detail == "CDO sequential serial + 8-digit hex EPC"`, `hint == "Serial counts up in one running series and is never reused. The EPC is the serial as 8 hex digits. Next free serial: {next}."`, `summary_label == "Serials"`, `summary_text == "{from} – {to} (EPC = 8 hex digits)"`.
  - `test_cdo_series`: `series == Series(3_768_677_565, 0xFFFFFFFF, "the largest number an 8-digit hex EPC holds")`; `jm.DS.get("go2_rand").series is None`.
  - `test_contract_holds_for_every_registered_sheet`, parametrized over the keys `["go2_rand", "cdo_seq"]`, calling `check_contract(jm, jm.DS.get(key))`.

- [ ] **Step 4: Run `python -m pytest tests/test_datasheets.py -q`.** Expected: errors/failures (`datasheets` does not exist).

- [ ] **Step 5: Implement `sheet_serials.register_series`.** Replace the literal `SEQ_SHEETS` with an empty dict plus `register_series`; store `limit_text` in the entry and use it in the `reserve` error as `f"That would run past {cfg['max']:,}, {cfg['limit_text']} (next free is {start:,})."`. Remove the CDO-specific comment block and move its facts (sample sheet ended at 3,768,677,564; 8 hex digits stop at `FFFFFFFF`) into `datasheets/cdo.py`.

- [ ] **Step 6: Implement `datasheets/base.py`.** `take_numbers` calls `sheet_serials.reserve(self.key, job_number, qty, who=who, note=note)`; `void` calls `sheet_serials.void`; `next_start` calls `sheet_serials.next_start(self.key)`; all three return `None`/do nothing when `series is None`. Provide the recipe from the spec ("Adding a sheet later", three steps) as the module docstring.

- [ ] **Step 7: Implement `datasheets/go2.py` and `datasheets/cdo.py`.** Each class sets the texts from Step 3 and delegates to the `generators` functions listed in Global Constraints: GO2 `file_names -> G.go2_file_names`, `write -> G.generate_go2_files(folder, label, qty, qty_db, exclude=existing)`, `check -> G.verify_go2_file(path, rows)`, `existing_values -> G.read_go2_epcs(folder)`, `roll_plan -> G.roll_plan_plain(qty, lpr, first_roll)`; CDO `file_names -> G.go2_file_names`, `write -> G.generate_cdo_files(folder, label, start, qty, qty_db)`, `check -> G.verify_cdo_file(path, start, rows)`, `roll_plan -> G.roll_plan_seq(start, qty, lpr, first_roll)`, series as in Step 3.

- [ ] **Step 8: Implement `datasheets/__init__.py`.** `SHEETS`, `register` (validates, stores, and calls `sheet_serials.register_series` when the sheet has a series), `get`, `for_customer`, `canonical_customer`; at the bottom import `go2` and `cdo` and register one instance each, GO2 first. In `app.py` add `import datasheets  # registers the data sheets and their number series` next to the other module imports (the app keeps working unchanged; Task 2 starts using it).

- [ ] **Step 9: Run `python -m pytest tests/test_datasheets.py -q`.** Expected: all pass. Then run `python -m pytest -q` and `git diff --exit-code tests/golden/`. Expected: all tests pass (313 plus the new ones), no diff in `tests/golden/`.

- [ ] **Step 10: Add a `datasheets/` row to the README file table ("One file per customer data sheet kind; see its docstring to add one") and commit.**

```bash
git add datasheets tests/test_datasheets.py tests/sheet_contract.py tests/conftest.py sheet_serials.py app.py README.md
git commit -m "Add datasheets package: GO2 and CDO as registered data sheets"
```

---

### Task 2: Job creation, add data and `/api/meta` use the registry

**Files:**
- Modify: `app.py` (imports `~36-44`; `create_job` docstring `~283-285`; `_read_new_job` `~321-332`; `_reserve_numbers`, `_roll_plan`, `_generate_data_files` `~445-472`; `_take_new_job_numbers` `~493-505`; `_write_new_job_files` `~519-527`; `_save_new_job` log text `~563`; `_load_job_for_addition` `~670`; `_preview_addition` `~690-704`; `_write_addition_files` `~726-735`; `api_meta` `~884-886`)
- Modify: `mock_demo/run_mock.py` (copy the `datasheets/` folder like `templates/`)
- Modify (test code only, patch targets): `tests/test_additions.py:261,291`, `tests/test_cdo.py:107-108`, `tests/test_data_sheet_safety.py:102-103,115-116,125-126,242-245`, `tests/test_job_build.py:88,113`, `tests/test_job_build_behaviour.py:172,177,182,300,305`
- Test: `tests/test_data_sheet_wiring.py`

**Interfaces:**
- Consumes: `datasheets.get / canonical_customer / SHEETS`, `DataSheet.take_numbers / void / next_start / write / roll_plan / file_names / existing_values` (Task 1).
- Produces: `/api/meta` `data_sheets` entries with keys `key, customer, label, detail, hint, counts, next_start, summary_label, summary_text, sequential` (`sequential` equals `counts`); top-level `cdo_next` kept. `app._reserve_numbers`, `_roll_plan`, `_generate_data_files` keep their current signatures (`kind` is `"standard"`, a sheet key, or `None`).

- [ ] **Step 1: Redirect the test patch points.** Wherever a listed line replaces `generate_go2_files`, `generate_cdo_files` or `roll_plan_seq` on `jm.app`, replace it on `jm.generators` instead (same replacement function). Delete the now-redundant restore lines `tests/test_cdo.py:108` and `tests/test_data_sheet_safety.py:244-245` (they re-bind `jm.app.generate_*` to the real function, which is no longer called). Lines patching `g.verify_*` are unchanged.

- [ ] **Step 2: Write the failing tests in `tests/test_data_sheet_wiring.py`.**
  - `test_app_no_longer_names_the_sheets`: read `app.py` as text; assert none of these identifiers appear: `CDO_SHEET`, `GO2_SHEET`, `DATA_SHEETS`, `generate_go2_files`, `generate_cdo_files`, `read_go2_epcs`, `go2_file_names`, `roll_plan_seq`, `sheet_customer`, `SHEETS_SEQ.reserve`, `SHEETS_SEQ.void`, `SHEETS_SEQ.next_start`.
  - `test_unregistered_stored_sheet_key_is_refused_not_a_crash`: create a CDO job (payload as in `tests/test_job_build_behaviour.py::CDO`), `UPDATE jobs SET data_sheet='retired_sheet'`; `jm.app.add_data(id, {"quantity": 10})` returns `({"error": "This job has no encoded data (no UPC, no data sheet), so there is nothing to add to."}, 400)`; `GET /api/jobs/<id>` via `jm.app.app.test_client()` returns 200.
  - `test_meta_keeps_old_keys_and_next_start_moves`: `GET /api/meta` lists `data_sheets` keys `["go2_rand", "cdo_seq"]`, every entry has all ten keys above, the CDO entry has `sequential is True`, `counts is True`, `next_start == 3_768_677_565 == meta["cdo_next"]`, the GO2 entry has `next_start is None`; after one CDO job of quantity 100 the CDO `next_start` and `cdo_next` are both `3_768_677_665`.

- [ ] **Step 3: Run the redirected suites and the new tests.** `python -m pytest tests/test_data_sheet_wiring.py tests/test_additions.py tests/test_cdo.py tests/test_data_sheet_safety.py tests/test_job_build.py tests/test_job_build_behaviour.py -q`. Expected: failures for the failure-injection tests (patches no longer reach the code) and for the three new tests.

- [ ] **Step 4: Switch `app.py` to the registry** (approach per site; no new helpers beyond what exists):
  - Imports: drop `generate_go2_files, generate_cdo_files, DATA_SHEETS, GO2_SHEET, CDO_SHEET, sheet_customer, roll_plan_seq, read_go2_epcs, go2_file_names` from the `generators` import; keep `roll_plan_plain` (plain special jobs); add `import datasheets as DS` (the Task 1 bare import becomes this one); keep `SHEETS_SEQ` only for `SHEETS_SEQ.SheetSerialError`.
  - `_read_new_job`: customer via `DS.canonical_customer(...) or s.customer`; `sheet = DS.get(s.data_sheet)`; unknown key keeps the message `Unknown data sheet.`; the customer/type refusal keeps its text using `sheet.customer`.
  - `_reserve_numbers`: standard branch unchanged; otherwise `sheet = DS.get(kind)`, `alloc = sheet.take_numbers(job_number, qty, who=who, note=note)`; if `alloc` register `build.took("sheet numbers", undo=lambda why: sheet.void(alloc, why))`; return `alloc` (`None` for no series).
  - `_roll_plan`: standard unchanged; a sheet key uses `DS.get(kind).roll_plan(start, qty, lpr, first_roll)`; `None` keeps `roll_plan_plain`.
  - `_generate_data_files`: standard unchanged; a sheet key uses `DS.get(kind).write(folder, label, qty, qty_db, start=start, **opts)` (`opts` is `existing=` for add data, empty for new jobs).
  - `_take_new_job_numbers`: the sheet branch becomes `elif s.kind:` and only reserves when `DS.get(s.kind).series`; error texts unchanged, with `f"{sheet.customer} serial allocation failed, job not created: {e}"` for the unexpected case (renders `CDO serial allocation failed…` for CDO).
  - `_write_new_job_files`, `_save_new_job`: customer text from `DS.get(kind).customer`.
  - `_load_job_for_addition`: kind is `"standard"` for a UPC job, the job's `data_sheet` when `DS.get(...)` finds it, else `None` (existing refusal).
  - `_preview_addition`: for a sheet, `nxt = sheet.next_start()`; `serial_start`/`serial_end` are `nxt` and `nxt + qty - 1`, or `None`/`None` when `nxt is None`; `files=sheet.file_names(a.label, n_files)`. The standard-job branch is unchanged.
  - `_write_addition_files`: standard keeps `{"tag": a.tag}`; a sheet passes `existing=sheet.existing_values(a.folder)` computed inside the same `try`.
  - `api_meta`: build `data_sheets` from `DS.SHEETS.values()` with the ten keys above; `cdo_next` = the `next_start` of the `cdo_seq` entry (this is the one allowed place the old key is named).
  - Rewrite the `create_job` docstring lines that describe the two sheets to point to `datasheets/`.

- [ ] **Step 5: Make the sandbox runner copy the package.** In `mock_demo/run_mock.py`, next to the `templates` copy, remove and re-copy `datasheets/` into the sandbox site (ignore `__pycache__`).

- [ ] **Step 6: Run the same six suites plus the new file.** Expected: all pass. Then `python -m pytest -q` (all pass) and `git diff --exit-code tests/golden/` (no diff).

- [ ] **Step 7: Sandbox check.** `python mock_demo/run_mock.py --reset` in the background, then `curl -s --noproxy '*' http://127.0.0.1:5051/api/meta`. Expected: HTTP 200 and `data_sheets` with two entries; stop the sandbox afterwards.

- [ ] **Step 8: Commit.**

```bash
git add app.py mock_demo/run_mock.py tests
git commit -m "Job creation, add data and /api/meta read data sheets from the registry"
```

---

### Task 3: The New job page reads sheet text from `/api/meta`; a third sheet works with no edits elsewhere

**Files:**
- Modify: `templates/index.html:1371` (`id="n-go2"` → `id="n-sheet-wrap"`), `:2049` (`$('#n-go2')` → `$('#n-sheet-wrap')`), `sheetChanged()` (`~2091-2092`), `openNewJob()` (`~2107`), `updateSummary()` (`~2125-2126`)
- Modify: `docs/superpowers/specs/2026-11-11-data-sheet-plugins-design.md` (status line)
- Test: `tests/test_data_sheet_wiring.py` (append)

**Interfaces:**
- Consumes: the `/api/meta` `data_sheets` entries from Task 2 (`hint` with `{next}`; `summary_label`; `summary_text` with `{from}`, `{to}`, `{total}`; `next_start`); `check_contract` from Task 1.
- Produces: nothing for later tasks.

- [ ] **Step 1: Write the failing tests (append to `tests/test_data_sheet_wiring.py`).**
  - `test_new_job_page_has_no_sheet_specific_code`: read `templates/index.html` as text; assert none of `n-go2`, `cdo_next`, `sequential`, `E2004`, `8 hex digits` appear.
  - `test_a_third_sheet_works_with_no_edits_elsewhere`: define `FakeSheet(DataSheet)` in the test (key `"fake_seq"`, customer `"FAKE"`, texts of your choosing, `series=Series(100, 10_000, "the fake limit")`; `write` creates one xlsx per `file_names` entry under `<folder>/Data` with a header row plus one row per number and raises/cleans up like the real ones; `check` raises `ValueError` when the row count differs; `roll_plan` delegates to `generators.roll_plan_plain`). Then: `jm.DS.register(FakeSheet())`; `check_contract(jm, jm.DS.get("fake_seq"))` passes; `create_job({"customer": "Fake", "title": "F", "job_ticket": "F1", "job_type": "special", "data_sheet": "fake_seq", "quantity": 30, "lpr": 10})` returns 201 with `job["customer"] == "FAKE"`, `rolls == 3`, `serial_alloc["start"] == 100` and its file on disk; `add_data(job_id, {"quantity": 15})` returns 201 and `addition["serial_start"] == 130`; a second job takes numbers starting at `145`; `/api/meta` lists the fake sheet with `next_start == 160` after that job of 15 labels (use quantity 15 for the second job).

- [ ] **Step 2: Run `python -m pytest tests/test_data_sheet_wiring.py -q`.** Expected: `test_new_job_page_has_no_sheet_specific_code` fails; the fake-sheet test passes if Task 2 is complete (if it fails, fix the gap in `app.py`, not the test).

- [ ] **Step 3: Update the page.**
  - Rename the container id and its one reference.
  - `sheetChanged()`: hint is `prob || (!d ? 'No data sheet will be written for this job.' : d.hint.replace('{next}', fmt(d.next_start)))`.
  - `updateSummary()`: for the selected sheet and a quantity, push `[sd.summary_label, sd.summary_text.replace('{from}', fmt(sd.next_start)).replace('{to}', fmt(sd.next_start + total - 1)).replace('{total}', fmt(total))]` (replaces the sequential/random ternary).
  - `openNewJob()`: the refresh becomes `api('/api/meta').then(m => { META.data_sheets = m.data_sheets; sheetChanged(); })`.
  - Search the file for any remaining `cdo_next`, `sequential`, `E2004`, `8 hex digits`; there must be none.

- [ ] **Step 4: Run `python -m pytest tests/test_data_sheet_wiring.py -q`.** Expected: all pass.

- [ ] **Step 5: Verify the page in the sandbox.** Start `python mock_demo/run_mock.py --reset` in the background, then drive the page with Playwright (`require('playwright')` works from any directory; no `--trace`): open `http://127.0.0.1:5051/`, `await page.evaluate(() => openNewJob())`, fill `#n-customer` with `cdo` (fire `input`), then `#n-qty` with `100`. Expected, read from `#n-sheet-hint` and `#n-summary`: hint matches `^Serial counts up in one running series and is never reused\. The EPC is the serial as 8 hex digits\. Next free serial: [\d,]+\.$`; summary has a row `Serials` whose text matches `^[\d,]+ – [\d,]+ \(EPC = 8 hex digits\)$`. Change the customer to `go2`: hint equals `Every EPC starts with E2004 and is unique. The numbers are written as fixed values and never change.` and the summary row is `EPCs` / `100 random, all E2004…`. Fail the step on any console error; stop the sandbox afterwards.

- [ ] **Step 6: Full verification.** `python -m pytest -q` (all pass) and `git diff --exit-code tests/golden/` (no diff).

- [ ] **Step 7: Mark the spec implemented** (status line `Status: implemented`) **and commit.**

```bash
git add templates/index.html docs/superpowers/specs/2026-11-11-data-sheet-plugins-design.md tests/test_data_sheet_wiring.py
git commit -m "New job page reads data sheet text from /api/meta; a third sheet needs no edits elsewhere"
git push -u origin claude/laughing-wright-s1z34w
```
