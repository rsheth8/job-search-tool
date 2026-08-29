"""Merge all data from one user id into another (consolidate split accounts).

Common case: trained matcher + profile + swipe labels under ``local`` should
fold into an Apple ``usr_…`` after Sign in with Apple.

Usage:
    python -m scripts.migrate_user <src> <dst>            # do the merge
    python -m scripts.migrate_user <src> <dst> --dry-run  # preview only

On Fly:
    flyctl ssh console -a job-search-tool \\
        -C "python -m scripts.migrate_user local usr_abc123"
"""
from __future__ import annotations

import sys

from app.usermerge import merge_user


def main(argv: list[str]) -> int:
    args = [a for a in argv if a != "--dry-run"]
    dry = "--dry-run" in argv
    if len(args) != 2:
        print(__doc__)
        return 2
    src, dst = args
    if src == dst:
        print("src and dst are the same — nothing to do.")
        return 0

    moved = merge_user(src, dst, dry_run=dry)
    verb = "would move" if dry else "moved"
    if not moved:
        print(f"Nothing to merge from '{src}' into '{dst}'.")
        return 0
    print(f"{'[dry run] ' if dry else ''}{verb} from '{src}' -> '{dst}':")
    for table, n in sorted(moved.items()):
        print(f"  {table:24} {n}")
    print(f"Total rows: {sum(moved.values())}")
    if dry:
        print("\nRe-run without --dry-run to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
