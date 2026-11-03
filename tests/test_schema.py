"""M2: the database records its schema version, each change runs exactly once inside its own
transaction, a copy is taken before any change, and a database from a newer app is refused."""
import os
import sqlite3

import pytest


def _version(jm):
    c = sqlite3.connect(jm.config.DB_PATH)
    try:
        return int(c.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()[0])
    finally:
        c.close()


def _set_version(jm, v):
    c = sqlite3.connect(jm.config.DB_PATH)
    c.execute("INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)", (str(v),))
    c.commit(); c.close()


def _snapshots(jm):
    folder, name = os.path.split(jm.config.DB_PATH)
    return sorted(f for f in os.listdir(folder) if f.startswith(name + ".before-schema-"))


def test_new_database_is_stamped_with_the_current_version_and_needs_no_copy(jm):
    assert _version(jm) == jm.app.SCHEMA_VERSION
    assert _snapshots(jm) == []                    # nothing to protect in a brand new database


def test_starting_again_changes_nothing(jm):
    jm.job(1)
    jm.app.init_db()
    assert _version(jm) == jm.app.SCHEMA_VERSION
    assert _snapshots(jm) == []


def test_unversioned_database_is_upgraded_once_after_a_copy_is_taken(jm):
    """A database from before versioning (no schema_version) keeps its jobs, gets the version,
    and a readable copy of the old file is left next to it first."""
    jm.job(1)
    c = sqlite3.connect(jm.config.DB_PATH)
    c.execute("DELETE FROM _meta WHERE key='schema_version'"); c.commit(); c.close()

    jm.app.init_db()

    assert _version(jm) == jm.app.SCHEMA_VERSION
    snaps = _snapshots(jm)
    assert len(snaps) == 1 and "-v1-" in snaps[0]          # named after the first version it moved to
    copy = sqlite3.connect(os.path.join(os.path.dirname(jm.config.DB_PATH), snaps[0]))
    try:
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert copy.execute("SELECT job_number FROM jobs").fetchall() == [("JOB-0001",)]
        assert copy.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone() is None
    finally:
        copy.close()
    jm.app.init_db()
    assert len(_snapshots(jm)) == 1                        # second start: nothing to do, no new copy


def test_database_from_a_newer_app_is_refused_and_left_alone(jm):
    jm.job(1)
    _set_version(jm, jm.app.SCHEMA_VERSION + 1)
    before = open(jm.config.DB_PATH, "rb").read()
    with pytest.raises(SystemExit):
        jm.app.init_db()
    assert _version(jm) == jm.app.SCHEMA_VERSION + 1
    assert open(jm.config.DB_PATH, "rb").read() == before


def test_a_failing_change_is_rolled_back_and_stops_the_app(jm, monkeypatch):
    jm.job(1)
    v = jm.app.SCHEMA_VERSION

    def half_done(cur):
        cur.execute("CREATE TABLE half_done (x INTEGER)")
        cur.execute("INSERT INTO jobs (job_number) VALUES ('BROKEN')")     # NOT NULL title: fails

    monkeypatch.setattr(jm.app, "MIGRATIONS", jm.app.MIGRATIONS + [(v + 1, "test change", half_done)])
    monkeypatch.setattr(jm.app, "SCHEMA_VERSION", v + 1)
    with pytest.raises(SystemExit):
        jm.app.init_db()

    c = sqlite3.connect(jm.config.DB_PATH)
    try:
        assert c.execute("SELECT name FROM sqlite_master WHERE name='half_done'").fetchone() is None
        assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    finally:
        c.close()
    assert _version(jm) == v
    assert len(_snapshots(jm)) == 1                        # the copy taken before trying is kept


def test_each_change_is_applied_at_most_once(jm, monkeypatch):
    ran = []
    v = jm.app.SCHEMA_VERSION
    monkeypatch.setattr(jm.app, "MIGRATIONS", jm.app.MIGRATIONS + [(v + 1, "count", lambda cur: ran.append(1))])
    monkeypatch.setattr(jm.app, "SCHEMA_VERSION", v + 1)
    jm.app.init_db(); jm.app.init_db()
    assert ran == [1] and _version(jm) == v + 1


def test_migration_versions_are_consecutive_and_end_at_schema_version(jm):
    versions = [m[0] for m in jm.app.MIGRATIONS]
    assert versions == list(range(1, len(versions) + 1))
    assert versions[-1] == jm.app.SCHEMA_VERSION
