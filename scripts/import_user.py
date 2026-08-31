"""Import a brain file made by scripts.export_user into THIS database, under a
chosen user id. Safe against a populated production DB: never overwrites existing
rows and never collides with the id sequence.

    # on Fly (brain.db already uploaded to the volume):
    fly ssh console -a job-search-tool \\
        -C "python -m scripts.import_user /data/brain.db U07XXXXX"
"""
from __future__ import annotations

import sys

from app.usermerge import import_user


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    in_path, dst_user = argv
    t = import_user(in_path, dst_user)
    if not t.counts:
        print("Nothing imported (already present, or empty file).")
        return 0
    print(f"Imported {in_path} -> '{dst_user}':")
    for table, n in sorted(t.counts.items()):
        print(f"  {table:24} {n}")
    print(f"Total rows added: {t.rows}")
    for table, why in sorted(t.skipped_tables.items()):
        print(f"  skipped {table}: {why}")
    return 0 if t.complete else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
