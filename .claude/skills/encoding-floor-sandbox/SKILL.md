---
name: encoding-floor-sandbox
description: Test and review Job Manager's encoding workflow (roll tracking, job drawer, rewind page, serial and EPC checks) in a throwaway sandbox with demo jobs, never the live database. Use when changing or reviewing UI or behaviour that encoding operators touch, when asked to "see the layout", "try it with demo data", "test the roll tracker", or before handing a change to the shop floor.
---

# Encoding floor sandbox

## Context: why this exists

Job Manager runs on one host PC and the encoding team uses it in a browser. It tracks RFID label
jobs: a job has a UPC, a quantity and labels per roll, and gets a **serial range** that is split
into **rolls**. Operators encode tags roll by roll, scan a tag (EPC, 24 hex chars) to check it
belongs to the roll, and mark rolls Complete or Left in progress. Nobody signs in; the app asks
"who did this" at the moment of the action.

What matters to the people using it:
- Operators work fast, often on tablets or a shared desktop, with a scanner that types into the
  focused field. The next action must be obvious and reachable without scrolling.
- Serial numbers must never be reused. Anything that touches serials, the registry, the journal
  or backups is high risk.
- The live app holds real customer jobs, the shared Z: drive, and the real serial registry.

So: **test in the sandbox, never against the live app.** Starting the real `app.py` can create
folders on the share, start backups and write to the real registry.

## The sandbox

`mock_demo/` in the project root builds a copy of the app under `mock_demo/site/` with its own
database, serial registry, job folders and backups, listening on `127.0.0.1:5051`.

```bash
python mock_demo/run_mock.py            # start (seeds demo data on first run)
python mock_demo/run_mock.py --reset    # delete demo data and re-seed
```

- Code and `templates/` are re-copied on every start. After editing `templates/index.html` or any
  `.py`, **restart the sandbox** to see the change.
- Seeding goes through the app's own API (`mock_demo/seed_demo.py`), so jobs, rolls and serials
  are realistic. Add scenarios there, not by hand-editing the database.
- Launch config is `.claude/launch.json` (`mock-demo`). The preview tool may report port 5052;
  the app is on **5051**, so navigate to `http://127.0.0.1:5051` yourself.
- Never point the sandbox at `local.db`, `serial_registry/`, or any Z: path. Check the paths the
  server prints at startup if in doubt.

## Demo jobs and what each is for

| Ticket | State | Use it to check |
|---|---|---|
| 40121 Acme | In progress, roll 4 left with a note, important note | Left-in-progress display, notes, "who is on which roll" |
| 40133 Northwind | 12 rolls, 8 done, 2 running | Long list scrolling, roll filter, auto-advance after Complete |
| 40152 Fabrikam | All rolls done | "Mark job complete" prompt, done styling |
| 40140 Contoso | On hold | On-hold tab, resume flow |
| 40161 Tailspin, 40170 Initech | Queued (Initech is special, no UPC) | Queued tab, Start job, no-serial roll panel |

## Test pass for the roll tracking page

Do these in the browser pane, with desktop width then tablet (`resize_window` to about 820 wide;
reset to `desktop` afterwards):

1. **Floor list**: All / Active / Queued / On hold tabs (All is the default) and counts; search by ticket, customer and
   UPC; card progress bar, due date, "who is on which roll", important-note flag.
2. **Open a job**: header chips, BarTender copy-path line, important and ticket notes shown above
   the rolls; Start, Hold, Resume and Complete buttons match the job's status.
3. **Roll list**: filter All / Not started / In progress / Done with correct counts; the selected
   roll stays visible under any filter; list scrolls inside its box; selecting a roll updates the
   panel and keeps the typed note draft.
4. **Roll card**: Complete / Left in progress / Reopen sit in the card header and stay visible while
   scrolling; Prev/Next (‹ ›) work and disable at the ends; Reopen and Reset ask for confirmation;
   Copy buttons work on serials and EPCs; notes tabs (Roll notes / Job notes / History) switch, keep a
   typed draft across roll switches, and a note added on the Roll tab is tagged with that roll.
4b. **Printers and records**: the printer strip on Roll tracking shows each printer's loaded job and the
   roll's record range; picking a printer on a not-started roll starts it; picking a busy printer asks
   first; "Take off" clears it. Roll rows lead with the record range (what an encoder types in BarTender,
   e.g. 1,001-1,500), with numbering restarting per database file when the job's qty per DB file is set.
5. **Complete a roll**: asks who, then moves to the next unfinished roll. After the last roll the
   "All N rolls done" banner appears.
6. **Tag check**: paste an EPC from the roll's own range (good), one from another roll (should
   offer "Go to roll N"), a wrong UPC prefix, and a malformed string. Results must read without
   relying on colour alone.
7. **Special job (40170)**: panel shows "no UPC, no serials" and hides the tag check.
8. **Tablet width**: roll panel appears above the roll list; no horizontal scroll; tap targets are
   comfortable.
