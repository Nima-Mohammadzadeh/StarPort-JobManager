# Floor page for Zebra RFID handhelds — design

> The screen flow in this spec is superseded by `2026-11-13-floor-scan-first-design.md` (scan is the home screen).

Date: 2026-11-12 · Status: approved (scan rules added 2026-11-13)

## Purpose

Encoders walk the floor with Zebra Android RFID handhelds. Today they open Zebra's TagMatiks app to decode
a scan, which knows nothing about Job Manager. They need a page on the same device that takes a tag scan
and does their roll work: see which job and roll a tag belongs to, and start, pause, complete or reopen
that roll, without going back to a desk PC.

## Decisions (agreed)

- **Main tasks:** roll tracking, scan to verify tags, job and tag lookup. Priority queue, printer board
  and due-date views are not in the first version.
- **Delivery:** a browser page served by the host PC, like `/rewind`. No native Android app.
- **Scan input:** assumed to arrive as keystrokes (Zebra DataWedge types the EPC into the focused field,
  as Serial Magic does on `/rewind`). One trigger pull is one tag. **Not yet confirmed on a real device**;
  see Open items.
- **Who:** the encoder picks their name from a list. Nobody signs in.
- **Not covered:** reading many tags in one trigger pull (counting a whole roll or box). Possible later
  if DataWedge can batch; if it can't, that is a native-app question.

## Page and access

- New route `GET /floor` renders new `templates/floor.html`, written like `templates/rewind.html`
  (single file, own styles, phone-sized, large touch targets).
- Not in the desktop menu. Encoders open it from a QR sign and add it to the home screen.
- `rewind_link()` and the QR routes are generalised to take a path, so a `/floor` link, QR image and
  printable sign (`/floor/qr.svg`, `/floor/qr`) exist beside the rewind ones. `/rewind` behaviour is unchanged.
- Settings gets a "Floor page" panel next to the Rewind one, with the link and Print QR sign.

## Who is using it

- New setting `encoders`: one name per line, cleaned like `clean_printers` (trimmed, no repeats, at most
  40 names of up to 40 characters). Edited in Settings, saved through the existing `POST /api/settings`.
- `GET /api/meta` is not changed. The floor page reads the list from a new `GET /api/floor/config`
  (encoders and printers), so the page does not load the heavy meta payload.
- First open shows large name buttons. The choice is kept in that device's `localStorage`
  (`jm_floor_operator`), so it survives reloads.
- The name is shown large in the header. Tapping it opens the picker again. Every action shows
  "as <name>" in its confirmation.
- If the list is empty, the page says so and tells the user to ask for names to be added in Settings.
  It does not fall back to free text.
- Every write sends `operator: <name>` in the request body, which the existing endpoints already use for
  the activity log and the roll's started/paused/completed-by fields.

## Scanning

- A scan field is kept focused (it refocuses after any tap that is not on another input). Enter ends a scan.
  The on-screen keyboard is suppressed on the field (`inputmode="none"`) so a scan does not pop it open.
- A scan calls the existing read-only `POST /api/rewind/lookup` with the current job (if one is open).
  No new lookup logic. Result handling matches the page's existing messages:
  - `found`: job, ticket, customer, roll, label number, FIRST/LAST/middle position, and the roll's
    state and who has it, with the action buttons below.
  - `other_job`, `wrong_product`, `not_issued`, `ambiguous`, `invalid`, `tid`: a full-width coloured message
    with the server's text, and no action buttons.
- A scan only identifies. It never changes a roll.
- **One tag at a time.** DataWedge cannot be made to send only one tag, so the page enforces it. Input is
  collected into a burst: a burst closes `BURST_MS` (400 ms) after the last Enter. If the burst holds one
  distinct tag, it is looked up. If it holds two or more different tags, the page shows a red message
  ("More than one tag read. Move closer to the one you want and pull again.") and takes none of them.
  Repeats of the same tag inside a burst count as one.
  The page does no tag-text cleanup of its own beyond trimming whitespace; DataWedge is set to send a clean EPC.
- **Gap between scans.** After a burst is accepted, input is ignored for `COOLDOWN_MS` (1500 ms), so a held
  trigger or a quick second pull does not stack results. A tag identical to the last accepted one is also
  ignored for that period. Both values are constants at the top of `floor.html` so they can be tuned on a device.
- Typed or pasted input works in the same field, for devices without a working trigger.

## Roll actions

All actions use existing endpoints and rules; nothing about roll states changes.

