# Scan or type a job ticket to open the job — short design

Date: 2026-11-16 · Status: awaiting approval

## Why

Rewinders and encoders on the floor should be able to find a job either way: by a tag on the roll, or by the job's ticket
number. The ticket is printed as a barcode on the traveler. On the Zebra, encoders scan that barcode; rewinders have no
barcode reader and type the ticket instead.

## Decisions (from the conversation)

- The barcode holds the job ticket number (for example 40133).
- Zebra (floor page): scan the traveler's barcode. Rewind page: type the ticket and press Enter.
- Approach A: the existing tag lookup (`POST /api/rewind/lookup`, shared by both pages) also answers tickets.

## Approaches considered

- **A. One lookup for everything (picked).** Least code, one place decides what a scan means, clearest messages.
- **B. A separate ticket endpoint.** Cleaner separation, more wiring in both pages, two places to keep in step.
- **C. Match on the tablet only** against the loaded open-jobs list. Simplest, but the floor page has no list until browsing,
  and archived or completed jobs could only say "not found".

## Design

- **Lookup.** A value that is a valid 24-hex EPC behaves exactly as today. Anything else is first matched against job tickets
  (trimmed, case ignored, cancelled jobs excluded, as the New job form already does in `_same_ticket`):
  - one open or completed job → result `job` with its id, ticket, job number, customer, title and status;
  - only an archived job → result `job_archived`: "Ticket 40133 is archived. Restore it in Job Manager first.";
  - more than one non-archived job (should not happen; tickets are unique) → the newest;
  - no job → today's tag messages, prefixed for short values: "No job with ticket 4013. If that was a tag, …".
- **Floor page (Zebra).** A `job` result clears the roll card and opens that job's roll list (`#/browse/<id>`), headed
  "Scan a tag or tap a roll". It goes through with Enter, or after about a second without Enter (the existing leftover-text
  path). `job_archived` and "no job" show the red message and clear the card, like any refused scan.
- **Rewind page.** In the job list's search box, Enter with a value that is not a tag asks the lookup; a `job` result opens
  that job; otherwise the list stays filtered and the message shows under the box. Typing without Enter still only filters.
  The scan box on a job or roll also accepts a ticket and opens that job.
- **Zebra setup note (README).** DataWedge's Barcode Input must be on in the Job Manager profile for traveler barcodes
  (it was suggested off earlier). Keystroke Output with Enter appended, as for tags.

## Non-goals

Printing barcodes, matching Job Manager's own `JOB-0012` numbers, camera scanning on the tablets.

## Testing

pytest for the lookup (ticket of an open, completed and archived job; case and spaces; no match keeps tag messages; tags
unchanged); browser checks on both pages (ticket with and without Enter, unknown ticket, archived ticket, tags still work);
then screenshots.
