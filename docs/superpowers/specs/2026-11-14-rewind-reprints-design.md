# Rewind page: reprint requests and sequence check — design

Date: 2026-11-14 · Status: awaiting approval (builder check done) · Scope: standard jobs (UPC + serials) only

## Intent and why

Rewinders sit at a table and spin rolls of finished inlays through a rewinding machine, checking them by eye and by scanning.
When they find bad labels they write the EPCs down by hand. Someone on the encoding team takes that paper to BarTender, opens
Select Records, finds those exact tags and prints them. Copying EPCs by hand is slow and error-prone.

The rewinder should record bad labels in the app instead, and the app should do the legwork in the background: build a small
data file with just those labels, ready for BarTender, and show the encoding team which jobs have reprints waiting. The rewinder
should also be able to spot-check a roll's sequence by scanning, and have the app point out the data faults that actually happen
(a stretch printed twice, data that restarted, invalid or foreign tags) without false alarms while they work fast.

## What they said (the user's decisions)

- Reprints are needed sometimes right away, sometimes in a batch. No alert; the encoding team should still see that a job has reprints waiting.
- The reprint data should be a file built in the background with just those labels in it.
- Rewinders sometimes need "label X to label Y" reprinted: everything in between.
- Sequence checking is mostly spot checks, often hundreds or thousands of labels apart, and around bad areas of a roll; rolls may be scanned in either direction. Large forward jumps are normal work and must not raise warnings.
- Real faults seen in practice: a stretch of labels printed twice (data repeats a stretch); data restarting (for example a roll bigger than its data file); invalid data from encoding.
- When a repeated stretch is found, rewinders cut out the repeats and the encoding team reprints any missing labels; the two sides talk in person at that point. The app does not work out "what's missing" by itself.
- Request flow: approach A (below). Standard jobs only for now.
- Physical rolls end exactly at the app's planned roll boundaries.
- Two rewinders can work on the same job at the same time.

## Goals

- A rewinder can mark single bad labels or a from–to range on a roll, with an optional reason, by scanning.
- Marks are saved on the server as they are made; a tablet reload or dead battery loses nothing.
- Send turns the marks into one reprint request; the app builds the reprint file in the background.
- The encoding team sees, on the job, how many reprint requests are waiting, and for each: rolls, label count, who sent it and when, the file with a Copy path button, and Mark printed.
- A sequence check mode on a roll that stays silent for forward jumps of any size, stops the rewinder for tags that are certainly wrong, and shows a non-blocking warning for backward jumps, naming the likely cause.

## Non-goals

- GO2 and CDO jobs (next project: their tags are not recognised by the current lookup; see "Next").
- Alerts, sounds or pop-ups on the desktop.
- Working out which labels are missing after repeats are cut out.
- Printing from the app or changing BarTender templates.
- Storing sequence-check scans.

## Anti-goals (failure even if it technically works)

- A reprint file containing any row that is not one of the marked labels.
- Any change to a job's original data files, or any new serial taken from the registry.
- Warnings while a rewinder works fast through a roll in the normal direction.
- A batch of marks lost because the tablet reloaded before Send.

## Approaches considered

- **A. Rewinder collects, then sends (picked).** Marks are saved on the server as they are scanned; Send closes the batch and the
  app builds the file. Safe for long batches and clear about when a batch is ready; one extra tap for the rewinder.
- **B. Marks only on the tablet until Send.** Simpler to build, but a long batch is lost if the tablet reloads or dies.
- **C. No Send; marks pile into an open list per job and an encoder clicks "Make reprint file".** Least effort for the rewinder,
  but the encoder cannot tell whether the rewinder is finished and may print half a batch.

## Design

### Who

- New setting `rewinders`: one name per line, cleaned like `encoders` (trimmed, no repeats, at most 40 names of 40 characters),
  edited in a "Rewinders" box in Settings (Tablets and handhelds). The rewind page asks for a name before marking or sending,
  keeps it on the device (`localStorage` key `jm_rewind_operator`), and shows it in the header; tapping it changes it.
  Looking up tags still works without a name, as today.

### Data (one new migration step, version 2)

- `reprint_requests`: id, job_id (references jobs, cascade on delete), seq (1, 2, 3 … per job), status, created_by, created_at,
  sent_by, sent_at, file_path, file_error, printed_by, printed_at.
  - status: `draft` (collecting marks) → `building` → `ready` (file made) → `printed`; or `failed` (file could not be made; can be retried).
  - At most one `draft` per job **per rewinder** (`created_by`). Each rewinder's marks go into their own draft, and Send sends only theirs.
- `reprint_items`: id, request_id (cascade), roll_number, serial_from, serial_to, reason, marked_by, marked_at.
  A single label has serial_from = serial_to. A range is inclusive.
- Both tables are covered by backups automatically (they live in the app database). Deleting a job removes its requests.

### Rewind page (tablet), on a roll

The roll view gets three modes, shown as chips above the scan box; Look up is the default and behaves exactly as today.

- **Look up:** unchanged.
- **Mark reprint:**
  - Each scan of a tag from this job adds that label to the draft, on the roll it belongs to (it may be a different roll of the same job).
  - A **Range** button: the next two scans become "from" and "to"; both must be on the same roll; the range is inclusive and may be scanned in either order.
  - Reason chips on each mark: No read, Bad print, Damaged, Other (with a short text). Optional.
  - A tag from another job, a tag Job Manager did not issue, or a label already in the draft is refused with a message and nothing is added.
  - A label already in an earlier request that is sent but not yet printed is accepted, with a warning naming that request
    ("Label 204 is already in Reprint 2, not printed yet"), because a reprinted label can legitimately go bad again.
  - The draft list shows each mark (roll, label number(s), last 5 of the EPC, reason) with ✕ to remove it, and a total.
  - **Send reprint request** (shows "Send 7 labels from rolls 3, 7"): one tap, then a confirmation line. Disabled when empty.
