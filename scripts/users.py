#!/usr/bin/env python3
"""One place to look at and manage accounts.

Everything here was previously either a one-off SQL statement typed at a
production shell or spread across three separate scripts. Both are fine right up
until the day you delete the wrong row.

    python -m scripts.users list
    python -m scripts.users show usr_abc123
    python -m scripts.users orphans
    python -m scripts.users export usr_abc123 /data/backup.db [--brain]
    python -m scripts.users import /data/backup.db usr_def456 [--brain]
    python -m scripts.users merge usr_old usr_new [--dry-run]
    python -m scripts.users delete usr_abc123 --yes

On Fly (`-C` execs directly — no shell, and it already starts in /app):

    fly ssh console -a job-search-tool -C "python -m scripts.users list"

`delete` writes a full backup first unless you pass --no-backup, and refuses to
do anything without --yes. `export` defaults to *everything* the user owns,
because the common reason to export is a backup; --brain gives you the portable
subset meant for carrying personalization between machines.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from app import users
from app.config import get_settings
from app.usermerge import BRAIN_TABLES, export_user, import_user, merge_user


def _report(t) -> int:
    """Print a Transfer. Returns an exit code: incomplete transfers are not ok."""
    for table, n in sorted(t.counts.items()):
        print(f"  {table:26} {n}")
    print(f"  {'TOTAL':26} {t.rows}")
    if t.dropped_columns:
        print("\n  Columns that could not cross (absent on the other side):")
        for table, cols in sorted(t.dropped_columns.items()):
            print(f"    {table}: {', '.join(cols)}")
    if t.skipped_tables:
        print("\n  Tables skipped:")
        for table, why in sorted(t.skipped_tables.items()):
            print(f"    {table}: {why}")
    return 0 if t.complete else 1


def cmd_list(args) -> int:
    accounts = users.list_accounts()
    if not accounts:
        print("No accounts.")
        return 0
    print(f"{'ID':24} {'METHOD':12} {'ROWS':>7}  {'CREATED':21} EMAIL")
    for a in accounts:
        rows = sum(users.footprint(a.id).values())
        print(f"{a.id:24} {a.method:12} {rows:>7}  {a.created_at[:19]:21} "
              f"{a.email or a.display_name or '—'}")
    print(f"\n{len(accounts)} account(s).")
    return 0


def cmd_show(args) -> int:
    a = users.get_account(args.user)
    if a is None:
        print(f"No account '{args.user}'.")
        orphan = users.orphaned_user_ids().get(args.user)
        if orphan:
            print(f"But {orphan} row(s) are still keyed to that id — see "
                  f"`users orphans`.")
        return 1
    print(f"id           {a.id}")
    print(f"email        {a.email or '—'}")
    print(f"display name {a.display_name or '—'}")
    print(f"sign-in      {a.method}")
    print(f"created      {a.created_at}")
    print(f"updated      {a.updated_at or '—'}")
    print("\nowns:")
    fp = users.footprint(a.id)
    if not fp:
        print("  (nothing yet)")
    for t, n in sorted(fp.items()):
        print(f"  {t:26} {n}")
    print(f"  {'TOTAL':26} {sum(fp.values())}")
    sess = users.footprint(a.id, include_credentials=True).get("sessions", 0)
    print(f"\nactive sessions: {sess}")
    return 0


def cmd_orphans(args) -> int:
    orphans = users.orphaned_user_ids()
    if not orphans:
        print("No orphaned rows — every user_id in the database has an account.")
        return 0
    ticking = set(users.unreachable_discovery_users())
    print("Rows keyed to a user id with no account row:\n")
    for uid, n in orphans.items():
        flag = "  <- discovery still ticks this" if uid in ticking else ""
        print(f"  {uid:28} {n}{flag}")
    print(f"\n{len(orphans)} orphaned id(s), {sum(orphans.values())} row(s).")
    print("Nothing can sign in as these. Remove one with: "
          "python -m scripts.users delete <id> --yes")
    if ticking:
        print(f"\n{len(ticking)} of them still has a search profile, so the "
              f"scheduler keeps fetching boards and spending LLM budget for a "
              f"feed nobody can open. Worth deleting first.")
    return 1 if ticking else 0


def cmd_export(args) -> int:
    tables = BRAIN_TABLES if args.brain else None
    t = export_user(args.user, args.out, tables=tables,
                    overwrite=args.overwrite)
    print(f"Exported '{args.user}' -> {args.out}"
          f"{' (brain subset)' if args.brain else ' (everything)'}:")
    return _report(t)


def cmd_import(args) -> int:
    tables = BRAIN_TABLES if args.brain else None
    t = import_user(args.path, args.user, tables=tables)
    print(f"Imported {args.path} -> '{args.user}':")
    return _report(t)


def cmd_merge(args) -> int:
    moved = merge_user(args.src, args.dst, dry_run=args.dry_run)
    verb = "would move" if args.dry_run else "moved"
    if not moved:
        print(f"Nothing to move from '{args.src}'.")
        return 0
    print(f"{verb} '{args.src}' -> '{args.dst}':")
    for t, n in sorted(moved.items()):
        print(f"  {t:26} {n}")
    print(f"  {'TOTAL':26} {sum(moved.values())}")
    if args.dry_run:
        print("\nRe-run without --dry-run to apply.")
    return 0


def cmd_delete(args) -> int:
    a = users.get_account(args.user)
    preview = users.delete_account(args.user, dry_run=True)
    if not preview:
        print(f"Nothing to delete for '{args.user}'.")
        return 1

    print(f"Account: {a.label if a else '(no users row — orphaned data)'}")
    for t, n in sorted(preview.items()):
        print(f"  {t:26} {n}")
    print(f"  {'TOTAL':26} {sum(preview.values())}")

    if not args.yes:
        print("\nNothing was deleted. Re-run with --yes to confirm.")
        return 1

    if args.backup:
        t = export_user(args.user, args.backup, tables=None, overwrite=True)
        print(f"\nBacked up {t.rows} row(s) -> {args.backup}")
        if not t.complete:
            print("  WARNING: the backup is incomplete —")
            for table, why in sorted(t.skipped_tables.items()):
                print(f"    {table}: {why}")
            if not args.force:
                print("\nRefusing to delete behind an incomplete backup. "
                      "Pass --force to override, or --no-backup if you don't "
                      "want one.")
                return 1

    removed = users.delete_account(args.user)
    print(f"\nDeleted {sum(removed.values())} row(s).")
    left = users.footprint(args.user, include_credentials=True)
    still = users.get_account(args.user)
    if left or still:
        print(f"INCOMPLETE — rows remain: {left}, account row: {bool(still)}")
        return 1
    print("Verified: no rows remain for that id.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="scripts.users", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="every account").set_defaults(fn=cmd_list)

    p = sub.add_parser("show", help="one account in detail")
    p.add_argument("user")
    p.set_defaults(fn=cmd_show)

    sub.add_parser("orphans", help="rows whose user id has no account"
                   ).set_defaults(fn=cmd_orphans)

    p = sub.add_parser("export", help="write one user's rows to a SQLite file")
    p.add_argument("user")
    p.add_argument("out")
    p.add_argument("--brain", action="store_true",
                   help="only the portable subset (profile, labels, postings, model)")
    p.add_argument("--overwrite", action="store_true",
                   help="replace the output file if it already exists")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("import", help="read a file made by export into an account")
    p.add_argument("path")
    p.add_argument("user")
    p.add_argument("--brain", action="store_true")
    p.set_defaults(fn=cmd_import)

    p = sub.add_parser("merge", help="repoint one id's rows onto another")
    p.add_argument("src")
    p.add_argument("dst")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_merge)

    p = sub.add_parser("delete", help="remove an account and everything it owns")
    p.add_argument("user")
    p.add_argument("--yes", action="store_true", help="actually do it")
    default_backup = (f"backup_%s_%s.db" %
                      ("user", datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")))
    p.add_argument("--backup", default=default_backup,
                   help=f"where to write the pre-delete backup (default: {default_backup})")
    p.add_argument("--no-backup", dest="backup", action="store_const", const=None,
                   help="skip the backup")
    p.add_argument("--force", action="store_true",
                   help="delete even if the backup came out incomplete")
    p.set_defaults(fn=cmd_delete)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    print(f"database: {get_settings().database_path}\n", file=sys.stderr)
    raise SystemExit(main())
