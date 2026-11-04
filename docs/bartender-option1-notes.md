# BarTender connection: Option 1 notes (2026-11-04)

Status: brainstorming, nothing built. Design not yet approved.

## Goal
When a job is created, the copied template (`Print\JOB-#### - <name>.btw`) is already connected to that
job's own encoding database (`Data\*.xlsx`), so operators never paste paths and never encode from the
wrong job's data. A BarTender failure must never block serial allocation.

## Today
`copy_template()` in `app.py` copies the Master Template into `Print\`. The database is written to `Data\`
by `generators.py`. Nothing links them: operators use the **Copy path** buttons and paste paths into
BarTender by hand. The copied `.btw` still points at whatever the master was built against.

## Options
1. Wire the copied template at job creation (recommended first step). A helper on the host PC opens the
   copy through BarTender automation, sets the database file name, saves, reads it back, and the job shows
   Connected or the reason it failed.
2. Option 1 plus the app drives printing per roll (record range). Larger, changes operator workflow, needs
   printer testing and rules for failed prints. Later phase.
3. Fixed shared path that the app overwrites per job. Rejected: only one job at a time, risk of encoding
   from the wrong data.

## What the research found (verify on the host PC; docs have gaps)
- Setting a text-file database's file name through automation is documented:
  https://help.seagullscientific.com/2019/en/Subsystems/ActiveX/Content/inputdatafilesetup.htm
- `Database.FileName` is documented as working for all file-based database types (so possibly Excel), but
  older forum posts say a connection cannot be swapped through automation. UNVERIFIED for Excel:
  https://help.seagullscientific.com/2022/en/subsystems/activex/Content/FileName_Property_(Database).htm
  https://support.seagullsoftware.com/hc/de/community/posts/216556847-How-To-Change-Database-Connection-String
- Command line `/D=<file>` points at a text-file database; nothing found for Excel:
  https://help.seagullscientific.com/2019/en/Content/CmdLine_Database.htm
- Licensing: .NET SDK needs Automation or higher on each PC that runs it:
  https://support.seagullsoftware.com/hc/de/community/posts/216556927-Sdk-And-Bartender-Versions
- Use UNC paths, not mapped drives. The data file must be saved and closed before BarTender connects.
- Named database connections can be shared across templates; changing one can change them all:
  https://support.seagullscientific.com/hc/en-us/community/posts/11988069451671-Databse-workbook-file-path-changes-for-all-files-when-updating
- Print-time alternatives:
  - REST API (BarTender 2022 R1+, Automation/Enterprise) print action with `DatabaseOverrides`, so the
    template is never edited. Example:
    https://support.seagullsoftware.com/hc/en-us/articles/19027918324503-Example-for-BarTender-REST-API-Print-Action-Using-TransformTextToRecordSetAction
    Overview: https://support.seagullsoftware.com/hc/en-us/articles/4413434027799-BarTender-REST-API-Overview
  - Named data source plus data-entry form (2019+):
    https://support.seagullsoftware.com/hc/en-us/articles/6569478231703-How-to-Change-the-Database-Dynamically-at-Print-Time
  - Integration Builder / Commander trigger files (Enterprise).
  - Librarian / Print Portal / Print Station for central templates and revision control (Enterprise).

## Next steps
1. On the host PC: Help > About in BarTender. Note the version and edition (need 2022 R1 or newer for REST).
2. Check whether the templates use an Excel or text-file connection, and whether it is a *named* connection.
3. Ask Claude for the throwaway test script. It tries (a) rewiring a copy of one real template through
   automation and reading the path back, and (b) a REST `DatabaseOverrides` print of a few records. It reports
   version, edition and connection type. Run it on a COPY of a real template, not a master.
4. Send the output back; the design is chosen from the results.
5. Still undecided: one connected copy per DB file (`_DB1`, `_DB2`) or one template re-pointed per roll.
