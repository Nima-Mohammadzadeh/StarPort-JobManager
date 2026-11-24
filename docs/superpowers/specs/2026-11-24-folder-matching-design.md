# New jobs go into the folders that already exist — design

Date: 2026-11-24 · Status: awaiting approval · Size: a project (this doc covers step 1 only)

## Why

Every new job makes a folder `Customer\Label size\MM.DD.YY - ticket` on the shared drive, named exactly as typed. A
typo or a different way of writing the same thing starts a brand-new customer folder or size folder. Tursso now has
five folders for one label: `2.91 x 0.47`, `2.913 3x 0.47`, `2.9133 x 0.47`, `2.9 x 0.47`, `2.91 x .47`. All hold
jobs and all refer to the same material, so finding an old job for a reorder means checking all five. Many customers
have the same problem. Nothing in the app compares what is typed with what already exists.

## What we decided in the conversation

- A size is its width and height. Sizes that are the same within a small tolerance are one size and share one folder.
  The tolerance is about **0.02 inch on each side**. `2.90 x 0.59` and `2.953 x .5905` are different labels and stay apart.
- Formatting never matters: `.47` / `0.47`, `1.50` / `1.500`, `X` / `x`.
- Customers are never merged automatically. A near match only produces a "Did you mean…?" question.
- Approach A: match against the folders that are really on Z:. The share stays the single source of truth; there is no
  second "official list" for anyone to maintain.
- **Step 1 (this design):** from now on, new jobs go into the existing folder. **Nothing already on Z: is moved,
  renamed or deleted.**
- When several existing folders match, one must win, always the same one. The rule is: **pinned folder, else the folder
  with the most jobs in it, else the most recently used.** Even an exact-text match goes through this rule, otherwise the old
  variants keep collecting jobs forever.
- A person can **pin** a folder ("always use this one for this customer and size") for the case where "most jobs" is wrong,
  for example an old folder with hundreds of jobs after the customer moved to a slightly different size.

## Goals

- New jobs land in the right existing size folder without the person having to know it exists.
- The person sees where the job is going before they create it, and can change it.
- A mistyped or differently-written customer is caught before it creates a second customer folder.
- The split stops growing from the day this ships.

## Non-goals

- Merging or renaming folders that already exist, and fixing their records (step 2, its own design: a review page where
  each group is approved before anything moves, and the keeper chosen there becomes the pin).
- Changing the starter lists or the suggestion boxes, template matching (`_sizes`, `match_templates`), or what the Edit
  screen does to an existing job.
- Jobs with no label size (they still go straight under the customer, as today).

## Anti-goals (a failure even if it "works")

- Two different customers silently merged, or two different sizes merged because the tolerance crept up.
- Job creation blocked because the share could not be listed. If listing fails, behave as today.
- Any change to a job that already exists, including its `folder_path`.
- Non-size folders (`OLD`, `New Items 12.11.2025`, `JPEG Images`, `Red Gold Proof Labels`) treated as sizes.

## Approaches considered

- **A. Match against the real folders on Z: (picked).** One source of truth, nothing new to maintain, fixes the problem at
  the moment of typing. Cost: until step 2 runs, a split customer still has several folders and the rule picks among them.
- **B. An official sizes list per customer in the app.** Stricter, but it is a second list that drifts from Z:, because
  people also make folders by hand there.
- **C. Drop the size level from the folder path.** Rejected: changes how everyone browses the share.
- Which folder wins: *most jobs* (picked, with the pin as the override) is stable; *most recent* alone is fooled because
  the newest folders are usually the fresh typos; *ask every time* makes every job setup slower for a rare case.
- Where to pin: from the New job form (picked, because that is where the mistake is noticed), plus a list in Settings to
  remove pins. Step 2 will also set pins.

## Design

**A new module, `folder_match.py`** (pure functions, no Flask), so it can be tested on its own.

- `parse_size(text)` reads a size only when the **whole** text is `number x number` (also `×`, `X`, `by`, with optional `"` or
  `in`). It returns the two numbers sorted small to large, so `0.91 x 1.85` and `1.85 x 0.91` are the same size. Text that
  is anything else (`2.913 3x 0.47`, `OLD`) is not a size and is never matched or targeted.
- `same_size(a, b)` is true when both numbers are within 0.02. A second, wider band (0.02 to 0.04 on both sides, a named
  constant) means "close, ask": it produces a "Did you mean…?" instead of an automatic match.
