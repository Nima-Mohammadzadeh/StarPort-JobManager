# Job Manager 3.3 — encoding team edition

One host PC runs the app. Everyone else uses it in a browser at `http://<host-ip>:5050`.
The full guide (how it works, where everything is stored, how it compares with the old
app, and a test checklist) is the separate **Job Manager 3.0 documentation**.

## Start it

1. Copy this folder to the host PC, for example `C:\JobManager`.
2. Edit `config.ini` (created from `config.ini.example` on first start):
   - `z_drive_base` = the shared customer folder. Use the network path
     (`\\10.44.4.15\Starport Tech Shared Drive\3 Encoding and Printing Files\Customers Encoding Files`).
   - `database` and `serial_registry` blank = next to `app.py` on the host.
3. **New install only:** double-click `new_install.bat` once. It makes the empty database.
4. Double-click `run.bat`. It installs Flask, openpyxl and waitress the first time.
5. In Settings, set the **Backup folder** on the share. It also turns on the serial journal.
   Backups run every 6 hours by default (adjustable). Each zip is a full snapshot and is only
   kept as a good backup if its serial history checks out. To restore, see `RESTORE.md`.

## Updating the app

Close the app, copy the new files over the existing Job Manager folder, and start `run.bat`.
`local.db`, `serial_registry`, `config.ini` and `backups` are yours and are not part of the app files,
so they stay as they are. The first start after an update brings the database up to date by itself
(it prints `updated to version N`).

Job Manager never creates a database by itself. If it is started from a folder that has no database
(for example a new copy unzipped somewhere else), it stops and says so instead of quietly starting
a second, empty one. A database newer than the app is refused the same way.

## What it does

- **Jobs**: open jobs, search, status, roll progress, a **Priority** card you can
  reorder, and a recent activity feed.
- **New job**: Standard (UPC + serials) or **Special** (no UPC, nothing outside the
  normal process gets serials). Folder: `Customer\Label size\MM.DD.YY - <ticket>`.
- **GO2 data sheet** (customer GO2 only): typing the customer as GO2 (any capitalization) switches
  New job to Special and selects the **random EPC** data sheet: `E2004` + 19 random hex digits, all
  unique, written into the job's Data folder (split by Qty per DB file if set). Values only, never
  formulas, and every file is re-read and checked before the job is created, so the prefix can't
  drift (the old sheet counted up to `E2005`, `E2006`... after row 100,000). Choose **None** to skip it.
- **CDO data sheet** (customer CDO only): `Serial` + `EPC`, where EPC is the serial as 8 hex digits (the old
  `=DEC2HEX(serial,8)` sheet, written as values). One running series for the customer: the first job starts
  at 3,768,677,565 (the sample sheet ended at 3,768,677,564) and every job after continues where the last
  ended, so a number is never used twice. The series is kept in the database, `serial_registry/data_sheets/cdo_seq.txt`
  and `sheet_serial_journal.csv` in the backup folder; the next number is past the highest of the three. A job
  that fails after taking numbers leaves a marked gap, never a repeat. It stops at `FFFFFFFF`.
- **Picking a sheet**: the Customer data sheet box always shows on Special jobs. Typing a customer that has a sheet
  (GO2, CDO, any capitalization) switches to Special and selects it; a sheet only works for its own customer.
- **One job per ticket**: a ticket that already has a job can't be used again (cancel
  the old job first to redo it). The form warns as soon as the ticket is typed, and
  the server refuses even if two people enter it at the same instant.
- **Data and backups** (Settings, first panel): lists where each kind of data lives (app
  database, serial registry, serial journal, job folders, Master Templates, config.ini), whether it
  can be reached, and whether the backups cover it, with plain warnings: no backup, backup
  overdue, backups only on this PC, serial registry offline. Every backup (database,
  registry, previews, config.ini) is checked by opening its copy of the database, and **Check the
  latest backup** repeats that on demand. With no backup folder set, backups go to a `backups`
  folder next to the app; set a folder on the share for real protection (it also turns on the
  serial journal). A header warning appears whenever backups need attention.
- **Rewind page (Android tablets)**: a separate page just for the rewind operators, at
  `http://<host-ip>:5050/rewind`. It isn't in the desktop menu; they open it by scanning
  the QR sign (Settings, Rewind page, Print QR sign) and add it to the home screen.
  They pick a job from the open jobs in encoding (new jobs appear by themselves within
  seconds), see every roll as on the printed sheet, and **scan a tag with the fob**:
  Serial Magic types it in and the page jumps to that roll and shows which label it is,
  calling out the FIRST and LAST tag. The job list's search box also takes a scan.
  Lookups are read-only.
- **Add data to a job**: if a job needs more labels after it was made, open it and choose **Add data**
  (standard, CDO and GO2 jobs; not archived ones). The preview shows the new serial range, rolls and files
  before anything is taken. The extra labels get new serials that are never reused (they may not sit next
  to the original range), new rolls that continue the numbering, and new `_ADD<n>` data files; existing
  files and rolls are never changed. A completed job reopens. The job's own quantity and start serial stay
  as ordered; each addition is kept in the `job_additions` table, which the serial safety nets, backups
  and the restore tool all read.
- **Roll tracking**: pick any roll; Complete, Left in progress (with a note), Reopen.
  The app asks who did it at that moment. Nobody signs in.
- **Tools**: roll tracker sheet, checklist, encoding database, serial lookup.
- **BarTender**: the template is copied into `Print\`. Each job shows **Copy path** buttons
  for the template and the encoding database so they can be pasted into BarTender's file box.
- **Templates**: Master Templates index, picked automatically by UPC.

## Serial numbers, in short

Every range is kept in three places: the **registry** (`serial_registry\<UPC>.txt`),
a copy in the **app database**, and the **journal** in the backup folder. The next serial
is past the highest end in any of them, so one lost or rolled-back record can't cause
reuse. If the registry can't be reached, a job only gets serials after someone confirms
the start serial; the range is written to the registry when it is back, and any overlap
is flagged as a conflict.

## Files

| File | Purpose |
|---|---|
| `app.py` | Server and API |
| `db.py` | The one way to open the app database (connections close themselves) |
| `migrations.py` | Database version and the numbered steps that update it, each run once |
| `serial_store.py` | Registry + database copy + journal, health, sync |
| `serial_registry.py` | Per-UPC registry files with locks |
| `generators.py` | EPC encoding, UPC checks, databases, roll plans, QC sheet |
| `checklist_generator.py` | Encoding checklist |
| `config.py`, `config.ini.example` | Paths and port |
| `lists.py` | Starter customer and label size lists |
| `templates/index.html` | The desktop interface |
| `templates/rewind.html` | The rewind operators' page (tablets, phones) |
| `run.bat`, `run.sh` | Start the app |
| `new_install.bat` | Make the empty database for a brand-new install (once) |
| `import_old.py`, `.bat` | Bring data over from the old app |
| `seed_serials.py`, `.bat` | Find serials used before the app and reserve them |
| `backup.py` | Snapshot zips and their serial check |
| `restore_backup.py`, `.bat` | Emergency restore (see RESTORE.md) |
| `tests/` | Automated tests (`pip install pytest`, then `python -m pytest -q`) |
