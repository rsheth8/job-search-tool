"""Accounts: looking at them, moving them, and removing them.

The bug that prompted this was found by trying to back up a production account
before deleting it, and being told:

    sqlite3.OperationalError: no such table: exp.fill_requests

`fill_requests` is a table that exists in the live database and nowhere in the
code — production had outlived it. `export_user` builds its destination from the
current schema and then assumed that destination was a superset of the source,
which is true of a fresh dev database and false of any database old enough to be
worth backing up. So the export path worked everywhere except the one place it
mattered.

Everything here is about that class of problem: two databases of different ages,
and never silently losing rows between them.
"""
from __future__ import annotations

import sqlite3

import pytest

from app import users
from app.db import SCHEMA, _migrate_schema, connect
from app.usermerge import BRAIN_TABLES, export_user, import_user


# --- fixtures ---------------------------------------------------------------

def _make_account(uid: str, *, email=None, apple=None, password=None,
                  created="2026-01-01T00:00:00Z") -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO users (id, apple_sub, email, display_name, password_hash, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uid, apple, email, None, password, created, created))


def _give_postings(uid: str, n: int) -> None:
    with connect() as c:
        for i in range(n):
            c.execute(
                "INSERT INTO job_postings (user_id, source, external_id, company, "
                "title, url, first_seen_at) VALUES (?, 'greenhouse', ?, ?, ?, ?, ?)",
                (uid, f"{uid}-{i}", "Acme", "Engineer", f"http://x/{uid}/{i}",
                 "2026-01-01T00:00:00Z"))


def _retire_a_table(name: str = "fill_requests") -> None:
    """Recreate the exact production condition: a user-scoped table that lives in
    the database but not in the current SCHEMA."""
    with connect() as c:
        c.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY, user_id TEXT, "
                  "label TEXT)")
        c.execute(f"INSERT INTO {name} (user_id, label) VALUES ('u1', 'stale')")


# --- the regression ---------------------------------------------------------

def test_export_survives_a_table_the_schema_has_retired(tmp_path):
    """The production failure, reproduced. A retired table must not take the whole
    backup down with it."""
    _make_account("u1", email="a@x.com", password="scrypt$…")
    _give_postings("u1", 3)
    _retire_a_table()

    t = export_user("u1", str(tmp_path / "out.db"), tables=None)

    assert t.counts["job_postings"] == 3          # the real data still crossed
    assert "fill_requests" in t.skipped_tables    # and the gap is named
    assert "destination schema" in t.skipped_tables["fill_requests"]
    assert not t.complete                         # so nobody trusts it blindly


def test_a_skipped_table_makes_the_transfer_incomplete(tmp_path):
    """`complete` is the flag a caller can gate on — the CLI refuses to delete
    behind an incomplete backup because of it."""
    _make_account("u1")
    _give_postings("u1", 1)
    _retire_a_table()
    assert export_user("u1", str(tmp_path / "o.db"), tables=None).complete is False


def test_an_empty_retired_table_does_not_make_a_backup_incomplete(tmp_path):
    """The reason every production delete needed --force.

    `fill_requests` and `unmatched_fields` are retired *and* empty — 0 rows
    database-wide. They still failed the completeness check, so backing up any
    account before deleting it reported "the backup is incomplete" and refused,
    and the only way through was --force. A flag you have to pass every single
    time stops being a safety check and becomes a keystroke, which is exactly
    when it will be passed over a backup that genuinely did lose something.

    A table that carried nothing cannot have lost anything. Still named, so the
    gap is never silent — just not counted against completeness.
    """
    _make_account("u1")
    _give_postings("u1", 2)
    with connect() as c:
        c.execute("CREATE TABLE fill_requests (id INTEGER PRIMARY KEY, "
                  "user_id TEXT, label TEXT)")   # retired, and no rows at all

    t = export_user("u1", str(tmp_path / "o.db"), tables=None)

    assert t.counts["job_postings"] == 2
    assert t.complete is True, "an empty retired table lost nothing"
    assert t.skipped_tables == {}
    assert "fill_requests" in t.skipped_empty, "it must still be named"
    assert "destination schema" in t.skipped_empty["fill_requests"]


def test_a_retired_table_with_this_users_rows_still_blocks(tmp_path):
    """The guard has to keep meaning something: rows that cannot cross still
    make the backup incomplete, even now that empty ones don't."""
    _make_account("u1")
    _give_postings("u1", 1)
    _retire_a_table()          # inserts a row for u1
    t = export_user("u1", str(tmp_path / "o.db"), tables=None)
    assert t.complete is False
    assert "fill_requests" in t.skipped_tables
    assert t.skipped_empty == {}


