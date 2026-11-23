# Floor page: scan is the front door — short design

Date: 2026-11-13 · Status: awaiting approval · Builds on `2026-11-12-floor-handheld-page-design.md` (merged, PR #6)

Size: a small change. One file (`templates/floor.html`), its Node tests, and the demo and docs notes. No backend, no new endpoints, no migration.

## Intent and why

Encoders walk the floor with Zebra RFID handhelds. They should pull the trigger on any tag and instantly see, large and
clear, what that tag belongs to, change that roll's progress from the same screen, then scan the next tag the same way,
without navigating, pressing Enter or hunting for buttons on a small screen.

## What they said

- Scanning is the front door: the scanned roll is what the encoder works from.
- Scan again to check a new tag, quickly and easily.
- The screen is a Zebra handheld; readability and speed matter more than showing everything.

## Goals

- Scan on any screen and the roll appears in place, with no job list, Back or Enter in between.
- The roll card fits a roughly 360x640 screen without scrolling, with the main action fully visible.
- A scan replaces the card, so checking a new tag and acting on it use the same screen.
- Auto-submit, so a Zebra that does not send Enter still works.

## Non-goals

- No change to roll rules, endpoints, serial logic, the encoder list or Settings.
- No scan history list, no automatic roll changes from a scan (a scan only identifies).
- Multi-tag batch reads stay out of scope.

## Anti-goals (failure even if it technically works)

- A button that acts on a different roll than the tag just scanned.
- Operators able to press Complete on a card left over from before a wrong or unknown scan.
- Text or buttons too small to read or hit on the handheld.

## Approaches considered

- **A. Scan screen is home (picked).** The home screen is the scan screen and shows the card in place; the job list is behind a small
  "Browse jobs" button. Simplest for the encoder; browsing becomes second-class, which matches the brief.
- **B. Keep today's structure and tune it.** Smallest change, but a scan still navigates between list, job and roll, so Back is needed.
- **C. Pin the scanned roll above everything.** Flexible, but a 360px-wide screen has little room left.

## Design (decided in conversation)

- Home (`#/`) is the scan screen. Empty state: a large "Scan a tag" with a Browse jobs button. A good scan puts the roll
  card in place on that screen, with no navigation. Scanning another tag replaces the card.
- Browse jobs shows the open-jobs list, then a job's rolls, then tapping a roll shows the same card on the home screen. Back returns toward home.
- Auto-submit on a complete read (see below), plus Enter still works.
- Card layout for about 360x640: one-line ticket and customer, a large roll number, a coloured state, label number with
  FIRST or LAST, printer and who, then one large primary button (Start, or Complete when running, Resume when paused, Reopen
  when done), the other actions smaller beneath, then serials. Text is at least 16px and touch targets at least 56px, except serials.
- Wrong-job, wrong-product, unknown, TID and refused-burst results show a full-width coloured message and **clear the card**,
  so no button can act on a roll that is not the tag in hand.
- After an action the card stays, updated, with a short green confirmation line ("Completed roll 4 as Sam").
- Kept as is: encoder names, the one-tag-at-a-time burst rule, dialogs that act on the roll they name, offline state, the
  Scan test (gear), error messages from the server.

## My calls (not from you; check these)

- **Auto-submit rule:** when the scan field holds exactly 24 hex characters and nothing more arrives for `AUTO_MS = 150` ms, it counts as a scan, same as Enter.
  Anything else (wrong length, non-hex) still needs Enter and goes through the existing rejection messages. A repeat from Enter right after is ignored as the same tag.
  150 ms is a guess; it needs a real Zebra.
- **Cooldown becomes per tag.** After a scan is used, that same tag is ignored for `COOLDOWN_MS` (1500 ms), so a held trigger does not
  re-trigger. A *different* tag is accepted straight away, so scanning the next roll is instant. Cost if wrong: a held trigger drifting across
  tags could swap the card, but the card always shows which roll is on screen and nothing changes without a tap.
- **A rejected scan clears the card** (see anti-goals), at the cost of the encoder having to scan the right tag again.
- Colour for state and the exact sizes follow the existing palette; the builder picks the values.

## Constraints

- Phone-sized, one hand, touch plus hardware trigger. Check 360x640 and 320x533.
- No third-party libraries; one self-contained page like `/rewind`.

## Testing

- Node tests for the auto-submit rule and the per-tag cooldown, with realistic key timing.
- Browser checks at 360x640 and 320x533: the main action is visible without scrolling, a scan replaces the card, a wrong tag clears it,
  the earlier floor-page checks still pass (dialogs, offline, refusal, Scan test).
- On a real Zebra: scan a tag with Enter off in DataWedge and confirm it still submits; tune `AUTO_MS`.

## Left to the builder

Exact type sizes, colours, spacing, icon, and wording of the empty state.
