# RFID proofing in Job Manager

The RFID Proofing Utility (V 1.0.0 Beta 21.4, Lean Core) now runs inside Job Manager. There is one
server, on the host PC, and everyone opens proofing from their browser:

- **Sidebar > Proofing**, or `http://<host>:5050/proofing`
- **A job's drawer > Proof > Start proof / Open proof** (opens the proof for that job, creating it the first time)

Everything the standalone app did is still there: Home / Proof / Queue / Settings, BarTender preview
(Record 1 / Page 1), image preview, SGTIN-96 calculation and the encoding inspector, validation,
the editable customer PDF, customer package, revision guard, approval lock, recovery snapshots,
template profiles and the inlay library.

The proofing page uses Job Manager's look: the same sidebar (Jobs, Roll tracking, Archive, Proofing,
Templates, Tools, Settings), colours, buttons, tables and status chips. Its own pages are tabs at the
top: **Jobs & proofs**, **Proof**, **Queue** and **Settings**.

## Jobs & proofs

The first tab lists Job Manager's open jobs (not archived or cancelled) with each one's proof state.
**Needs a proof** (the default) shows jobs with no proof, a proof that isn't approved yet, or an
approved proof the job has since changed; **All open jobs** shows them all. Search by ticket, job #,
customer, item or UPC.
**Start proof** creates the proof from the job; **Open proof** opens the existing one.

## Sync with the job