def test_another_users_rows_do_not_block_this_users_backup(tmp_path):
    """Completeness is per-transfer. A retired table full of someone else's rows
    carries nothing for *this* user, so it cannot fail *this* backup."""
    _make_account("u1")
    _make_account("u2")
    _give_postings("u1", 1)
    _retire_a_table()          # the stale row belongs to u1
    t = export_user("u2", str(tmp_path / "o.db"), tables=None)
    assert t.complete is True
    assert "fill_requests" in t.skipped_empty


def test_a_clean_export_is_complete(tmp_path):
    _make_account("u1")
    _give_postings("u1", 2)
    t = export_user("u1", str(tmp_path / "o.db"), tables=None)
    assert t.complete and t.skipped_tables == {} and t.dropped_columns == {}


def test_a_column_the_destination_lacks_is_dropped_and_named(tmp_path):
    """The same failure one level down: same table name, different columns,
    because the two databases are different ages."""
    _make_account("u1")
    _give_postings("u1", 1)
    with connect() as c:
        c.execute("ALTER TABLE job_postings ADD COLUMN experimental TEXT")

    t = export_user("u1", str(tmp_path / "o.db"), tables=("job_postings",))
    assert t.counts["job_postings"] == 1
    assert t.dropped_columns["job_postings"] == ["experimental"]
    assert not t.complete


def test_import_skips_a_table_the_file_does_not_have(tmp_path):
    """The mirror case: restoring an old backup into a newer schema."""
    _make_account("u1")
    _give_postings("u1", 2)
    path = str(tmp_path / "brain.db")
    export_user("u1", path, tables=("job_postings",))
    with sqlite3.connect(path) as f:
        f.execute("DROP TABLE training_labels")

    t = import_user(path, "u2", tables=("job_postings", "training_labels"))
    assert t.counts["job_postings"] == 2
    assert "training_labels" in t.skipped_tables
    assert "source database" in t.skipped_tables["training_labels"]


def test_export_all_is_a_superset_of_the_brain(tmp_path):
    _make_account("u1")
    _give_postings("u1", 2)
    brain = export_user("u1", str(tmp_path / "b.db"), tables=BRAIN_TABLES)
    every = export_user("u1", str(tmp_path / "a.db"), tables=None)
    assert set(brain.counts) <= set(every.counts)
    assert every.rows >= brain.rows


def test_a_round_trip_preserves_the_rows(tmp_path):
    _make_account("u1")
    _give_postings("u1", 5)
    path = str(tmp_path / "rt.db")
    export_user("u1", path, tables=None)
    t = import_user(path, "u2", tables=None)
    assert t.counts["job_postings"] == 5
    with connect() as c:
        assert c.execute("SELECT COUNT(*) FROM job_postings WHERE user_id = 'u2'"
                         ).fetchone()[0] == 5


# --- listing and inspecting -------------------------------------------------

def test_accounts_list_oldest_first_with_their_method():
    _make_account("u_apple", apple="sub1", email="a@x.com", created="2026-01-02T00:00:00Z")
    _make_account("u_email", email="b@x.com", password="scrypt$x", created="2026-01-01T00:00:00Z")
    got = [(a.id, a.method) for a in users.list_accounts()]
    assert got == [("u_email", "email"), ("u_apple", "apple")]


def test_an_account_with_both_credentials_reports_both():
    """Picking one would misreport how someone can actually get in."""
    _make_account("u1", apple="sub1", email="a@x.com", password="scrypt$x")
    assert users.get_account("u1").method == "apple+email"


def test_an_account_with_neither_is_unclaimed():
    _make_account("u1")
    assert users.get_account("u1").method == "unclaimed"


def test_footprint_counts_only_what_the_user_owns():
    _make_account("u1")
    _make_account("u2")
    _give_postings("u1", 4)
    _give_postings("u2", 7)
    assert users.footprint("u1")["job_postings"] == 4
    assert users.footprint("u2")["job_postings"] == 7


def test_footprint_leaves_sessions_out_unless_asked():
    """A session is a credential, not the user's work — counting it in "how much
    does this person own" makes the number meaningless."""
    from app import auth

    _make_account("u1")
    auth.create_session("u1")
    assert "sessions" not in users.footprint("u1")
    assert users.footprint("u1", include_credentials=True)["sessions"] == 1