9. **Keyboard and a11y**: Tab through the page, focus ring visible, filter buttons report pressed
   state, tag-check results are announced (`aria-live`).
10. **Console**: `read_console_messages` with `onlyErrors` is clean after the pass.

Also glance at the Jobs drawer (Rolls section) and `/rewind` if the change touched shared roll
logic or helpers.

## Floor page (Zebra handhelds, `/floor`)

Design for 360x640 (also check 320x533). The demo seeds encoder names Sam, Priya and Dana. `/floor` shares the lookup and
roll endpoints with `/rewind`; there is no scanner in the sandbox, so type a tag into the scan field (Playwright:
`fill`, with no Enter, to prove auto-submit). Take EPCs from `GET /api/rewind/jobs/<id>` (`epc_start`). Restart the
sandbox after editing `floor.html`: Flask keeps the template in memory. Run tests with
`-o tmp_path_retention_policy=none --basetemp=<scratch>`: every test copies the repo, and pytest otherwise keeps
three sessions of copies and can fill the disk.

1. **Names**: an empty Encoders list shows "No encoder names are set up" and no input; with names, tap one: it shows in the
   header and survives a reload; tapping it re-opens the picker.
2. **Scan is home**: the empty state says "Scan a tag" with a Browse jobs button. A valid EPC typed with no Enter shows its
   roll in place within about a second; a different tag replaces the card straight away; the same tag is ignored for 1.5 s.
3. **Rejections**: an unknown tag, or two different tags inside 400 ms, clear the card and show a red message with no buttons.
4. **Roll actions**: one big primary button per state (Start, Complete, Resume, Reopen); Start asks for a printer (busy ones are
   marked), Pause takes an optional note, Complete and Reopen need a second tap; each action shows a green
   "Completed roll 4 as Sam" style line that clears itself; a roll finished elsewhere shows the server's "already done" message.
5. **Dialogs**: a scan typed while a dialog is open does nothing; the dialog closes if the card changes.
6. **Fit**: the primary button is fully on screen in every state at 360x640 and 320x533; card text is at least 16px (serials at
   least 13px), buttons at least 56px high, the roll number at least 48px.
7. **Offline**: set the browser offline: the banner shows, every action is disabled, and they return when it reconnects.
8. **Browse**: Browse jobs, a job, a roll: its card shows on the home screen; Back returns.
9. **Scan test** (gear icon): a good tag stays on the test screen and shows "Found"; the raw text and length are shown.

## Templates page (mock BarTender library)

`mock_demo/seed_templates.py` fills `mock_demo/site/Master Templates/` with about 40 fake `.btw` files (random
bytes, not real BarTender documents) in customer folders, with preview images and a few jobs that already copied
one. `run_mock.py` runs it whenever that folder is missing; `--reset` rebuilds it. Check: folder tree and counts,
search, sort, List / Previews toggle, select a row (arrow keys too), then Rename, Move to folder (new folder name),
Duplicate, Replace file (old one appears under Previous versions), Remove (file lands in `_Removed`), Add template,
New folder. Bad names (`..`, slashes) must be refused. At tablet width the folder tree becomes a dropdown.

## Archive page (long-term list)

The Archive is paged, sorted and filtered by the server (`/api/archive`). To judge it with realistic volume,
insert a few hundred archived rows straight into `mock_demo/site/demo.db` (jobs with `archived_at` set), then
check: sort each column (aria-sort changes), page numbers with ellipsis, rows per page, Go to page, customer /
status / archived-date filters, search, Clear filters, and the empty state.

## Gotchas found while testing

- `templates/index.html` is rendered by Jinja, so JavaScript template literals must never contain
  `${{` (for example `${{a: 1}[x]}`): it is read as a Jinja `{{` and the page returns a 500. Put the
  object in a `const` first.
- Seeding only runs when `mock_demo/site/demo.db` is missing. To re-seed after editing
  `seed_demo.py`, stop the sandbox first (`preview_stop`), then delete `mock_demo/site`. If Windows
  says the folder is in use, a sandbox `app.py` is still listening on port **5051**; stop only that
  process. The real app listens on 5050, so never stop that one.
- Flex children with `overflow:hidden` (like `.seg`) shrink to nothing in a height-limited column;
  give them `flex:none`.

## Useful tricks

- Skip the "who" prompt in scripted checks: set `localStorage.setItem('jm_operator','Sam')` and
  override `window.askName = async () => 'Sam'`.
- Drive state from the console, e.g. `showView('rolls'); await loadFloor(); await openWork(id); openRoll(9)`.
- `file://` snapshots of `templates/index.html` have no `localStorage` or API, so they only prove
  the script parses. Use the sandbox for real checks.
- Stop with `preview_stop` when done, and reset any viewport emulation.

## Reporting

State what you ran in the sandbox and what you did not (for example phone width, `/rewind`, a
real scanner). Say clearly that nothing was tested against live data. Demo data changes made
during testing (rolls you completed) are disposable: `--reset` restores them.
