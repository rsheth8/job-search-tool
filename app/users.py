"""Administering accounts: who exists, what they own, and removing them.

``auth.py`` is about *becoming* a user — verifying a token, minting a session.
Nothing there answers the operational questions: who is in this database, how
much does this account own, is this row still reachable, delete this person. Those
were being answered with one-off SQL typed at a production shell, which is how a
DELETE lands without a WHERE clause.

The rule everywhere below is that the set of user-scoped tables is **discovered,
never hardcoded**. A hardcoded list silently rots the moment someone adds a table:
a footprint that under-reports, or worse, a delete that leaves rows keyed to an id
nobody owns any more. ``usermerge.user_tables`` already had this right; this module
uses the same source of truth.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .db import connect
from .usermerge import user_tables

# Tables that reference a user but are not the user's own data. Sessions are
# credentials, not content: they belong to a delete, but not to a footprint that
# is trying to answer "how much of this person's work is in here?".
CREDENTIAL_TABLES = {"sessions"}


@dataclass(frozen=True)
class Account:
    id: str
    email: str | None
    display_name: str | None
    method: str          # "apple" | "email" | "unclaimed"
    created_at: str
    updated_at: str | None

    @property
    def label(self) -> str:
        return self.email or self.display_name or self.id


def _account(row: sqlite3.Row) -> Account:
    keys = row.keys()
    apple = "apple_sub" in keys and row["apple_sub"]
    pw = "password_hash" in keys and row["password_hash"]
    # Both can be true: an account may have been created with Apple and later
    # given a password. Report the pair rather than picking a winner.
    method = ("apple+email" if apple and pw
              else "apple" if apple
              else "email" if pw
              else "unclaimed")
    return Account(
        id=row["id"],
        email=row["email"] if "email" in keys else None,
        display_name=row["display_name"] if "display_name" in keys else None,
        method=method,
        created_at=row["created_at"],
        updated_at=row["updated_at"] if "updated_at" in keys else None,
    )


def list_accounts(conn: sqlite3.Connection | None = None) -> list[Account]:
    """Every account, oldest first."""
    def run(c):
        return [_account(r) for r in
                c.execute("SELECT * FROM users ORDER BY created_at, id")]

    if conn is not None:
        return run(conn)
    with connect() as c:
        return run(c)


def get_account(user_id: str,
                conn: sqlite3.Connection | None = None) -> Account | None:
    def run(c):
        row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _account(row) if row else None

    if conn is not None:
        return run(conn)
    with connect() as c:
        return run(c)


def footprint(user_id: str, *, include_credentials: bool = False,
              conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """Rows this user owns, per table. Only non-empty tables appear."""
    def run(c):
        out: dict[str, int] = {}
        for t in user_tables(c):
            if not include_credentials and t in CREDENTIAL_TABLES:
                continue
            n = c.execute(
                f"SELECT COUNT(*) FROM {t} WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
            if n:
                out[t] = n
        return out

    if conn is not None:
        return run(conn)
    with connect() as c:
        return run(c)


def orphaned_user_ids(conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """User ids that own rows but have no ``users`` row, and how many.

    These accumulate from deleted accounts, seeded fixtures, and the CLI's
    ``local`` id. They are invisible to every product surface — nothing can sign
    in as them — so they only ever show up as a database that keeps growing and a
    discovery loop doing work for nobody.
    """
    def run(c):
        known = {r[0] for r in c.execute("SELECT id FROM users")}
        counts: dict[str, int] = {}
        for t in user_tables(c):
            if t in CREDENTIAL_TABLES:
                continue
            for uid, n in c.execute(
                    f"SELECT user_id, COUNT(*) FROM {t} GROUP BY user_id"):
                if uid is not None and uid not in known:
                    counts[uid] = counts.get(uid, 0) + n
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    if conn is not None:
        return run(conn)
    with connect() as c:
        return run(c)


def unreachable_discovery_users(
        conn: sqlite3.Connection | None = None) -> list[str]:
    """Ids the discovery loop will keep working for that nobody can sign in as.

    Discovery iterates ``job_search_profile``, not ``users`` — reasonably, since
    a profile is what it needs. But the two can disagree. An id with a profile
    and no account is a ghost the scheduler serves every ``JOB_POLL_SECONDS``:
    real board fetches, real LLM spend, and postings piling up in a feed that has
    no door. Nothing surfaces it, because every product screen is reached through
    a session and a session needs an account.
    """
    def run(c):
        known = {r[0] for r in c.execute("SELECT id FROM users")}
        return [r[0] for r in c.execute(
            "SELECT DISTINCT user_id FROM job_search_profile ORDER BY user_id")
            if r[0] not in known]

    if conn is not None:
        return run(conn)
    with connect() as c:
        return run(c)


def delete_account(user_id: str, *, dry_run: bool = False,
                   conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """Remove a user and everything keyed to them. Returns ``{table: rows}``.

    One transaction: a partial delete would leave an account that can still sign
    in but has lost half its data, which is worse than either outcome. The
    ``users`` row goes last so a failure anywhere leaves the account intact and
    re-runnable.
    """
    def run(c):
        removed: dict[str, int] = {}
        tables = user_tables(c)
        for t in tables:
            n = c.execute(
                f"SELECT COUNT(*) FROM {t} WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
            if n:
                removed[t] = n
        has_row = c.execute(
            "SELECT COUNT(*) FROM users WHERE id = ?", (user_id,)).fetchone()[0]
        if has_row:
            removed["users"] = has_row
        if dry_run:
            return removed

        c.execute("BEGIN")
        try:
            for t in tables:
                c.execute(f"DELETE FROM {t} WHERE user_id = ?", (user_id,))
            c.execute("DELETE FROM users WHERE id = ?", (user_id,))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        return removed

    if conn is not None:
        return run(conn)
    with connect() as c:
        return run(c)
