# BarTender template to job database: planning notes

Status: planning only. Nothing is built. Saved 2026-11-04.

## Goal

When a job is created, the BarTender template copied into `Print\` should use that job's own
encoding database (`Data\*.xlsx`), so operators don't paste paths into BarTender by hand and
can't encode from the wrong job's data.

## Where things are today

- `app.py` `copy_template()` copies the Master Template to `Print\JOB-#### - <name>.btw`.
- `generators.py` `generate_db_files()` writes `Data\<UPC>.xlsx` (or `_DB1`, `_DB2` ... when split by Qty per DB file). Columns: Serial #, UPC, EPC. CDO and GO2 jobs have their own sheets.
- Nothing links the two files. Jobs only show Copy path buttons, and the copied `.btw` still points at the master template's database.

## Options considered

1. Wire the template at job creation (recommended first step). A helper on the host PC opens the copied `.btw` through BarTender automation, points its database connection at the job's file, saves, and reads the path back. Operators keep their current workflow.
2. Also drive printing: the app sends each roll to BarTender with its record range. Bigger change and needs printer testing. Better as a later phase.
3. One fixed data file path that the app overwrites per job. Rejected: only one job at a time, easy to encode from the wrong data.

## What the BarTender docs say (verify on our version)

- Text file connection file name can be set through automation (Automation or Enterprise edition): <https://help.seagullscientific.com/2019/en/Subsystems/ActiveX/Content/inputdatafilesetup.htm>
- `Database.FileName` is documented as working for all file-based database types, which may include Excel. Older forum threads say connections can't be swapped through automation. UNPROVEN for Excel: <https://help.seagullscientific.com/2022/en/subsystems/activex/Content/FileName_Property_(Database).htm>
- Command line `/D=<file>` switches a text file database. Nothing found for Excel: <https://help.seagullscientific.com/2019/en/Content/CmdLine_Database.htm>
- REST API (BarTender 2022 R1 or later, Automation or Enterprise): the print action can override a database connection at print time with `DatabaseOverrides`, so the `.btw` is never rewired. Overview: <https://support.seagullsoftware.com/hc/en-us/articles/4413434027799-BarTender-REST-API-Overview>
- Example that overrides a database connection with custom text data using `TransformTextToRecordSetAction`: <https://support.seagullsoftware.com/hc/en-us/articles/19027918324503-Example-for-BarTender-REST-API-Print-Action-Using-TransformTextToRecordSetAction>
- Print-time data source choice with a named data source and a data-entry form (2019 or later): <https://support.seagullsoftware.com/hc/en-us/articles/6569478231703-How-to-Change-the-Database-Dynamically-at-Print-Time>
- Librarian, Print Portal and Print Station (Enterprise): revision history, rollback, workflow. Could replace the Master Templates folder later: <https://support.seagullsoftware.com/hc/en-us/articles/360047228074-Using-Librarian-features-in-Print-Portal>
- Integration Builder and Commander use trigger files. Forum report of commands working from the command line but failing in Integration Builder: <https://support.seagullsoftware.com/hc/fr/community/posts/360023601334-Print-Command-Script-works-from-command-line-not-from-Integration-Builder>
- Licensing: the .NET SDK needs an Automation-or-higher licence on each PC that runs it. Professional and Basic don't include it.
- Pitfalls: use UNC paths, not mapped drives like Z:. The data file must be saved and closed before BarTender connects. A named database connection changed on one template can change every template that shares it.

## Next steps

1. On the host PC, check the BarTender version and edition (Help > About). The REST API needs 2022 R1 or later. I believe we have Enterprise but have not confirmed it.
2. Claude writes a throwaway test script for the host PC. On a COPY of one real template it:
   - reports BarTender version, edition, database connection type, and whether the connection is a named one;
   - tries to repoint the template at a test `.xlsx` through automation, saves, and reads the path back;
   - tries a REST API print with `DatabaseOverrides` for a few records, with no real printer unless one is chosen.
3. Run the script on the host PC and send the output back to Claude.
4. Based on the results, choose the final design (rewire, REST override, or write a CSV copy for a text file connection). Then write the spec, then the implementation plan.
5. Still undecided: for jobs split into several DB files, one connected template copy per DB file, or one template re-pointed per roll.

## Rules for any build

- A BarTender problem must never block serial allocation. The job is still created and shows "not connected" with the reason.
- Read the connection back after setting it, and show Connected or the failure on the job.
- Test BarTender behaviour only on the host PC with copies of templates. The cloud sandbox is Linux and has no BarTender.