A linked proof and its job stay in step on these fields: customer, ticket (Job #), part #, quantity,
UPC (GTIN and barcode 1), label size, serial and its EPC, inlay, template, and PO (PO / data reference).

- **Job → proof.** When a job is edited (or its template changes), its proofs are updated at once;
  a background check every 30 seconds catches anything else. A proof is also checked whenever it is
  opened or saved. A field is only replaced if nobody changed it on the proof: each proof remembers
  the job's values from the last sync, and a value typed on the proof is kept.
- **Kept values are reported.** The proof page shows a yellow bar, *Differs from JOB-…*, listing each
  field with the proof's value and the job's, and **Use job value** / **Use all job values** buttons.
  The Jobs list and drawer show *Proof differs from job*.
- **Approved and archived proofs are never changed.** If the job changes after approval, the proof is
  flagged *Proof out of date* in the Jobs list, drawer, Queue and Jobs & proofs tab, and the proof page
  offers **Start next revision**.
- **Proof → job.** Saving a proof fills the job's part #, PO # and inlay **only where the job has none**.
  It never overwrites job data. The change is logged in the job's activity.
- **An open proof updates itself.** If its job changes while the page is open and nothing has been typed,
  the page reloads the proof and says what changed ("Updated from JOB-… at 14:05: Part #, PO … Regenerate
  the preview and the PDF before sending"). If something has been typed, nothing is replaced; it says the
  proof changed on the server and offers **Reload**.
  Every sync is in the job's activity as *Job sync*.

## What Job Manager adds

| | |
|---|---|
| **Start proof from a job** | Filled in from the job: customer, ticket (as Job #), part #, quantity, UPC (GTIN and barcode 1), label size, inlay (matched by name in the inlay library), template (the job's Print copy, else the Master Templates match by UPC), PO as the "PO / Data Reference", and the job's **first serial and its EPC, encoded the way Job Manager encodes them** (filter 1, 7-digit company prefix). CDO jobs get the serial as 8 hex digits; GO2 jobs are marked as random EPCs. |
| **Outputs in the job folder** | A linked proof's PDF and package are written to the job's `Proof` folder (earlier versions move to `Proof\archive`). Unlinked proofs still use the proofing output folder. |
| **Job state** | The Jobs list shows a chip (Proof draft / generated / customer review / approved / changes requested). The drawer lists the job's proofs with a link to each PDF. Proof events (started, PDF written, status changes) appear in the job's activity with the person's Job Manager name. |
| **Cross-checks** | Warnings (not blockers) when a linked proof's GTIN isn't the job's UPC, its EPC isn't what Job Manager encodes for its serial, or its serial is outside the job's range. |
| **Templates** | **Choose** searches Job Manager's Master Templates index; the job's own template and UPC matches are listed first. |
| **Backups** | Job Manager's backups include proof records, history, proofing settings and the inlay library. A restore brings back missing proof files and never overwrites existing ones. Previews and generated PDFs are not in the zip (PDFs are in the job folders; previews can be regenerated). |
| **Linking** | A proof can be linked to a job (**Link to job** under the proof title), and **Settings > Link Proofs to Jobs** links every proof whose Job # is a Job Manager ticket. |

## Setup on the host PC

1. Check out the branch and run `run.bat` once. It installs **Pillow** and **reportlab** (and the
   others) the first time. `pywin32` is optional (only for BarTender's COM diagnostics).
2. In `config.ini`, set where proof records live:
   ```ini
   [proofing]
   data_dir = Z:\3 Encoding and Printing Files\Operations\proofing_data
   ```
   Blank keeps them in `proofing_data` next to `app.py` (the Data panel then warns "This PC only").
   If the folder can't be reached, Job Manager still starts; the Proofing page says what's wrong.
3. **BarTender must be installed on the host PC**, the one that runs Job Manager. Previews render
   there, one at a time. If it isn't found automatically, enter the path to `bartend.exe` under
   Proofing > Settings.
4. **Inlays**: under Proofing > Settings, enter the inlay source folder (as the host PC sees it) and **Sync Inlays**.
5. **Existing proofs**: stop Job Manager and drag the old *Proofing Test* folder onto
   `import_proofing.bat`. It shows a preview first and never overwrites anything. Then start Job
   Manager; imported proofs are linked to jobs by ticket.

## Bugs found in the standalone app (fixed here)

These are in the Beta 21.4 / 21.5 files that were provided, not in Job Manager:

1. **Generate Proof crashed** ("handle_pageBegin … pop from empty list") for every proof saved by the
   current page. The "Customer Variable Data Protection V12" change drew the variable-data page by
   calling `showPage()` inside the page header/footer callback. Fixed in `proofing/core/proof_pdf.py`:
   that page is now added after the proof as a page of its own.
2. **The page stopped at startup.** The V12 script read `state.job.po_data_reference` before a proof
   existed, so `bootstrap()` never ran (BarTender card stuck on "Checking", buttons dead). Fixed in
   `proofing/static/app.js`.
3. **All proofs shared one preview file.** Every BarTender preview was written to
   `%LOCALAPPDATA%\RFID_Proofing_Utility\cache\bartender\preview_1.png`, so reopening an older proof
   showed (and could reuse) whichever label was rendered last. Previews are now kept per proof in
   `<data_dir>\previews\<proof>\`.
4. **Different EPC filter from Job Manager.** The proofing app defaults to filter 3; Job Manager
   encodes filter 1. For example, the approved Levata 1987006 proof shows `307434401C00AF8000000001`,
   but Job Manager's database holds `303434401C00AF8000000001` for that serial. Proofs started from
   a job now use filter 1, and linked proofs warn when they differ. Check other already-approved
   proofs that were sent to customers.

## Changes made because the server is shared

- No shutdown after 5 minutes without a heartbeat, and no `/api/shutdown`. Closing a browser tab never stops Job Manager.
- The Windows file pickers that opened on the host PC are gone. Instead you pick templates from the
  index and upload preview images, and you type the BarTender/inlay paths in Settings. Outputs open in the browser.
- Paths from the browser are only accepted inside the proofing data folder (proof records) or the
  job folders (outputs). The BarTender setting only accepts `bartend.exe`.
- Saving checks that nobody else saved the same proof since you opened it, and asks before
  overwriting. A new proof never overwrites another one with the same customer / job / revision.
  Two people pressing **Start proof** at once get one proof.
- Proofing **Settings are shared by everyone** (preparing company, DPI, BarTender path, disclaimer).
  Operator initials default to the initials of the name set in Job Manager on that PC.

## Files

| Path | What |
|---|---|
| `proofing/core/` | The proofing app's `app/` package. Changed: `settings.py` (data folder from config.ini), `models.py` (`jm_job_id`, `jm_job_number`, `jm_sync`), `package_export.py` (link and sync fields kept out of customer packages), `proof_pdf.py` (fix 1) |
| `proofing/routes.py` | Its server (`support/runtime/web_server.py`) as a Flask blueprint at `/proofing`, plus the Job Manager link, the jobs list and the sync (`sync_proof`, `reconcile`, `job_changed`) |
| `proofing/jm_link.py` | `job_proofs` table: which proof belongs to which job and its sync state (rebuilt from the proof files at startup) |
| `proofing/__init__.py` | Registration, fallback page when Pillow/reportlab are missing, Data panel row, backup sources |
| `proofing/static/` | The proofing page (`index.html`, `app.js`, `styles.css`), adapted for `/proofing` and restyled as Job Manager (changes marked `JM:`). Element IDs are unchanged |
| `proofing/assets/branding/` | Logos printed on the proof |
| `import_proofing.py`, `.bat` | Import from the standalone app |
| `tests/test_proofing.py` | Proofing tests |

Changed Job Manager files: `app.py` (import, `init_schema`, proof state on `/api/jobs`, backups, Data panel,
registration, `proofing.job_changed` after a job edit), `config.py` / `config.ini.example` (`[proofing] data_dir`), `backup.py` / `restore_backup.py`
(proof records), `templates/index.html` (Proofing nav, Jobs list chip incl. sync state, drawer Proof section, `/?job=<id>`, `/?view=<page>`),
`requirements.txt`, `run.bat`, `run.sh`, `mock_demo/run_mock.py`, `tests/conftest.py`.

## Tests

`python -m pytest -q` runs the existing tests plus `tests/test_proofing.py`, covering: start from a job
(EPC equals Job Manager's), save conflicts, path safety, the PDF in the job's Proof folder, approval,
the EPC warning, backup and restore of proof records, relinking, deleting a job with a proof, the import,
the fallback page, concurrent Start proof, and the sync (job edits reach untouched proof fields, typed
values are kept and reported, blank job fields are filled from the proof, approved proofs are only
flagged, edits apply without waiting for the background check, the jobs list, and the open page
noticing a change).

`tests/test_snapshot.py::test_allocations_during_backups_never_cause_false_failures` fails on the
uploaded `main` as well, before any of this: concurrent `registry_ok(force=True)` calls in one process
share the `.write_check_<pid>` file name.
