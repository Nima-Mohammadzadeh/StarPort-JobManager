# Floor page: handheld density — short design

Date: 2026-11-14 · Status: approved (chosen from side-by-side screenshots: now / compact / tighter)

## Why

The scan-first floor page (2026-11-13) was sized for reading at arm's length: text at least 16px, buttons at least 56px,
a 52px roll number. On the Zebra handhelds that was too large: too much spacing, text and box size. The user compared three
levels in screenshots and chose the tightest.

## Decision

| | Before | Now |
|---|---|---|
| Roll number | 52px | 28px |
| Card text | 16–20px | 12.5–14.5px (small badges 11–12px) |
| Main button | 68px tall | 46px |
| Other buttons | 56px | 44px |
| Header | 52px | 40px |
| Scan field, padding, gaps | | about half |

Buttons stay at least 44px tall; below that, mis-taps start on a handheld, especially with gloves. Nothing about behaviour changes.

## Approaches considered

- **Compact** (roll number 34px, buttons 48/52px): a clear improvement, judged still too large.
- **Tighter** (picked): about half the screen for a running roll card with its buttons; about six rolls visible in Browse.
- Smaller than tighter: rejected, buttons under 44px and serials under 12px get hard to hit and read.

## Implementation

One clearly marked "Handheld density" style block at the end of `templates/floor.html`'s styles. The sandbox skill's fit
checks use the new limits.