| Button | Shown when | Call |
|---|---|---|
| Start | roll is not started | `POST /api/jobs/<id>/rolls/<n>/printer` with the chosen printer (starts the roll) |
| Change printer | roll is running | same endpoint |
| Pause (optional note) | roll is running | `POST /api/jobs/<id>/rolls/<n>` `status: paused` |
| Resume | roll is paused | `status: running` |
| Complete | roll is running or paused | `status: complete`, after one confirming tap |
| Reopen | roll is complete | `status: running`, after one confirming tap |

- Start shows the printer list from `/api/floor/config` as big buttons. A printer already running another roll is marked.
- Complete and Reopen ask for a second tap ("Complete roll 3 as Sam?") because a mis-tap on a moving
  floor is the likely mistake.
- After each action the roll card refreshes from `GET /api/rewind/jobs/<id>`. If the server refuses
  (for example, "This roll is done. Reopen it before changing its printer."), its message is shown as is.
- Without a scan: a searchable list of open jobs (`GET /api/rewind/jobs`) opens a job and its rolls;
  tapping a roll opens the same card. The list refreshes on its own every few seconds, as `/rewind` does.

## Setup help

- A "Scan test" screen shows exactly what the Zebra sent: the raw text, its length, and any hidden
  characters (prefix/suffix, trailing Tab or Enter), plus the verdict from the lookup. It exists to make a
  wrong DataWedge setting, such as decimal instead of hex, TID instead of EPC, or a stray prefix, easy to see.
- The page also states, in one line under the scan field, when no scan has arrived for a while
  ("No scan yet. Pull the trigger, or open Scan test"), without any popup.

## Components and boundaries

| Unit | Does | Depends on |
|---|---|---|
| `GET /floor`, `templates/floor.html` | The encoder UI | the endpoints below |
| `GET /api/floor/config` | Encoder names and printer names | `app_settings`, `clean_*` |
| `encoders` setting + `clean_encoders()` | Stores and cleans the name list | `get_settings`, `POST /api/settings` |
| `rewind_link(path)` and QR routes | Link and QR sign for `/floor` and `/rewind` | existing QR code |
| Existing: `/api/rewind/lookup`, `/api/rewind/jobs[/<id>]`, `/api/jobs/<id>/rolls/...` | Read and roll changes | unchanged |

Backend change is small on purpose: one setting, one config endpoint, one page route, one generalised link
function. No database migration (settings live in `app_settings`). No change to serial logic, the registry,
backups or restore.

## Errors and edge cases

- Host unreachable or Wi-Fi drops: the header shows an "Offline" state (the way `/rewind` does with its sync dot),
  and action buttons are disabled with the reason. Nothing is queued or retried silently.
- A write that fails shows the server's message and leaves the card as it was.
- A name no longer in the list stays selected on that device until someone changes it (past activity still
  carries it), and is shown normally.
- Archived jobs: the lookup already reports them as other jobs; they are not offered as open jobs.
- Two people acting on the same roll: the server's last write wins, as on the desktop. The card refresh
  after each action, plus the periodic refresh, shows the current state and who has it.

## Testing

- Automated (pytest, in `tests/`): `clean_encoders` (trim, de-duplicate, cap, separators); saving and
  reading the setting through `/api/settings`; `GET /api/floor/config`; `GET /floor` returns the page;
  the generalised link and QR routes keep returning what `/rewind` did.
- Sandbox: run the page against demo jobs with the `encoding-floor-sandbox` skill, never the live database.
  Check at phone and small-tablet widths: name picker, scan found / wrong-job / not-issued, start, pause,
  complete, reopen, scan test, offline state. Add `/floor` to that skill's notes.
- Real device (cannot be done in this environment). A short checklist for one Zebra:
  1. Open `/floor` from the QR sign in Chrome and add it to the home screen.
  2. In DataWedge, send RFID reads as keystrokes with no prefix or suffix, EPC as hex, and an Enter at the end.
  3. Pull the trigger on a tag in a demo job and confirm Scan test shows 24 hex characters and `found`.
  4. Complete one roll on the Zebra and confirm it shows on the desktop with the right name.

## Open items

- **Scan delivery is confirmed.** The Zebra, through DataWedge, types the EPC into a text field. The
  profile settings we rely on: RFID Input on, memory bank EPC only, hex to ASCII off, RSSI off, trigger mode
  Immediate, a short tag read duration; Keystroke Output on with Enter appended and 50 ms inter-character
  delay. The Scan test screen is how to check them on each device.
- Whether encoders also need to **add a job note** from the floor is left out of the first version.

## Out of scope

Multi-tag batch reads, priority queue and printer-board views, creating jobs, adding data, templates,
archive and delete, and any change to how serials are issued.
