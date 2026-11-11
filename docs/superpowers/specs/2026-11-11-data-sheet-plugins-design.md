# Data sheets as plug-ins — design

Date: 2026-11-11 · Status: awaiting review

## Purpose

Some customers (GO2, CDO) get a special job whose data file is not the normal UPC encoding database
(`Serial # | UPC | EPC`, SGTIN-96) but a customer-specific sheet. Today those two sheets are wired into the
server and the New job page by name, in about 25 places. Adding a third kind of sheet means editing job
creation, add data and the page.

After this change a data sheet *kind* is one self-contained definition. A developer adds a new kind by
writing one file and registering it; job creation, add data, the page and the safety rules need no edits.

## Decisions (agreed)

- **Who adds a sheet:** a developer, in code. No Settings screen, no building sheets from the browser.
- **This change is a restructure only.** GO2 and CDO become the first two sheets and behave exactly as
  today. No new sheet kind ships here (for example an SGTIN-96 customer sheet): that is a separate change on
  top of the new interface.
- **Serial rules never loosen.** A number is never handed out twice; every file is re-read and checked after
  it is written; a file that fails the check is removed and the job is not created.

## Where GO2 and CDO are wired in today

| Place | What it does by name |
|---|---|
| `generators.py` `DATA_SHEETS` | The two sheets: customer, label, detail, `sequential` flag |
| `app.py` `_reserve_numbers`, `_roll_plan`, `_generate_data_files`, `_take_new_job_numbers`, `_write_new_job_files` | `if kind == CDO_SHEET … else go2` |
| `app.py` `_preview_addition`, `_write_addition_files` | Same branching; GO2 also passes `exclude=read_go2_epcs(folder)` |
| `app.py` `/api/meta` | Sends the sheet list and a CDO-only `cdo_next` |
| `sheet_serials.py` `SEQ_SHEETS` | The one counting series (`cdo_seq`: first 3,768,677,565, max `FFFFFFFF`) |
| `templates/index.html` | `sheetChanged()` hint text and `updateSummary()` rows written separately for "sequential" and "random"; `META.cdo_next` |

Both functions `create_job` and `add_data` already work as lists of steps with one undo path (`job_build.py`),
so the only code that has to change is the code that names a sheet.

## The interface

A new package `datasheets/` holds the interface, the registry and one file per sheet.

```python
class DataSheet:
    # identity, sent to the page
    key: str            # stored in jobs.data_sheet, e.g. "cdo_seq"
    customer: str       # canonical customer name, e.g. "CDO"
    label: str          # option text in the New job picker
    detail: str         # short text on the job page
    hint: str           # help line under the picker; may contain {next}
    summary_label: str  # row label in the New job summary, e.g. "Serials" / "EPCs"
    summary_text: str   # row text; may contain {from} {to} {total}

    # numbers: a sheet that counts declares a series, the framework does the taking
    series: Series | None      # Series(first, max, limit_text) or None (GO2 takes no numbers)

    # files
    def file_names(self, label, parts) -> list[str]
    def write(self, folder, label, qty, qty_db, start=None, existing=()) -> {"files": [...]}
    def check(self, path, start, rows) -> None      # re-open one file; raise ValueError on any problem
    def existing_values(self, folder) -> set        # values already in the job (GO2: its EPCs); default: empty

    # rolls
    def roll_plan(self, start, qty, lpr, first_roll=1) -> list
```

Rules the interface enforces:

- `write()` writes the file(s) **and** runs `check()` on each. On any failure it deletes what it wrote and
  raises, so there is never a half-written sheet.
- **Taking and voiding numbers is written once, in the framework, not per sheet.** A sheet with a `series`
  gets `reserve` and `void` from `sheet_serials.py`, keyed by its `key`. A new sheet can't get the
  never-repeat logic wrong because it doesn't write any. (This refines the earlier sketch, which had each
  sheet implement `take_numbers` and `void`.)
- `series.limit_text` carries the sheet-specific half of the "ran past the largest number" error, so CDO's
  message stays word for word what it is today ("…the largest number an 8-digit hex EPC holds…").
- `existing_values()` is how add data avoids repeating a value (GO2 reads the EPCs already in the job's Data
  folder). It is passed to `write()` as `existing`.

## The registry

`datasheets/__init__.py`:

- `SHEETS`: ordered mapping `key → DataSheet`. Registering a duplicate key or a duplicate customer name
  raises at import, so a bad registration fails loudly at startup, not on the shop floor.
- `get(key)`, `for_customer(name)` (any capitalization), `canonical_customer(name)`.
- Registering a sheet with a `series` registers the series with `sheet_serials.py` (replacing its hard-coded
  `SEQ_SHEETS`).

`datasheets/go2.py` and `datasheets/cdo.py` hold the two sheets. **They wrap the existing functions in
`generators.py`** (`generate_go2_files`, `verify_go2_file`, `generate_cdo_files`, `cdo_epc`, …) instead of
moving them. That keeps the diff small and the behaviour byte-for-byte identical. Moving the code into the
sheet files is cosmetic and can happen later.

## What job creation, add data and the page do afterwards

**Server (`app.py`).** The branching on `CDO_SHEET` / go2 is replaced by calls on the sheet object:

