# Plan: "All" defaults + one dense card per roll (Roll tracking)

## Context
Two things from the last review of the Roll tracking page (`templates/index.html`, single-file UI; sandbox in `mock_demo/`):

1. Status filters should open on **All**: the Jobs "Status" dropdown (now defaults to Open) and the Roll tracking Active / Queued / On hold / All tabs (default Active).
2. The roll area wastes space. From the screenshot:
   - **Roll list rows** are ~42px tall and 330px wide but only say "Not started" + a serial range. About 60% of each row is empty.
   - **Roll detail panel** shows qty / size / inlay / UPC in a 4-column grid with short values, then serial + EPCs, then actions, tag check and notes. It ends ~680px down and leaves a blank band to the right and below. The primary action (Complete / Reopen) is below the data block.
   - **Job notes + History** live in a third column far from the roll, with a second composer that duplicates the per-roll one ("Whole job / Roll n" selector already exists).

Goal: fewer, denser, better-related elements. One **roll card** that holds everything about the selected roll; a compact list that only needs to answer "which roll, what state, who"; notes and history reachable from the same card.

## Dependency graph
```
T1 Jobs default All ─┐
T2 Roll tabs default All ┴─(independent, ship first)

T3 Compact roll rows ──────────────(independent of card work)

T4 Card header + actions ──> T5 Tag-range block + tag check ──> T6 Notes/History tabs in card ──> T7 Responsive + cleanup
```
All code is in `renderWork`, `rollPanelHtml`, `rollBatchesHtml`/`rollRowHtml`, `renderFloor`, `loadFloor`, `renderJobs` and their CSS in `templates/index.html`. No back-end change is needed for any task.

## Proposed design (recommended)

**Roll list (left, ~280px)**: one line per roll, ~34px:
`[#] [▶/✓/○] 10801–11300 · Priya`
Number bold, state glyph + colour rail kept, serial last-5 range in mono, operator right-aligned and truncated. Block headers ("Rolls 1–10 · 2 of 10") stay. Result: 10 rolls fit in ~340px instead of ~440px, and the list column can drop from 330 to ~280px.

**Roll card (centre)**: one bordered card, top to bottom:
1. **Header row**: `‹ Prev  Roll 1 of 18  [Done]  Next ›` on the left; **primary action on the right of the same row** (Complete roll n / Reopen) with "Left in progress…" and "Reset" beside it. Actions never scroll out of view and the "Done 10/28 23:02 by Priya" line becomes a muted subtitle under the title.
2. **Two-column body** (stacks under ~560px container width):
   - Left "Roll": qty (large), size, inlay and UPC as one inline key/value strip (no empty grid cells), then a compact **Tag range** table: Serial range, First EPC, Last EPC, each with a small copy button.
   - Right "Check a tag": scanner input, last result shown right below it (✓/✗ with "Go to roll n"), the last 3 results only.
3. **Footer tabs inside the card**: `Roll notes (n) · Job notes (n) · History`. One shared composer with the existing "Whole job / Roll n" selector pre-set to the current roll; Important checkbox kept. Replaces the separate third column and the two composers.

Wide screens (container >= 1700px): keep Job notes/History as a right rail instead of tabs, since there is room; otherwise tabs.

**Defaults**: `STATUS_FILTER = 'all'`, `FLOOR_FILTER = 'all'`, "All" listed first in both controls. Remove the auto-switches that assume Active (`loadFloor` fallback to queued, `FLOOR_FILTER = 'running'` in `rollAction` / `workStatus`, `STATUS_FILTER = 'open'` after job creation near the `openJob(r.job.id)` call) so the user's choice is not overridden. "All" on the floor list keeps today's server order (in progress, on hold, then queued).

## Tasks

### Phase 1 — Defaults (small, ship first)
**T1. Jobs status dropdown defaults to All**
- Change `STATUS_FILTER` initial value and the reset after job creation; put "All" first in `FILTERS`.
- Acceptance: fresh load shows every non-archived job and the dropdown reads "All (n)"; choosing another status still filters; Group by / density unaffected.
- Verify: sandbox, clear `localStorage`, reload; counts in the dropdown match row counts.

**T2. Roll tracking tabs default to All**
- Change `FLOOR_FILTER` initial value; reorder `FFILTERS` so All is first; delete the Active→Queued fallback in `loadFloor` and the forced `FLOOR_FILTER='running'` in `rollAction` / `workStatus`.
- Acceptance: opening the page lists all jobs with counts; starting a Pending job does not jump the tab; empty-state text still sensible for each filter.
- Verify: sandbox; start a queued job via the page and confirm the list does not change filter.

