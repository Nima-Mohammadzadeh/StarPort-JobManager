# Changelog

Versioning follows `MAJOR.MINOR.PATCH` (Semantic Versioning).
- **MAJOR**: breaking change (data format, config, or workflow operators must relearn)
- **MINOR**: new feature, backwards compatible
- **PATCH**: bug fix or hardening only

The single source of truth is `APP_VERSION` in `app.py`; `VERSION` and this file must match it.

## [3.4.0] - 2026-10-30

### Added
- New job: operator chooses automatic or specified serials. A specified start serial only has to be unused for that UPC.
- Templates: browse folders; view, rename, move, replace or remove Master Templates.
- CDO sequential data sheet; the data sheet picker is now always shown.
- GO2 random EPC data sheet for special jobs.
- Auto-archive of completed jobs; paged, sortable Archive.
- UI redesign: side nav, roll card with printers and records, grouped jobs, teal theme.

### Changed
- Job details open in a centered dialog instead of a side drawer.

### Fixed
- False "serial registry offline" when two threads checked at once.
- Hardened CDO and GO2 job creation, with safety tests.

## [3.3] - 2026-10-28
- Baseline (initial commit).