- `_reserve_numbers`: `if sheet.series:` reserve from the series, record `build.took("sheet numbers",
  undo=void)`. Standard jobs (UPC serials) are untouched.
- `_roll_plan`, `_generate_data_files`: `sheet.roll_plan(...)`, `sheet.write(...)`.
- `_preview_addition`: `sheet.file_names(...)`, plus the next number if it counts.
- `_load_job_for_addition`: a job's kind is `"standard"` or `jobs.data_sheet`, resolved through the registry.
- `create_job` / `add_data` themselves do not change.

**`/api/meta`.** Each entry in `data_sheets` carries `key, customer, label, detail, hint, counts, next_start,
summary_label, summary_text`. Existing keys (`sequential`, `cdo_next`) stay for now so nothing reading them
breaks; the page stops using them.

**New job page.** `sheetChanged()` and `updateSummary()` read the text from the sheet definition, substituting
`{next}`, `{from}`, `{to}`, `{total}`, instead of having CDO and GO2 wording built in. The "series moves as
jobs are made" refresh reads `next_start` from each counting sheet, not `cdo_next`. The section that holds the picker is
currently `id="n-go2"`; it is renamed `n-sheet-wrap` (two references). Because
`templates/index.html` is rendered by Jinja, any new JavaScript must avoid `${{` (known gotcha).

**Database.** None. `jobs.data_sheet` already stores the key, and keys do not change (`go2_rand`,
`cdo_seq`). No migration.

## Safety net and tests

1. **Nothing may change for GO2 and CDO.** The 59 recorded scenarios in `tests/golden/job_build.json` must pass
   with **no edits to the recorded results**. The 313 existing tests must pass.
2. **Test fault-injection points move.** 21 patch points across 5 test files (`test_additions`, `test_cdo`,
   `test_data_sheet_safety`, `test_go2`, `test_job_build_behaviour`) replace functions by name
   (`monkeypatch.setattr(jm.app, "generate_go2_files", …)`). After the change `app` no longer calls those names;
   the sheet does. Those patches are redirected to where the sheet calls them. This is a change to test code
   only, and the recorded expected results stay as they are.
3. **A contract test runs against every registered sheet** (so a future sheet gets it for free):
   - `write()` produces files that `check()` accepts, and the names match `file_names()`;
   - a tampered file (changed cell, missing row, repeated value) is rejected by `check()`;
   - a `write()` that fails leaves no files behind;
   - for sheets with a series: two reservations never overlap, `void` leaves a gap and never a repeat,
     preview reserves nothing, and the series refuses to run past its `max` with the sheet's own message;
   - lookup by customer works in any capitalization; keys and customers are unique.
4. **Sandbox run:** create and extend a GO2 job and a CDO job end to end, as before.

## Adding a sheet later (the recipe this enables)

1. Add `datasheets/<name>.py` with a `DataSheet` subclass: identity text, optional `series`, `file_names`,
   `write`, `check`, `roll_plan`.
2. Register it in `datasheets/__init__.py`.
3. The contract test picks it up automatically; add sheet-specific tests for its file format.

Illustration only, **not part of this change**: an SGTIN-96 customer sheet would declare a series
(`first=1`, `max=2**38-1`), write `Serial # | UPC | EPC` rows through `generate_epc()`, and check each row by
decoding the EPC. It needs a UPC for the customer, which is the extension point below.

## Not included

- A Settings screen for building sheets.
- Any new sheet kind. The first one follows once this lands.
- **Extra input fields per sheet** (for example a customer-supplied UPC). The interface has no `inputs`
  declaration yet; it gets one when the first sheet needs it, so GO2 and CDO don't carry unused machinery.
- Moving the GO2 and CDO generator code out of `generators.py`.
- Changing any behaviour. In particular these three existing oddities are kept exactly as they are and stay
  covered by the recorded scenarios: a failed final save on a new job leaves its row, folder and files behind;
  a voided CDO range can point at a job number that is then reused; a standard job is still created when its
  encoding database can't be written (warning only).

## Risks

- **The page is vanilla JavaScript in one large file.** The change is small (two functions), but there is no
  automated browser test; it is verified in the sandbox and by the meta-endpoint tests.
- **`cdo_next` and `sequential` in `/api/meta`.** `tests/test_cdo.py::test_meta_lists_both_sheets` reads
  `cdo_next`. Both keys stay in the response for now; removing them (and updating that test) is a later clean-up.
- **Registration errors at import.** Intentional (fail at startup), but it means a typo in a new sheet stops
  the app from starting, which the host-PC update steps should note.

## Acceptance

- GO2 and CDO jobs (create, preview, add data, failure paths) behave identically: the 59 recorded
  scenarios and all existing tests pass with their expected results unchanged.
- No code in `app.py` or `templates/index.html` names `GO2`, `CDO`, `go2_rand`, `cdo_seq`, `cdo_next` or
  `n-go2`, except the two sheet files, their tests, and the compatibility keys in `/api/meta`. (Comments and
  docstrings that give GO2 and CDO as examples are fine.)
- A trivial third sheet registered inside the contract test (a fake) passes the contract test and is
  usable through `create_job` and `add_data` with no edits outside its own file.
