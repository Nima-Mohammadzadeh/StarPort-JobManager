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
   - `[proofing] data_dir` = where saved RFID proofs live (blank = `proofing_data` next to `app.py`;
     a folder on the share is better). See `PROOFING.md`.
3. Double-click `run.bat`. It installs Flask, openpyxl, waitress, Pillow and reportlab the first time.
4. In Settings, set the **Backup folder** on the share. It also turns on the serial journal.
   Backups run every 6 hours by default (adjustable). Each zip is a full snapshot and is only
   kept as a good backup if its serial history checks out. To restore, see `RESTORE.md`.

## What it does

- **Jobs**: open jobs, search, status, roll progress, a **Priority** card you can
  reorder, and a recent activity feed.
- **Needs attention** (card on the Jobs page; the header shows the count from any page): open jobs
  that are overdue, marked complete with rolls not done, due today or tomorrow and not started, on
  hold, have a roll still in progress 12 hours after it was started, have every roll done but aren't
  marked complete, are queued with no BarTender template in `Print\`, or have a missing job folder,
  plus serial conflicts. Problems (red) come before things to look at (amber). Job folders are checked by
  a background thread every 5 minutes, never while a page waits, so a slow Z: drive can't hold the
  page up; **Check now** starts a check.
- **On hold needs a reason**: putting a job on hold asks why (Material, Printer, Encoding, Data / file,
  Customer, Quality, Scheduling, Other) and what it is waiting for. The reason shows on the job, the
  roll tracking cards and the rewind page, can be changed while the job is held, and is cleared when
  the job resumes (the activity log keeps it, with how long the job was held). Jobs that were on hold
  before this show "no reason recorded" with an **Add a reason** button.
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
- **Roll tracking**: pick any roll; Complete, Left in progress (with a note), Reopen.
  The app asks who did it at that moment. Nobody signs in.
- **Tag checks and verified rolls**: a tag scanned on a roll card (desktop) or on the rewind page with a job open is kept.
  When a roll's FIRST and LAST tags have both passed, the done roll shows **Verified** (on the card, the roll list, the
  rewind page). A tag from another roll, another job or another product is a failed check and shows in Needs attention
  until the roll passes again. Scanner setup problems (not hex, a TID) aren't kept. Reopening or resetting a roll clears
  its checks. Marking a job complete warns about rolls not done and, once a job's tags are being scanned, rolls not verified.
- **Next roll on the same printer**: completing a roll that ran on a printer offers "Start roll N on <printer>" (and, with
  BarTender on, "Start and send to BarTender"), using the name already given, as long as that printer isn't running something else.
- **Pace**: from rolls with a real start and finish time (1 minute to 8 hours; bulk "done through" marks are left out):
  minutes per roll, labels per hour, and when the job should be done at that pace with the printers running it now.
  Shown on the roll tracking header and in the job dialog once two rolls have been timed.
- **Reprints and spoiled labels**: **Log reprint…** on a roll card records how many labels (or which serials), why
  (Bad encode, Print defect, Jam / torn, Setup / test, Wrong data, Other) and who. Totals show on the job; entries can be
  removed (the activity log keeps that).
- **Shift summary** (`/shift`, Tools, or under the job list on Roll tracking): rolls done by job, printer and person, labels per
  hour, rolls left in progress with their last note, jobs on hold, important notes, status changes, reprints, failed tag
  checks and BarTender failures, for this shift or the previous one, ready to print. Shift start times are in Settings >
  Printers and BarTender (default 06:00, 14:00, 22:00).
- **New job like this** (job dialog, More): opens New job with the customer, item, size, inlay, part, UPC, labels per roll,
  rows per database file, overage, data sheet and the same Master Template filled in. Never the ticket, PO, quantity, due date
  or serials.
- **Tools**: roll tracker sheet, checklist, encoding database, serial lookup, shift summary.
- **BarTender**: the template is copied into `Print\`. Each job shows **Copy path** buttons
  for the template and the encoding database so they can be pasted into BarTender's file box.
- **Send to BarTender** (BarTender Enterprise; Settings > Printers and BarTender): see *Sending rolls to BarTender* below.

## Sending rolls to BarTender

With it turned on, each roll card has **Send to BarTender**. Job Manager writes a BarTender XML script (BTXML) into the
**trigger folder** (default `…\3 Encoding and Printing Files\BarTender Triggers`): the job's template from its `Print\`
folder, `UseDatabase`, the roll's **record range** (the same numbers the card shows), and the Windows printer for the
roll's printer (from the printer names list in Settings; a printer not listed prints on the printer saved in the
template). The file is written under a temporary name and renamed to `<request id>.btxml`, so BarTender never reads half a file.

- A roll whose records cross into a second database file is sent as one request per file. BTXML can't name an Excel
  database, so BarTender prints from the database the template is pointed at (as it does when an encoder opens it):
  for split jobs, the card names the file each part needs. The database path is also written in each request as a comment.
- States: *sent* → *BarTender has it* (the file left the trigger folder) → *printed* or *didn't print* (from the reply in
  the **acknowledgement folder**, matched by the request id in the file name or contents; `Severity="Error"` in a BTXML
  response, or a line saying error/failed in a text reply, is a failure with BarTender's message). With no
  acknowledgement folder set, *taken by BarTender* is as far as it goes.
- A roll can't be sent again while a request is waiting; a roll already printed asks before sending again. A request not
  picked up yet can be cancelled. Not picked up after 3 minutes, or failed, shows in Needs attention.
- Folders are watched by a background thread every 5 seconds; sending waits at most 10 seconds for the share.

**One-time setup on the BarTender PC (Integration Builder, BarTender Enterprise):**
1. New Integration, trigger **File**: folder = the trigger folder, file name filter `*.btxml`, after processing **delete** the file.
2. Action **Run BarTender XML Script** (BTXML), script = the trigger file's contents (*Event Data*). Turn on *Save response*
   (or add a **Write File** action) to the acknowledgement folder with a file name that contains the trigger file name,
   for example `%FileName%.response.xml`.
3. Deploy it with the Administration Console so it runs as a service, and give its account read/write on both folders and
   read on the job folders.
- **Templates**: Master Templates index, picked automatically by UPC.
- **Proofing** (`/proofing`, sidebar **Proofing**, or **Start proof** in a job's drawer): the RFID Proofing
  Utility (Lean Core) inside Job Manager. A proof started from a job is filled in from it, including the first
  serial and EPC exactly as Job Manager encodes them; its PDF and customer package go into the job's `Proof`
  folder; the Jobs list shows each job's proof state. BarTender previews render on the host PC. Saved proofs
  from the standalone app come over with `import_proofing.bat`. Details: `PROOFING.md`.

## When something goes wrong

- **Only one copy runs.** Starting Job Manager while it is already running (a second `run.bat`) stops
  straight away and opens the running one. Two copies would both run backups, auto-archive and serial sync.
- **Error log.** Server errors are written to `logs/jobmanager.log` next to `app.py`, with the page, the
  person and the full details. The message in the browser ends with a reference such as `(ref 3f9a1c2e)`;
  search the log for it. Requests slower than 3 seconds are logged too. The log keeps 5 files of 2 MB.
- **A slow or dropped Z: drive doesn't freeze the app.** The serial registry, journal, job folders, Master
  Templates and backup folder are checked by a background thread every 15 seconds; the pages read the last
  result. A check stuck for over 30 seconds shows that share as not answering.
- **Settings are checked before saving.** A number out of range (for example 1.5 hours between backups) is
  refused with a message and nothing is saved. Settings changes go in the activity log with who made them.
- **After an update**, open pages notice within 15 seconds. A page with nothing open or typed reloads
  itself (the rewind tablets too); otherwise a bar asks to reload, and changes from the old page are refused
  until it does, so nothing is saved by out-of-date page code.

## Checks and tools

- **`JM_TOOLS.bat`** (on the host PC) opens a menu: host PC check, serial and database check, back up now, check
  the newest backup, database upkeep, restore a backup, and the check reports folder.
- **Host PC check** (`JM_TOOLS.bat` option 1, or Tools > **Check this PC**): checks the Python packages, config.ini,
  the database (integrity, and that it isn't inside OneDrive, which can corrupt a live database), read and write
  on the job folders, serial registry, backup folder and proofing folder, Master Templates, BarTender, the serial
  and database check, and that Job Manager answers. `JM_TOOLS.bat` then walks through a few hands-on steps (create a
  job, open its database, scan a tag on a tablet, open from another PC, a BarTender preview, a restart). Each
  check has a 20-second limit. The result goes to `check_reports\HOST_CHECK_<date>.txt` and `.json`: PASS, FAIL,
  or INCOMPLETE when a required step was skipped (not checked is never a pass). Nothing is changed.
- **Serial and database check** (runs a couple of minutes after start and every 6 hours; Settings > Data, *Check
  now*): every standard job's rolls start at its first serial, run one after another with no gaps or repeats, add
  up to quantity + overage and carry the right EPCs; each job's range is recorded and covers its rolls; no two
  recorded ranges for a UPC overlap (nor two jobs' own ranges, nor two CDO sheet ranges); and the database passes
  SQLite's quick check, with no rolls or notes left without a job. Read-only. Problems show in Needs attention.
  Conflicts already reviewed on the Tools page aren't reported again.
- **Encoding files are written safely.** The encoding database is written under a hidden temporary name,
  re-opened and checked row by row (serial, UPC, EPC), and only then given its real name; if any file fails,
  none appear. An existing file is never replaced.
- **Database upkeep** (SQLite optimize and a write-ahead-log checkpoint) runs after every good backup, and from
  Tools or `JM_TOOLS.bat`.
- **Start-up** opens the browser once the app actually answers, not after a fixed delay.

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
| `floor_ops.py` | BarTender print requests, tag checks, reprints, pace, shift summary |
| `integrity.py`, `host_check.py`, `jm_tools.py`, `JM_TOOLS.bat` | Serial and database check, host PC check, tools menu |
| `config.py`, `config.ini.example` | Paths and port |
| `lists.py` | Starter customer and label size lists |
| `templates/index.html` | The desktop interface |
| `templates/rewind.html` | The rewind operators' page (tablets, phones) |
| `run.bat`, `run.sh` | Start the app |
| `import_old.py`, `.bat` | Bring data over from the old app |
| `seed_serials.py`, `.bat` | Find serials used before the app and reserve them |
| `backup.py` | Snapshot zips and their serial check (proof records included) |
| `restore_backup.py`, `.bat` | Emergency restore (see RESTORE.md) |
| `proofing/` | RFID proofing at `/proofing`: the proofing app's core, its server as a blueprint, its page |
| `import_proofing.py`, `.bat` | Bring saved proofs over from the standalone RFID Proofing Utility |
| `tests/` | Automated tests (`pip install pytest`, then `python -m pytest -q`) |