- **Sequence check:** see below.
- Any mark, removal or send needs a rewinder name; the page asks for one first if none is set.
- Above the job's rolls (and on the job list card) the rewind page shows the current rewinder's "Reprint draft: N labels" and the job's "Sent: waiting / printed".

### Sequence check

Runs on the tablet only; nothing is stored. It applies to the roll that is open.

- The first two scans on the roll that are at least 2 labels apart set the direction (up or down). Until then nothing is flagged.
- **Silent:** any move in the roll's direction, of any size. Scanning the same label again.
- **Stops the rewinder (red, must be dismissed):** a tag from another roll of this job, from another job, or not issued by Job Manager.
  These are always real problems, because physical rolls end exactly at the planned boundaries. When the tag is from an
  earlier roll of the same job (a move against the direction), the message adds the likely cause: "Label 2,940 is from roll 6.
  The data may have repeated or restarted here." 
- **Warns, never blocks (yellow, in the scan list):** a move against the direction.
  - If it lands within the first 10 labels of the roll or of a data file: "Data may have restarted at label N after label M."
  - Otherwise: "Went back K labels: possible repeated stretch, labels A to B."
  - Each warning has only **Ignore**. It does **not** offer to add a reprint: the labels it names already exist on the roll, so
    reprinting them would put duplicate EPCs into the field. As the user described, the rewinder cuts out the repeats and the
    encoding team and rewinder agree in person what is missing; the rewinder then marks those labels with Range.
- A running list of the last 20 scans shows label numbers with ✓, ↩ (back) or ✗.
- **Clear check** resets direction and list.

### Reprint file (built in the background)

- On Send, the request goes to `building` and a background worker builds the file; the page does not wait.
- For each marked label, the worker finds its row in the job's own data files (every `.xlsx`/`.csv` in the job folder's `Data`,
  including `_ADD<n>` files from added data) and **verifies the row's EPC equals the label's EPC** before using it. It may use the
  serial to go straight to the likely file and row, but must check the EPC; a label whose row is not found fails the build.
- The file copies the header row and the found rows exactly, in label order, and uses the same file type as the source
  (`.xlsx` or `.csv`). It goes in `<job folder>\Reprints\Reprint <seq> - rolls <list>.<ext>`; the folder is created if needed.
- The original data files are opened read-only and never changed. No serials are taken.
- On success: status `ready`, `file_path` set, activity logged ("Reprint 2 ready: 7 labels, rolls 3, 7"). On failure: status
  `failed` with a plain reason (job folder not reachable, a label not found in the data files, file in use), activity logged.

### Encoding team (desktop)

- Jobs list and Roll tracking cards show "N reprints waiting" for jobs with requests in `building`, `ready` or `failed`.
- The job (drawer and Roll tracking panel) gets a **Reprints** section: each request with its number, rolls, label count, who sent
  it and when, status, and for `ready`: the file name, **Copy path**, **Open folder**, and **Mark printed** (asks who, like other
  actions); for `failed`: the reason and **Retry**. Printed requests stay listed, greyed, for history. Drafts in progress show as
  "Dana collecting: N labels", one line per rewinder, with no actions.
- The record numbers (file name and row number within it) of each label are listed under the request, for anyone who prefers
  Select Records in the original file.

## Constraints

- Standard jobs only: the job has a UPC and serial-numbered rolls. On other jobs the Mark reprint and Sequence check modes are hidden.
- The lookup logic stays the one shared with the floor page; no change to what it accepts.
- Tablet-sized UI, same look as the current rewind page; works in Chrome on Android.
- Data files can be large (up to about a million rows); building a file must not block the server or the tablet.
- One migration step; nothing else in the database changes.

## My calls (not from you; check these)

- Ranges must be on one roll; reasons are optional chips.
- "Restart" means landing within the first 10 labels of the roll or a data file.
- Reprint files go in a top-level `Reprints` folder of the job, not inside `Data`, so nothing that reads `Data` picks them up.
- Rewinder names are a separate list from encoder names.
- Sequence warnings never add reprints themselves (builder check: reprinting the named range would duplicate EPCs).
- A label already in a sent, unprinted request is accepted with a warning, not refused.

## Testing

- Unit tests (pytest): migration step 2 creates the tables and is idempotent across a backup/restore; marking, removing, range
  validation (same roll, in order either way), refusing other-job and duplicate labels, one draft per job per rewinder (two
  rewinders on one job send independently); Send moves to building;
  the worker builds a file whose rows equal the marked rows exactly (xlsx and csv sources, multiple data files, an `_ADD` file),
  fails cleanly when the folder is missing or a row is not found, never modifies the source files, takes no serials; Mark printed
  and Retry; job delete removes requests.
- Node tests for the sequence-check rules (pure function, like the floor page's scan collector): direction, silent forward jumps,
  backward warning wording for restart and repeat, red flags.
- Browser checks in the demo sandbox at tablet size: mark single and range, reload keeps the draft, send, file appears, desktop
  section and badges, mark printed, sequence warnings.

## Left to the builder

Exact layout and wording, the worker mechanism (thread or queue), how rows are located as long as every row is verified by EPC,
endpoint names, and whether Copy path copies the folder or the file.

## Next

- GO2 and CDO: recognise their tags by finding them in the job's data files, then reprints and sequence checks work for them too
  (and the floor page can identify them).
- Floor page: better usage of the core workflow and more readable spacing (separate item from the same request).