- `size_name(w, h)` makes the name for a **new** size folder: shortest form, at most four decimals, leading zero,
  lowercase `x` (`2.9133 x .47` becomes `2.9133 x 0.47`), in the order typed.
- `customer_key(name)` for comparing customers: case, spaces, punctuation and `&`/`and` ignored.

**Resolving the folder** happens in one place, `_make_job_folder`'s path building (`job_folder_parts`), fresh at creation time
with no cache, because two people can create at once. The form preview calls the same function.

1. *Customer.* Same name apart from case: use the existing folder's spelling (Windows can't hold both anyway). Looks similar
   (one or two letters off, punctuation, one name inside the other, like `Turso` / `Tursso`, `Pesa` / `Pesa Label`): the
   server **refuses** the create with the question unless the request says which was meant. It never decides alone.
   Candidates are the folders under Z: plus customers in job history, not the static starter list (it contains known typos).
2. *Size.* List the customer's folders that parse as sizes. Matches are those within 0.02. None: make a new folder with
   `size_name`. One: use it. Several: pinned, else most job folders inside (immediate subfolders), else most recently
   modified, else name order (so the answer is always the same).
3. Typed text that is not a size: used as typed (as today) unless it equals an existing folder name ignoring case.
4. The folder name comes from the real directory entry, and still passes through `safe_folder_name`.
5. Listing errors anywhere: fall back to today's behaviour.

**The job record.** `label_size` is saved as the folder's name, so the app and the share agree and searches see one size. The
activity entry says what happened: "Typed 2.9 x .47, filed under 2.91 x 0.47."

**The pin.** Database table `size_pins(customer, folder, created_at, created_by)`, added as migration 2 (the end of
`MIGRATIONS`, never editing version 1). A pin only counts if its folder is among the current matches, so a pin on a folder that is
gone is ignored and shown as stale. Setting a pin removes any other pin for that customer whose size matches.

**What the person sees on New job** (text sketch):

```
Customer    [Turso                    ]
            ⚠ Did you mean Tursso?   [Use Tursso]   [No, Turso is a new customer]
Label size  [2.9 x .47                ]
            ✓ Goes into the existing folder 2.91 x 0.47
              (5 folders match; this one has the most jobs)   [Change…]
```

*Change…* lists the matching folders with job counts and last used. Choosing one applies to this job; a tick box
"Always use this one for Tursso 2.91 x 0.47" makes it a pin. A size that matches nothing, for a customer who already has
other sizes, shows a note, not a block: "New size folder 2.19 x 0.47. Tursso has 9 other sizes. Check it isn't a typo."
Create stays disabled while a customer question is open.

**Interfaces** (names are the builder's to adjust): `GET /api/folder-match?customer=&label_size=` returns the customer
verdict, the size verdict, the candidates and which one wins; the create request accepts `customer_choice` and
`size_folder` (checked against the current candidates); pin set and remove endpoints; a "Pinned size folders" list in Settings.

## Constraints

- The host runs Windows with the jobs on a network share; names are case-insensitive; every job folder sits at
  `root\customer\size\job` (several features rely on that depth).
- In `create_job`, the customer question must be raised before anything is taken (job number, serials); the folder is still made
  before serials, as today.
- Database changes follow `migrations.py` rules. Tests use the throwaway installs from `tests/conftest.py`.

## Testing

pytest, using the real Tursso folder list from the screenshot as the fixture:
- Parsing and matching: formats, flipped sizes, `2.9 / 2.91 / 2.913 / 2.9133` match; `2.90 x 0.59` vs `2.953 x .5905` do not;
  `2.913 3x 0.47` and `OLD` are never size folders.
- The winner rule: pin over count over recency, and an exact-text match still goes to the winner.
- Through `create_job`: three jobs typed three ways end in one folder; case-only customer; similar customer refused without
  a choice and accepted with one; no size; share unreadable falls back; pins set, replaced, stale.
- Migration 2 (existing databases, backups and the restore tool still work).
- Browser check of the New job form with screenshots.

## Left to the builder

Exact similarity thresholds for customers (start strict); how batch creation presents a customer question; the Settings
layout; wording of messages.

## My calls, for you to check

- A flipped size (`0.91 x 1.85` / `1.85 x 0.91`) counts as the same label.
- The "ask" band of 0.02 to 0.04.
- `label_size` is stored as the folder's name, not as typed.
- A customer that differs only by case uses the existing spelling without asking.
- "Jobs in a folder" means its immediate subfolders.
