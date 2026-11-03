# Restoring Job Manager from a backup

For emergencies only: the database is damaged or lost, the host PC was replaced, or the
serial registry folder was deleted. Day to day you never need this.

## What a backup holds

Every zip in the backup folder is a full snapshot of what the app owns: the app database,
every serial registry file, a copy of the serial journal, template previews, config.ini and
a manifest. Each one was checked when it was made: a zip is only named `backup_...zip` if
its serial registry covers every serial that was in use. Zips that failed the check are
named `failed_backup_...zip` and should not be used.

Job folders and Master Templates are on the share, which IT backs up. A restore does not touch them.

## Steps

1. Close the Job Manager window on the host PC (the app must not be running).
2. In the backup folder, pick the newest `backup_...zip`.
3. Drag it onto `restore_backup.bat` in the app folder. A preview shows what is in the
   backup, what is there now, and how many serial lines will be added.
4. Press Y to restore. If the current database has jobs created or changed since the
   backup, you are asked:
   - **K, keep (recommended):** those jobs, their rolls and notes come along.
   - **D, drop:** the backup's jobs are used exactly. Dropped jobs are listed in
     `restore_log.txt`; their serials stay used and their folders on the share stay.
   If nothing changed since the backup, there is no question.
5. Start Job Manager with `run.bat`, then open Settings and press **Back up now**.

## What it will never do

- Serial numbers never go backwards. The registry is merged (no line is removed) and every
  serial range in the current database is kept. After the restore it checks every UPC and
  stops with a warning if any next serial is lower than before.
- Job numbers never go backwards.
- It never overwrites without a copy: everything that was there is saved first in
  `restore_safety_<date>` next to the app, with `restore_log.txt`.
- It keeps your current config.ini. If the backup's differs, it is saved beside it as
  `config.ini.from-backup` (on a new PC with no config.ini, the backup's is used).

## Backups from another version of the app

- **Older backup:** fine. The next start upgrades its database, after saving a copy beside it
  named `local.db.before-schema-v<N>-<date>`.
- **Backup made by a newer version than the one installed:** the app refuses to start on it
  ("last used by a newer Job Manager"). Install the newer app, or restore an older backup.

## Undoing a restore

Close the app, copy `local.db`, the `serial_registry` folder and `config.ini` back from the
`restore_safety_<date>` folder, and start the app. Copying the registry back is safe: the
app also keeps every range in the database and the journal, and always uses the highest.

## Command line

    python restore_backup.py "<zip>"                     preview only
    python restore_backup.py "<zip>" --apply             restore, asks if needed
    python restore_backup.py "<zip>" --apply --keep-new-work
    python restore_backup.py "<zip>" --apply --drop-new-work
    python restore_backup.py "<zip>" --apply --allow-unverified   only if no good backup exists
