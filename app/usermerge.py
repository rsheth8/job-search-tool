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

from .db import connect

# Tables that are global (not per-user) even if unrelated columns exist.
_SKIP = {"sqlite_sequence"}


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
