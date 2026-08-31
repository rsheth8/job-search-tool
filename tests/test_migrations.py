"""Upgrading an existing database, which is the only path production takes.

Every other test in this suite starts from an empty file, so `init_db` always
sees `CREATE TABLE IF NOT EXISTS` actually create the table — with every column
the current schema declares. Production never does that. It opens a volume
written by an older build, where `CREATE TABLE IF NOT EXISTS` is a no-op and the
columns are whatever that build had.

That gap took the service down. `SCHEMA` indexed `llm_usage(user_id, day,
feature)` while `feature` was added by `_migrate_schema`, which runs *after*
`executescript(SCHEMA)`. On a fresh file the table was created with the column
and the index was fine; on the real volume the CREATE TABLE was skipped, the
index raised "no such column: feature" out of `init_db` at import, and the
machine crash-looped to its restart limit. A green suite the whole time.

So these tests build databases in older shapes and upgrade them.
"""
from __future__ import annotations

import sqlite3

import pytest

from app import config
from app.db import SCHEMA, connect, init_db

# Columns that exist only because _migrate_schema adds them. A table created by
# an older build will not have them, and nothing in SCHEMA may assume they do.
MIGRATION_ADDED: dict[str, tuple[str, ...]] = {
    "recruiters": ("apollo_person_id",),
    "company_domains": ("apollo_org_id",),
    "job_postings": ("snoozed_until", "embedding", "sort_order"),
    "job_search_profile": ("min_relevance", "applicant_json"),
    "apply_queue": ("questions_json", "sort_order"),
    "device_tokens": ("timezone",),
    "feedback": ("context",),
    "llm_usage": ("feature",),
}


def _fresh_shape() -> dict[str, list[str]]:
    """Column lists for a database built by the current code."""
    init_db()
    with connect() as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]
        return {t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
                for t in tables}


def _older_db(path: str, table: str, drop: tuple[str, ...]) -> None:
    """Write `table` as an older build would have: same CREATE, minus `drop`."""
    fresh = sqlite3.connect(":memory:")
    fresh.executescript(SCHEMA)
    cols = [r for r in fresh.execute(f"PRAGMA table_info({table})")]
    fresh.close()

    kept = [c for c in cols if c[1] not in drop]
    assert len(kept) < len(cols), f"{table} already lacks {drop}"

    # PRAGMA reports a composite key as pk=1,2,…; inlining PRIMARY KEY on each
    # is "more than one primary key". Inline only a single-column key, and
    # declare a composite one at table level.
    pk_cols = [c[1] for c in sorted((c for c in kept if c[5]), key=lambda c: c[5])]
    defs = []
    for _, name, ctype, notnull, default, pk in kept:
        piece = f"{name} {ctype}"
        if pk and len(pk_cols) == 1:
            piece += " PRIMARY KEY"
        if notnull:
            piece += " NOT NULL"
        if default is not None:
            piece += f" DEFAULT {default}"
        defs.append(piece)
    if len(pk_cols) > 1:
        defs.append(f"PRIMARY KEY ({', '.join(pk_cols)})")

    old = sqlite3.connect(path)
    old.execute(f"CREATE TABLE {table} ({', '.join(defs)})")
    old.commit()
    old.close()


# ---------------------------------------------------------------------------
# The outage, pinned
# ---------------------------------------------------------------------------

def test_upgrading_a_pre_feature_llm_usage_table_does_not_raise(tmp_path, monkeypatch):
    """The exact production failure: init_db raised OperationalError at import."""
    path = str(tmp_path / "old.db")
    monkeypatch.setenv("DATABASE_PATH", path)
    config.get_settings.cache_clear()

    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE llm_usage (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    TEXT NOT NULL,
            day        TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_llm_usage_user_day ON llm_usage(user_id, day);
        INSERT INTO llm_usage (user_id, day, created_at)
        VALUES ('usr_old', '2026-08-01', '2026-08-01T00:00:00Z');
    """)
    old.commit()
    old.close()

    init_db()  # used to raise: no such column: feature

    with connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(llm_usage)")}
        assert "feature" in cols
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='llm_usage'")}
        assert "idx_llm_usage_feature" in idx
        # The upgrade must not lose what was already billed.
        assert conn.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0] == 1
        assert conn.execute(
            "SELECT feature FROM llm_usage").fetchone()[0] == ""


# ---------------------------------------------------------------------------
# The whole class, not just the one instance
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("table,drop", sorted(MIGRATION_ADDED.items()))
def test_every_migration_added_column_upgrades_cleanly(table, drop, tmp_path, monkeypatch):
    path = str(tmp_path / f"old_{table}.db")
    monkeypatch.setenv("DATABASE_PATH", path)
    config.get_settings.cache_clear()

    _older_db(path, table, drop)
    init_db()

    with connect() as conn:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for col in drop:
        assert col in cols, f"{table}.{col} was not added by _migrate_schema"


def test_no_schema_index_references_a_migration_added_column():
    """The rule the outage broke, stated directly.

    `executescript(SCHEMA)` runs before `_migrate_schema`, so SCHEMA may only
    name columns its own CREATE TABLE guarantees. An index on a
    migration-added column works on a fresh file and fails on every existing
    one — the worst possible split, because CI only ever sees fresh files.
    """
    import re

    offenders = []
    for stmt in SCHEMA.split(";"):
        if "CREATE INDEX" not in stmt.upper() and "CREATE UNIQUE INDEX" not in stmt.upper():
            continue
        on = stmt.upper().find(" ON ")
        if on == -1:
            continue
        body = stmt[on:]
        for table, cols in MIGRATION_ADDED.items():
            for col in cols:
                if re.search(rf"\b{col}\b", body):
                    offenders.append(f"{col}: {' '.join(stmt.split())[:80]}")
    assert not offenders, (
        "SCHEMA indexes a column that only _migrate_schema adds; this crashes "
        "init_db on any existing database:\n  " + "\n  ".join(offenders)
    )


def test_an_upgraded_database_matches_a_fresh_one(tmp_path, monkeypatch):
    """Both paths must converge, or behaviour depends on install date."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "fresh.db"))
    config.get_settings.cache_clear()
    fresh = _fresh_shape()

    path = str(tmp_path / "upgraded.db")
    monkeypatch.setenv("DATABASE_PATH", path)
    config.get_settings.cache_clear()
    for table, drop in MIGRATION_ADDED.items():
        _older_db(path, table, drop)
    init_db()

    with connect() as conn:
        for table in MIGRATION_ADDED:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            assert set(fresh[table]) <= cols, (
                f"{table} upgraded is missing {set(fresh[table]) - cols}")


def test_init_db_is_idempotent_on_an_already_current_database(tmp_path, monkeypatch):
    """It runs at import, so every restart re-runs it."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "twice.db"))
    config.get_settings.cache_clear()
    init_db()
    init_db()
    init_db()