def test_footprint_discovers_tables_rather_than_hardcoding_them():
    """A table added tomorrow must be counted without anyone editing this module."""
    _make_account("u1")
    with connect() as c:
        c.execute("CREATE TABLE brand_new (id INTEGER PRIMARY KEY, user_id TEXT)")
        c.execute("INSERT INTO brand_new (user_id) VALUES ('u1')")
    assert users.footprint("u1")["brand_new"] == 1


def test_get_account_returns_none_for_a_stranger():
    assert users.get_account("nobody") is None


# --- orphans ----------------------------------------------------------------

def test_orphans_are_rows_whose_owner_has_no_account():
    _make_account("u1")
    _give_postings("u1", 2)
    _give_postings("ghost", 5)   # no users row
    orphans = users.orphaned_user_ids()
    assert orphans == {"ghost": 5}


def test_a_clean_database_has_no_orphans():
    _make_account("u1")
    _give_postings("u1", 3)
    assert users.orphaned_user_ids() == {}


def test_deleting_an_account_creates_orphans_of_nothing():
    """The point of doing the delete through this module: it must not leave the
    rows behind under a dead id."""
    _make_account("u1")
    _give_postings("u1", 3)
    users.delete_account("u1")
    assert users.orphaned_user_ids() == {}


# --- deletion ---------------------------------------------------------------

def test_delete_removes_the_rows_and_the_account():
    _make_account("u1", email="a@x.com")
    _give_postings("u1", 6)
    removed = users.delete_account("u1")
    assert removed["job_postings"] == 6 and removed["users"] == 1
    assert users.get_account("u1") is None
    assert users.footprint("u1") == {}


def test_delete_takes_the_sessions_with_it():
    """A live session after the account is gone is a token that authenticates to
    nothing — or worse, to a recycled id."""
    from app import auth

    _make_account("u1")
    token = auth.create_session("u1")
    users.delete_account("u1")
    assert auth.user_id_for_token(token) is None


def test_delete_leaves_other_accounts_untouched():
    _make_account("u1")
    _make_account("u2")
    _give_postings("u1", 3)
    _give_postings("u2", 4)
    users.delete_account("u1")
    assert users.footprint("u2")["job_postings"] == 4
    assert users.get_account("u2") is not None


def test_dry_run_reports_without_deleting():
    _make_account("u1")
    _give_postings("u1", 5)
    preview = users.delete_account("u1", dry_run=True)
    assert preview["job_postings"] == 5 and preview["users"] == 1
    assert users.get_account("u1") is not None
    assert users.footprint("u1")["job_postings"] == 5


def test_delete_works_on_orphaned_data_with_no_account_row():
    """Cleaning up after a fixture or an older deletion."""
    _give_postings("ghost", 4)
    removed = users.delete_account("ghost")
    assert removed["job_postings"] == 4 and "users" not in removed
    assert users.orphaned_user_ids() == {}


def test_delete_of_a_stranger_is_a_no_op():
    _make_account("u1")
    _give_postings("u1", 2)
    assert users.delete_account("nobody") == {}
    assert users.footprint("u1")["job_postings"] == 2


def test_delete_reaches_tables_added_after_this_was_written():
    """Discovery, again — a hardcoded list would leave these rows keyed to a
    deleted id."""
    _make_account("u1")
    with connect() as c:
        c.execute("CREATE TABLE later_feature (id INTEGER PRIMARY KEY, user_id TEXT)")
        c.execute("INSERT INTO later_feature (user_id) VALUES ('u1')")
    users.delete_account("u1")
    with connect() as c:
        assert c.execute("SELECT COUNT(*) FROM later_feature").fetchone()[0] == 0


# --- the CLI ----------------------------------------------------------------
#
# The exit codes are the contract: this runs over `fly ssh console`, where a
# wrong one is the difference between a script that stops and a script that keeps
# going. And `delete` is the only irreversible thing in the repo, so what it
# refuses to do matters more than what it does.

from scripts import users as cli  # noqa: E402


def _run(argv, capsys) -> tuple[int, str]:
    code = cli.main(argv)
    return code, capsys.readouterr().out


def test_delete_without_yes_changes_nothing(capsys):
    _make_account("u1")
    _give_postings("u1", 3)
    code, out = _run(["delete", "u1", "--no-backup"], capsys)
    assert code == 1
    assert "Nothing was deleted" in out
    assert users.get_account("u1") is not None
    assert users.footprint("u1")["job_postings"] == 3


