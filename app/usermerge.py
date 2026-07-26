"""Merge all data from one user id into another.

Your data can end up split across two ids — e.g. ``local`` (the web pages + swipe
trainer default) and your Slack user id (what chat keys on). This consolidates
them: every user-scoped table is repointed from ``src`` to ``dst`` so a single id
owns the whole search.

Safe by construction:
  * Auto-discovers every table with a ``user_id`` column (no hardcoded list to
    drift out of date). Child rows (e.g. application_events keyed by
    application_id) follow their parent automatically.
  * Uses ``UPDATE OR IGNORE`` so a row that would collide with one the destination
    already has (a single-row-per-user table like the profile, or a duplicate
    posting) is left under ``src`` rather than overwriting ``dst`` or erroring.
  * Read-then-report: returns a per-table moved-row count so you can see exactly
    what happened. Pass ``dry_run=True`` to preview without writing.
"""
from __future__ import annotations

import sqlite3

from .db import SCHEMA, connect

# Tables that are global (not per-user) even if unrelated columns exist.
_SKIP = {"sqlite_sequence"}

# The "search brain" — what's worth carrying from a local dev DB to production:
# your profile + identity, swipe labels, surfaced postings, and the trained model.
# These are all directly user-scoped (no child-id remapping needed), so they move
# cleanly across databases. CRM history (applications/events/reminders) is left
# out on purpose — it's machine-local and not what personalization depends on.
BRAIN_TABLES = (
    "job_search_profile",   # profile + applicant identity (applicant_json)
    "training_labels",      # the swipe labels
    "job_postings",         # surfaced/labeled postings (applied/dismissed feed too)
    "reranker_models",      # the trained model itself
)

# `posting_summaries` belongs to the brain too, but can't live in BRAIN_TABLES: it is
# keyed by cache_key ("{source}:{external_id}") and has no user_id column, so the
# `WHERE user_id = ?` copy the tables above use doesn't apply to it. It was left out
# entirely for that reason, and that silently cost real accuracy.
#
# It holds the LLM judgements (fit_score / tech_overlap / stretch) that the re-ranker
# consumes. Leave it behind and every one of those features falls back to its neutral
# default on the destination, identically for every row — so the model can't learn
# anything from them, and the three weights collapse to the same near-zero value.
# Observed on a real restore: the re-ranker dropped to AUC 0.730, exactly its measured
# without-llm_fit baseline, silently losing the ~0.06 that those features were worth.
# Nothing warned; label count and the saved model both restored fine.
#
# So it's carried explicitly, scoped to the cache keys the exported user's own rows
# refer to (postings and labels both, since a label can outlive its posting row).
_SUMMARY_KEYS_SQL = """
    SELECT source || ':' || external_id FROM main.job_postings    WHERE user_id = ?
    UNION
    SELECT source || ':' || external_id FROM main.training_labels WHERE user_id = ?
"""


def user_tables(conn: sqlite3.Connection) -> list[str]:
    """Every table that has a ``user_id`` column."""
    out = []
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ):
        if name in _SKIP:
            continue
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({name})")}
        if "user_id" in cols:
            out.append(name)
    return out


def merge_user(src: str, dst: str, *, dry_run: bool = False,
               conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """Repoint every user-scoped row from ``src`` to ``dst``. Returns
    ``{table: rows_moved}``. No-op (empty dict) when src == dst."""
    if src == dst:
        return {}
    own = conn is None
    if own:
        ctx = connect()
        conn = ctx.__enter__()
    try:
        moved: dict[str, int] = {}
        for table in user_tables(conn):
            if dry_run:
                n = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (src,)
                ).fetchone()[0]
            else:
                cur = conn.execute(
                    f"UPDATE OR IGNORE {table} SET user_id = ? WHERE user_id = ?",
                    (dst, src),
                )
                n = cur.rowcount
            if n:
                moved[table] = n
        if dry_run and own:
            conn.rollback()
        return moved
    finally:
        if own:
            ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Cross-database export / import (move a trained brain from local -> prod)
# ---------------------------------------------------------------------------

