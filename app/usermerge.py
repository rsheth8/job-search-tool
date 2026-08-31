"""Merge all data from one user id into another.

Your data can end up split across two ids — e.g. ``local`` (CLI) and an Apple
``usr_…`` (what the iOS app keys on). This consolidates them: every user-scoped
table is repointed from ``src`` to ``dst`` so a single id owns the whole search.

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
from dataclasses import dataclass, field

from .db import SCHEMA, connect

# Tables that are global (not per-user) even if unrelated columns exist.
_SKIP = {"sqlite_sequence"}

# Excluded from a "everything this user owns" transfer. Sessions are credentials,
# not content, and they carry a foreign key to `users` — a row this transfer never
# takes, because a user's *account* is not part of their data. Exporting them
# could only ever produce a dangling reference, and importing them would hand a
# restored account someone else's live tokens.
_NEVER_TRANSFER = {"sessions"}

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


@dataclass(frozen=True)
class Transfer:
    """What a cross-database move actually carried — and what it could not.

    The counts alone are not enough to trust an export. Two databases of
    different ages do not have the same shape: production outlives tables the
    code has since dropped, and a table gains columns between releases. Anything
    that cannot cross has to be *named*, because the failure mode of a silent
    skip is a backup that looks complete and isn't.
    """

    counts: dict[str, int] = field(default_factory=dict)
    skipped_tables: dict[str, str] = field(default_factory=dict)
    dropped_columns: dict[str, list[str]] = field(default_factory=dict)

    @property
    def rows(self) -> int:
        return sum(self.counts.values())

    @property
    def complete(self) -> bool:
        """True when every requested table crossed with all of its columns."""
        return not self.skipped_tables and not self.dropped_columns


def _tables_in(conn: sqlite3.Connection, schema: str) -> set[str]:
    return {r[0] for r in conn.execute(
        f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table'")}


def _columns_in(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table})")]


def _dependency_order(conn: sqlite3.Connection, schema: str,
                      tables: list[str]) -> list[str]:
    """Sort so a table is inserted after everything it references.

    Foreign keys are enforced on the destination, so alphabetical order is not
    merely untidy — `apply_queue` sorts before `job_postings` and references it,
    which fails outright. Anything referencing a table outside this set (a
    parent we are not transferring) is left where it is; that edge cannot be
    satisfied by ordering and is handled by the caller.
    """
    inside = set(tables)
    deps = {
        t: {r[2] for r in conn.execute(f"PRAGMA {schema}.foreign_key_list({t})")}
        & inside - {t}
        for t in tables
    }
    ordered: list[str] = []
    seen: set[str] = set()

    def visit(t: str, path: frozenset[str]) -> None:
        if t in seen or t in path:  # a cycle cannot be ordered; leave it be
            return
        for parent in sorted(deps.get(t, ())):
            visit(parent, path | {t})
        seen.add(t)
        ordered.append(t)

    for t in tables:
        visit(t, frozenset())
    return ordered


def _remapped_parent(conn: sqlite3.Connection, schema: str, table: str) -> str | None:
    """The table this one references by a surrogate id, if any.

    Only matters on import, where the parent's ``id`` is reassigned by the
    destination. Export copies ids verbatim, so a backup keeps its links intact.
    """
    for r in conn.execute(f"PRAGMA {schema}.foreign_key_list({table})"):
        if (r[4] or "id") == "id":
            return r[2]
    return None


def _plan(conn: sqlite3.Connection, src_schema: str, dst_schema: str, tables):
    """Work out what can actually cross between two attached databases.

    Returns ``(plan, skipped, dropped)`` where plan is a list of
    ``(table, shared_columns)``. Both directions matter: a table the source has
    and the destination lacks cannot be written, and one the destination has but
    the source lacks cannot be read.

    ``tables=None`` means "every user-scoped table in the source".
    """
    src_tables, dst_tables = _tables_in(conn, src_schema), _tables_in(conn, dst_schema)
    if tables is None:
        tables = [t for t in sorted(src_tables)
                  if t not in _SKIP and t not in _NEVER_TRANSFER
                  and "user_id" in _columns_in(conn, src_schema, t)]
    tables = _dependency_order(conn, src_schema,
                               [t for t in tables if t in src_tables]) + \
        [t for t in tables if t not in src_tables]

    plan, skipped, dropped = [], {}, {}
    for t in tables:
        if t not in src_tables:
            skipped[t] = "not present in the source database"
            continue
        if t not in dst_tables:
            # The usual cause: a long-lived database still carries a table the
            # current schema no longer creates (fill_requests, unmatched_fields).
            skipped[t] = "not present in the destination schema"
            continue
        src_cols = _columns_in(conn, src_schema, t)
        dst_cols = set(_columns_in(conn, dst_schema, t))
        shared = [c for c in src_cols if c in dst_cols]
        if not shared:
            skipped[t] = "no columns in common"
            continue
        missing = [c for c in src_cols if c not in dst_cols]
        if missing:
            dropped[t] = missing
        plan.append((t, shared))
    return plan, skipped, dropped


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


def _init_db_file(path: str, *, overwrite: bool = False) -> None:
    """Create a schema-complete SQLite file (SCHEMA + migrations) at ``path``.

    Refuses an existing file. The schema is built with ``CREATE TABLE IF NOT
    EXISTS``, so writing into one that already has rows *appends* — which turns
    a re-run into either a silently doubled backup or a confusing
    ``UNIQUE constraint failed`` from the second copy of the same rows. Neither
    is something a backup command should do quietly.
    """
    import os

    if os.path.exists(path):
        if not overwrite:
            raise FileExistsError(
                f"{path} already exists. Exporting into it would append to what "
                f"is already there. Pass overwrite=True (CLI: --overwrite) or "
                f"choose another path."
            )
        os.remove(path)

    from .db import _migrate_schema

    c = sqlite3.connect(path)
    try:
        c.executescript(SCHEMA)
        _migrate_schema(c)
        c.commit()
    finally:
        c.close()


def export_user(user_id: str, out_path: str, *, tables=BRAIN_TABLES,
                overwrite: bool = False,
                conn: sqlite3.Connection | None = None) -> Transfer:
    """Copy ``user_id``'s rows into a standalone SQLite file at ``out_path``.

    ``tables=None`` exports *every* user-scoped table — use that for a backup;
    the default ``BRAIN_TABLES`` is the portable subset worth carrying between
    machines.

    The destination is built from the current schema, which is not necessarily a
    superset of the source: a database that has been alive across releases still
    carries tables the code has since dropped. Those are reported in the returned
    ``Transfer`` rather than raising, so one retired table cannot take a whole
    backup down with it — but they are never silently dropped either.
    """
    _init_db_file(out_path, overwrite=overwrite)
    own = conn is None
    if own:
        ctx = connect()
        conn = ctx.__enter__()
    try:
        conn.execute("ATTACH DATABASE ? AS exp", (out_path,))
        counts: dict[str, int] = {}
        try:
            plan, skipped, dropped = _plan(conn, "main", "exp", tables)
            for t, cols in plan:
                names = ", ".join(cols)
                cur = conn.execute(
                    f"INSERT INTO exp.{t} ({names}) SELECT {names} FROM main.{t} "
                    f"WHERE user_id = ?",
                    (user_id,),
                )
                if cur.rowcount:
                    counts[t] = cur.rowcount
        finally:
            conn.commit()  # release the write txn before detaching
            conn.execute("DETACH DATABASE exp")
        return Transfer(counts=counts, skipped_tables=skipped, dropped_columns=dropped)
    finally:
        if own:
            ctx.__exit__(None, None, None)


def import_user(in_path: str, dst_user_id: str, *, tables=BRAIN_TABLES,
                conn: sqlite3.Connection | None = None) -> Transfer:
    """Insert rows from a file made by ``export_user`` under ``dst_user_id``.

    ``INSERT OR IGNORE`` plus dropping autoincrement ``id`` columns means it
    never clobbers existing rows or collides with the destination's id sequence,
    so it is safe against a populated production database.

    ``tables=None`` imports every user-scoped table the *file* contains. As in
    ``export_user``, anything the two databases do not share is reported, not
    silently skipped — an import that quietly dropped a table would look like a
    successful restore.
    """
    own = conn is None
    if own:
        ctx = connect()
        conn = ctx.__enter__()
    try:
        conn.execute("ATTACH DATABASE ? AS imp", (in_path,))
        added: dict[str, int] = {}
        try:
            plan, skipped, dropped = _plan(conn, "imp", "main", tables)
            for t, cols in plan:
                # Dropping 'id' is what makes an import safe against a populated
                # database — but it means a child row's stored parent id no longer
                # refers to anything. `apply_queue.posting_id` would point at
                # whatever posting happens to hold that number in the destination,
                # which is worse than not importing it. Say so rather than
                # producing plausible, wrong links.
                parent = _remapped_parent(conn, "main", t)
                if parent:
                    skipped[t] = (f"references {parent}(id), which is renumbered "
                                  f"on import — the links cannot be preserved")
                    continue
                # Drop a surrogate autoincrement 'id' so the destination assigns
                # its own (avoids PK collisions); repoint user_id to dst.
                cols = [c for c in cols if c != "id"]
                if not cols:
                    skipped[t] = "no columns left after dropping the surrogate id"
                    continue
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
        finally:
            conn.commit()  # release the write txn before detaching
            conn.execute("DETACH DATABASE imp")
        return Transfer(counts=added, skipped_tables=skipped, dropped_columns=dropped)
    finally:
        if own:
            ctx.__exit__(None, None, None)