def test_delete_with_yes_removes_and_verifies(tmp_path, capsys):
    _make_account("u1")
    _give_postings("u1", 3)
    code, out = _run(["delete", "u1", "--yes", "--backup",
                      str(tmp_path / "b.db")], capsys)
    assert code == 0
    assert "Verified: no rows remain" in out
    assert users.get_account("u1") is None


def test_delete_writes_a_restorable_backup_first(tmp_path, capsys):
    _make_account("u1")
    _give_postings("u1", 4)
    path = str(tmp_path / "b.db")
    _run(["delete", "u1", "--yes", "--backup", path], capsys)

    # The account is gone, and the backup can put it back.
    assert users.footprint("u1") == {}
    t = import_user(path, "u1", tables=None)
    assert t.counts["job_postings"] == 4


def test_delete_refuses_to_run_behind_an_incomplete_backup(tmp_path, capsys):
    """The whole reason the backup reports what it skipped. Deleting behind a
    backup you cannot fully restore is the one unrecoverable move here."""
    _make_account("u1")
    _give_postings("u1", 2)
    _retire_a_table()
    code, out = _run(["delete", "u1", "--yes", "--backup",
                      str(tmp_path / "b.db")], capsys)
    assert code == 1
    assert "incomplete" in out.lower()
    assert users.get_account("u1") is not None      # nothing was deleted


def test_force_overrides_an_incomplete_backup(tmp_path, capsys):
    _make_account("u1")
    _give_postings("u1", 2)
    _retire_a_table()
    code, _ = _run(["delete", "u1", "--yes", "--force", "--backup",
                    str(tmp_path / "b.db")], capsys)
    assert code == 0
    assert users.get_account("u1") is None


def test_no_backup_skips_the_check_entirely(capsys):
    _make_account("u1")
    _give_postings("u1", 2)
    _retire_a_table()
    code, _ = _run(["delete", "u1", "--yes", "--no-backup"], capsys)
    assert code == 0


def test_delete_of_nothing_reports_and_fails(capsys):
    code, out = _run(["delete", "ghost", "--yes", "--no-backup"], capsys)
    assert code == 1
    assert "Nothing to delete" in out


def test_list_shows_each_account_with_its_row_count(capsys):
    _make_account("u1", email="a@x.com", apple="s1")
    _give_postings("u1", 3)
    code, out = _run(["list"], capsys)
    assert code == 0
    assert "u1" in out and "a@x.com" in out and "apple" in out
    assert "1 account(s)" in out


def test_list_on_an_empty_database(capsys):
    code, out = _run(["list"], capsys)
    assert code == 0 and "No accounts" in out


def test_show_points_at_orphans_when_the_account_is_gone(capsys):
    """The question after a delete is always "where did my data go?" — answer it
    rather than saying 'not found'."""
    _give_postings("ghost", 5)
    code, out = _run(["show", "ghost"], capsys)
    assert code == 1
    assert "5 row(s) are still keyed to that id" in out


def test_show_lists_what_the_account_owns(capsys):
    _make_account("u1", email="a@x.com")
    _give_postings("u1", 2)
    code, out = _run(["show", "u1"], capsys)
    assert code == 0
    assert "job_postings" in out and "a@x.com" in out


def test_orphans_finds_rows_with_no_account(capsys):
    _make_account("u1")
    _give_postings("u1", 1)
    _give_postings("ghost", 6)
    code, out = _run(["orphans"], capsys)
    assert code == 0
    assert "ghost" in out and "6" in out
    assert "u1" not in out.split("Nothing can sign in")[0].replace("ghost", "")


def test_orphans_on_a_clean_database(capsys):
    _make_account("u1")
    _give_postings("u1", 2)
    code, out = _run(["orphans"], capsys)
    assert code == 0 and "No orphaned rows" in out


def test_export_exits_nonzero_when_it_could_not_take_everything(tmp_path, capsys):
    """So a backup step in a script stops instead of continuing on a half-copy."""
    _make_account("u1")
    _give_postings("u1", 1)
    _retire_a_table()
    code, out = _run(["export", "u1", str(tmp_path / "o.db")], capsys)
    assert code == 1
    assert "fill_requests" in out


def test_export_brain_is_the_portable_subset(tmp_path, capsys):
    _make_account("u1")
    _give_postings("u1", 2)
    code, out = _run(["export", "u1", str(tmp_path / "o.db"), "--brain"], capsys)
    assert code == 0 and "brain subset" in out