def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _init_db_file(path: str) -> None:
    """Create a schema-complete SQLite file (SCHEMA + migrations) at ``path``."""
    from .db import _migrate_schema

    c = sqlite3.connect(path)
    try:
        c.executescript(SCHEMA)
        _migrate_schema(c)
        c.commit()
    finally:
        c.close()


def export_user(user_id: str, out_path: str, *, tables=BRAIN_TABLES,
                conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """Copy ``user_id``'s rows from the brain tables into a standalone, schema-
    complete SQLite file at ``out_path`` (ready to ship to another machine).
    Returns ``{table: rows}``."""
    _init_db_file(out_path)
    own = conn is None
    if own:
        ctx = connect()
        conn = ctx.__enter__()
    try:
        conn.execute("ATTACH DATABASE ? AS exp", (out_path,))
        counts: dict[str, int] = {}
        try:
            for t in tables:
                cols = ", ".join(_columns(conn, t))
                cur = conn.execute(
                    f"INSERT INTO exp.{t} ({cols}) SELECT {cols} FROM main.{t} "
                    f"WHERE user_id = ?",
                    (user_id,),
                )
                if cur.rowcount:
                    counts[t] = cur.rowcount
            # The LLM-judgement cache, matched by cache_key rather than user_id.
            cur = conn.execute(
                f"INSERT INTO exp.posting_summaries (cache_key, summary_json, created_at) "
                f"SELECT cache_key, summary_json, created_at FROM main.posting_summaries "
                f"WHERE cache_key IN ({_SUMMARY_KEYS_SQL})",
                (user_id, user_id),
            )
            if cur.rowcount:
                counts["posting_summaries"] = cur.rowcount
        finally:
            conn.commit()  # release the write txn before detaching
            conn.execute("DETACH DATABASE exp")
        return counts
    finally:
        if own:
            ctx.__exit__(None, None, None)


def import_user(in_path: str, dst_user_id: str, *, tables=BRAIN_TABLES,
                conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """Insert the brain rows from a file made by ``export_user`` into the current
    database under ``dst_user_id``. ``INSERT OR IGNORE`` + dropping autoincrement
    ``id`` columns means it never clobbers existing rows or collides with the
    destination's id sequence — safe to run against a populated production DB.
    Returns ``{table: rows_added}``."""
    own = conn is None
    if own:
        ctx = connect()
        conn = ctx.__enter__()
    try:
        conn.execute("ATTACH DATABASE ? AS imp", (in_path,))
        added: dict[str, int] = {}
        try:
            for t in tables:
                # Drop a surrogate autoincrement 'id' so the destination assigns
                # its own (avoids PK collisions); repoint user_id to dst.
                cols = [c for c in _columns(conn, t) if c != "id"]
                select = ", ".join("?" if c == "user_id" else c for c in cols)
                before = conn.execute(
                    f"SELECT COUNT(*) FROM main.{t} WHERE user_id = ?", (dst_user_id,)
                ).fetchone()[0]
                conn.execute(
                    f"INSERT OR IGNORE INTO main.{t} ({', '.join(cols)}) "
                    f"SELECT {select} FROM imp.{t}",
                    (dst_user_id,),
                )
                after = conn.execute(
                    f"SELECT COUNT(*) FROM main.{t} WHERE user_id = ?", (dst_user_id,)
                ).fetchone()[0]
                if after - before:
                    added[t] = after - before
            # Summaries are a global cache keyed by cache_key, so there's no user to
            # repoint — take everything the file carries. INSERT OR IGNORE on the
            # cache_key primary key means an entry the destination already has wins,
            # and an older export can't stomp a fresher local summary.
            before = conn.execute(
                "SELECT COUNT(*) FROM main.posting_summaries").fetchone()[0]
            conn.execute(
                "INSERT OR IGNORE INTO main.posting_summaries "
                "(cache_key, summary_json, created_at) "
                "SELECT cache_key, summary_json, created_at FROM imp.posting_summaries"
            )
            after = conn.execute(
                "SELECT COUNT(*) FROM main.posting_summaries").fetchone()[0]
            if after - before:
                added["posting_summaries"] = after - before
        finally:
            conn.commit()  # release the write txn before detaching
            conn.execute("DETACH DATABASE imp")
        return added
    finally:
        if own:
            ctx.__exit__(None, None, None)
