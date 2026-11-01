# Release rules

Every user-visible change ships under a formal version `MAJOR.MINOR.PATCH` (see CHANGELOG.md).
When making a change: bump `APP_VERSION` in `app.py`, update `VERSION`, the README title, and add a
CHANGELOG.md entry (Added / Changed / Fixed) in the same commit, then tag `vX.Y.Z`.
Features = MINOR, fixes only = PATCH, breaking = MAJOR. Never leave unreleased work unversioned.