**Checkpoint A**: both defaults verified, no console errors. Safe to merge on its own.

### Phase 2 — Compact list
**T3. One-line roll rows**
- Rework `rollRowHtml` + `.ri` / `.rb-body` CSS: single-line grid (number, glyph, range, operator), 34px rows, keep colour rail, notes dot, selected outline, tooltip with full state text, `aria-label` with state + operator + range.
- Acceptance: 10 rows <= ~350px tall at default font; selection, filter chips, batch folding, auto-advance and "Go to roll n" still work; text never overflows at 280px.
- Verify: sandbox job 41026 (18 rolls) and 40133 (12 rolls); keyboard Tab/Enter on rows.

### Phase 3 — The roll card (vertical slices; each leaves the page working)
**T4. Card header with actions in the title row**
- In `rollPanelHtml`, merge Prev/Next/title/state chip and the action buttons into one header; move the done/in-progress timestamp under the title; keep `rollAction`, `showLeft`, the "left in progress" input and confirmations unchanged.
- Acceptance: Complete/Reopen visible without scrolling at 900px viewport height; all three roll states show the right buttons; confirmations and "who" prompt unchanged.
- Verify: complete, reopen, reset, left-in-progress on a demo job.

**T5. Tag range table + side-by-side tag check**
- Replace the `.rp-data` grid with a key/value strip and a Tag range table with copy buttons; put the tag check beside it (container query >= 560px) and trim results to the last 3.
- Acceptance: no empty grid cells; EPC last-5 highlight preserved; copy works (fallback to selecting text if the clipboard API is unavailable); special job (no UPC) shows a one-line "No serials" note and hides the tag check.
- Verify: paste a good EPC, a wrong-roll EPC (offers "Go to roll n"), a wrong-product EPC, malformed text; special job 40170.

**T6. Notes and History as tabs inside the card**
- Add Roll notes / Job notes / History tabs under the card; reuse `notesHtml`, `composerHtml`, `loadJobHistory`, `addNote`; one composer whose roll selector defaults to the current roll. Remove the right-hand `.w-side` column below 1700px container width and the duplicate `r-note-*` composer; keep note drafts when switching rolls (`openWork` / `openRoll` currently preserve `w-note-*` and `r-note-*`, so update those ids together).
- Acceptance: adding a roll note appears under Roll notes and Job notes; important notes still pop in the job header; tab counts update; typed drafts survive roll switches and background refresh (`openWork(..., true)` from the 8s refresh at the `WORK && !keepWork` path).
- Verify: add, edit, delete a note; switch rolls with a half-typed note; History tab loads job-only items.

**Checkpoint B**: demo walkthrough of the full roll flow (select, scan, complete, auto-advance, note) at 1455px with the side nav open and at 1920px. Review with the user before Phase 4.

### Phase 4 — Polish
**T7. Responsive + cleanup**
- Container-query rules: card single-column under 560px, tabs vs right rail at 1700px; tablet (820px) stacks card above list as today; remove now-dead CSS (`.rp-data` variants, `.w-side`, `.rb-body .ri` multi-line styles, duplicate `.btn.big` rule).
- Update `.claude/skills/encoding-floor-sandbox/SKILL.md` test pass (card header actions, tabs, defaults) and seed a job with long notes and a no-UPC job with notes to exercise the tabs.
- Acceptance: no horizontal scroll at 820 / 1100 / 1455 / 1920; no console errors; skill checklist matches the new UI.

## Risks
- Note composers/draft preservation are keyed by element ids (`w-note-*`, `r-note-*`) in `openWork`, `openRoll`, `addNote`, the refresh handler; T6 must change them in one commit.
- `renderWork` is re-run by background refresh; anything stateful (active tab, scan results in `VERIFY`) must live in module-level variables, not the DOM.
- Jinja renders this file: avoid `${{` in template literals (see sandbox skill).
- Real serials/EPC data are only exercised in the sandbox; copy buttons and EPC checks should be confirmed once on the live app by an operator.

## Verification (end to end)
1. `python mock_demo/run_mock.py` (never the live app); clear `localStorage`; reload.
2. Jobs page: Status = All (n), Group by Customer, compact.
3. Roll tracking: tabs show All; open 41026 (18 rolls) and 40133 (12 rolls); walk T3-T6 acceptance lists above.
4. Resize to 820 / 1100 / 1455 / 1920 px; check console for errors; Tab through the card.
5. Report what was and was not tested (phone width, real scanner).

## On approval
Save this as `tasks/plan.md` and the task list (T1-T7 with the acceptance bullets as checkboxes) as `tasks/todo.md` in the project root, then start with Phase 1.
