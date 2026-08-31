"""Export one user's "search brain" (profile + identity + swipe labels + postings
+ trained model) to a standalone SQLite file you can ship to another machine.

Use it to carry your locally-trained matcher to production:

    # locally (point at the DB that has your data):
    DATABASE_PATH=job_search.db python -m scripts.export_user local brain.db

Then ship brain.db to Fly and run scripts.import_user there.
"""
from __future__ import annotations

import sys

from app.usermerge import export_user


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    user_id, out_path = argv
    t = export_user(user_id, out_path)
    if not t.counts:
        print(f"No brain data found for '{user_id}'.")
        return 0
    print(f"Exported '{user_id}' -> {out_path}:")
    for table, n in sorted(t.counts.items()):
        print(f"  {table:24} {n}")
    print(f"Total rows: {t.rows}")
    for table, why in sorted(t.skipped_tables.items()):
        print(f"  skipped {table}: {why}")
    print(f"Ship {out_path} to the target machine, then run scripts.import_user.")
    return 0 if t.complete else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