def test_merge_dry_run_moves_nothing(capsys):
    _make_account("u1")
    _make_account("u2")
    _give_postings("u1", 3)
    code, out = _run(["merge", "u1", "u2", "--dry-run"], capsys)
    assert code == 0
    assert "would move" in out
    assert users.footprint("u1")["job_postings"] == 3
    assert users.footprint("u2") == {}


def test_merge_repoints_the_rows(capsys):
    _make_account("u1")
    _make_account("u2")
    _give_postings("u1", 3)
    code, _ = _run(["merge", "u1", "u2"], capsys)
    assert code == 0
    assert users.footprint("u2")["job_postings"] == 3
    assert users.footprint("u1") == {}


# --- the expensive kind of orphan -------------------------------------------

def test_an_orphan_with_a_profile_is_flagged_as_still_costing_money():
    """Discovery iterates job_search_profile, not users. An id with a profile and
    no account is served by the scheduler forever — real fetches, real LLM spend,
    for a feed that cannot be opened."""
    with connect() as c:
        c.execute("INSERT INTO job_search_profile (user_id, roles) VALUES (?, ?)",
                  ("ghost", "engineer"))
    assert users.unreachable_discovery_users() == ["ghost"]


def test_a_profile_with_an_account_is_not_flagged():
    _make_account("u1")
    with connect() as c:
        c.execute("INSERT INTO job_search_profile (user_id, roles) VALUES (?, ?)",
                  ("u1", "engineer"))
    assert users.unreachable_discovery_users() == []


def test_orphans_exits_nonzero_when_one_is_still_being_served(capsys):
    """So this is usable as a check, not just a report."""
    _give_postings("ghost", 2)
    with connect() as c:
        c.execute("INSERT INTO job_search_profile (user_id, roles) VALUES (?, ?)",
                  ("ghost", "engineer"))
    code, out = _run(["orphans"], capsys)
    assert code == 1
    assert "discovery still ticks this" in out
    assert "LLM budget" in out


def test_plain_orphans_without_a_profile_exit_zero(capsys):
    """Dead rows are untidy; dead rows being actively served are a bill."""
    _give_postings("ghost", 2)
    code, out = _run(["orphans"], capsys)
    assert code == 0
    assert "discovery still ticks" not in out


# --- foreign keys between the tables being moved ----------------------------
#
# Found by running the finished CLI against production, which is the only place
# these tables all have rows at once:
#
#     sqlite3.IntegrityError: FOREIGN KEY constraint failed
#
# The plan was in alphabetical order, so `apply_queue` was inserted before the
# `job_postings` it references. Every test above used a single table, so none of
# them could see it.

def _linked_rows(uid: str = "u1") -> None:
    """One row in each table that references another."""
    with connect() as c:
        c.execute("INSERT INTO job_postings (user_id, source, external_id, company,"
                  " title, url, first_seen_at) VALUES (?,'greenhouse','e1','Acme',"
                  "'Eng','http://x','2026-01-01')", (uid,))
        pid = c.execute("SELECT id FROM job_postings WHERE user_id = ?",
                        (uid,)).fetchone()[0]
        c.execute("INSERT INTO apply_queue (user_id, posting_id, status, created_at,"
                  " updated_at) VALUES (?,?,'staged','x','x')", (uid, pid))
        c.execute("INSERT INTO applications (user_id, company, status,"
                  " last_updated_at) VALUES (?,'Acme','Applied','x')", (uid,))
        aid = c.execute("SELECT id FROM applications WHERE user_id = ?",
                        (uid,)).fetchone()[0]
        c.execute("INSERT INTO reminders (user_id, application_id, remind_at, body,"
                  " created_at) VALUES (?,?,'x','b','x')", (uid, aid))


def test_exporting_everything_does_not_violate_a_foreign_key(tmp_path):
    """The production failure. Alphabetically `apply_queue` precedes the
    `job_postings` it points at."""
    _make_account("u1")
    _linked_rows()
    t = export_user("u1", str(tmp_path / "o.db"), tables=None)
    assert t.counts["apply_queue"] == 1 and t.counts["job_postings"] == 1
    assert t.counts["reminders"] == 1 and t.counts["applications"] == 1
    assert t.complete


def test_a_parent_is_ordered_before_its_children():
    from app.usermerge import _dependency_order

    with connect() as c:
        order = _dependency_order(c, "main",
                                  ["apply_queue", "job_postings", "reminders",
                                   "applications"])
    assert order.index("job_postings") < order.index("apply_queue")
    assert order.index("applications") < order.index("reminders")


