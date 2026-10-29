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
3. Double-click `run.bat`. It installs Flask, openpyxl and waitress the first time.
4. In Settings, set the **Backup folder** on the share. It also turns on the serial journal.
   Backups run every 6 hours by default (adjustable). Each zip is a full snapshot and is only
   kept as a good backup if its serial history checks out. To restore, see `RESTORE.md`.

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
| `serial_store.py` | Registry + database copy + journal, health, sync |
| `serial_registry.py` | Per-UPC registry files with locks |
| `generators.py` | EPC encoding, UPC checks, databases, roll plans, QC sheet |
| `checklist_generator.py` | Encoding checklist |
| `config.py`, `config.ini.example` | Paths and port |
| `lists.py` | Starter customer and label size lists |
| `templates/index.html` | The desktop interface |
| `templates/rewind.html` | The rewind operators' page (tablets, phones) |
| `run.bat`, `run.sh` | Start the app |
| `import_old.py`, `.bat` | Bring data over from the old app |
| `seed_serials.py`, `.bat` | Find serials used before the app and reserve them |
| `backup.py` | Snapshot zips and their serial check |
| `restore_backup.py`, `.bat` | Emergency restore (see RESTORE.md) |
| `tests/` | Automated tests (`pip install pytest`, then `python -m pytest -q`) |