def test_an_export_keeps_its_links_intact(tmp_path):
    """Export copies ids verbatim, so the backup is internally consistent and
    can be read back on its own."""
    _make_account("u1")
    _linked_rows()
    path = str(tmp_path / "o.db")
    export_user("u1", path, tables=None)
    with sqlite3.connect(path) as f:
        row = f.execute("SELECT q.posting_id, p.company FROM apply_queue q "
                        "JOIN job_postings p ON p.id = q.posting_id").fetchone()
    assert row is not None and row[1] == "Acme"


def test_sessions_are_never_transferred(tmp_path):
    """Credentials, not content — and their parent `users` row is not part of a
    user's data, so exporting them could only make a dangling reference."""
    from app import auth

    _make_account("u1")
    auth.create_session("u1")
    t = export_user("u1", str(tmp_path / "o.db"), tables=None)
    assert "sessions" not in t.counts
    assert t.complete   # deliberately excluded, so not a gap


def test_import_refuses_to_invent_parent_links(tmp_path):
    """Dropping 'id' is what makes import safe against a populated database, but
    it renumbers the parents. A child row's stored id would then point at
    whichever row happens to hold that number — plausible and wrong."""
    _make_account("u1")
    _linked_rows()
    path = str(tmp_path / "o.db")
    export_user("u1", path, tables=None)

    t = import_user(path, "u2", tables=None)
    assert t.counts["job_postings"] == 1      # directly user-scoped: fine
    assert "apply_queue" in t.skipped_tables
    assert "renumbered" in t.skipped_tables["apply_queue"]
    assert not t.complete                     # and the restore says so


def test_the_brain_subset_still_round_trips_cleanly(tmp_path):
    """BRAIN_TABLES was chosen to be free of this problem; that must stay true."""
    _make_account("u1")
    _linked_rows()
    path = str(tmp_path / "b.db")
    assert export_user("u1", path, tables=BRAIN_TABLES).complete
    assert import_user(path, "u2", tables=BRAIN_TABLES).complete


# --- exporting onto an existing file ----------------------------------------
#
# Hit while backing up production. The first export died on the foreign-key bug
# above, leaving a partial file; the retry then said
#
#     sqlite3.IntegrityError: UNIQUE constraint failed: applications.id
#
# because the schema is built with CREATE TABLE IF NOT EXISTS, so the second run
# appended to the first run's rows. The error at least stopped it. Without the
# primary keys it would have produced a quietly doubled backup.

def test_export_refuses_to_append_to_an_existing_file(tmp_path):
    _make_account("u1")
    _give_postings("u1", 2)
    path = str(tmp_path / "o.db")
    export_user("u1", path, tables=None)

    with pytest.raises(FileExistsError, match="already exists"):
        export_user("u1", path, tables=None)


def test_overwrite_replaces_rather_than_appends(tmp_path):
    """Not merely 'does not raise' — the result must be one copy, not two."""
    _make_account("u1")
    _give_postings("u1", 3)
    path = str(tmp_path / "o.db")
    export_user("u1", path, tables=None)
    export_user("u1", path, tables=None, overwrite=True)
    with sqlite3.connect(path) as f:
        assert f.execute("SELECT COUNT(*) FROM job_postings").fetchone()[0] == 3


def test_the_cli_surfaces_the_refusal(tmp_path, capsys):
    _make_account("u1")
    _give_postings("u1", 1)
    path = str(tmp_path / "o.db")
    _run(["export", "u1", path], capsys)
    with pytest.raises(FileExistsError):
        _run(["export", "u1", path], capsys)


def test_the_cli_can_overwrite_on_request(tmp_path, capsys):
    _make_account("u1")
    _give_postings("u1", 1)
    path = str(tmp_path / "o.db")
    _run(["export", "u1", path], capsys)
    code, _ = _run(["export", "u1", path, "--overwrite"], capsys)
    assert code == 0


def test_deletes_own_backup_is_not_blocked_by_a_stale_file(tmp_path, capsys):
    """delete writes its backup itself, so it must not be stopped by leftovers
    from an earlier attempt — that would make a retry impossible."""
    _make_account("u1")
    _give_postings("u1", 2)
    path = str(tmp_path / "b.db")
    export_user("u1", path, tables=None)          # a stale file in the way
    code, _ = _run(["delete", "u1", "--yes", "--backup", path], capsys)
    assert code == 0
    assert users.get_account("u1") is None
