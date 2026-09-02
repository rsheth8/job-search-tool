#!/usr/bin/env python3
"""Is a typical application under three minutes yet?

The Phase 1 exit test, readable. Run it after a dogfood sitting:

    python -m scripts.timings usr_abc123
    python -m scripts.timings usr_abc123 --days 1      # tonight only
    python -m scripts.timings usr_abc123 --sessions    # every lap

On Fly:

    fly ssh console -a job-search-tool -C "python -m scripts.timings usr_abc123"

The two leg medians are the point. A slow *open → fill* is the page loading or
the fill itself; a slow *fill → Filed* is everything a person still does by
hand — attaching the résumé, exotic dropdowns, reading it over. Cut the one
that is actually big, which is the thing the plan could not previously know.
"""
from __future__ import annotations

import argparse
import sys

from app import clock
from app.config import get_settings


def _mmss(seconds: int | None) -> str:
    if seconds is None:
        return "  —  "
    sign = "-" if seconds < 0 else ""
    seconds = abs(int(seconds))
    return f"{sign}{seconds // 60:d}:{seconds % 60:02d}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="scripts.timings", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("user")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--sessions", action="store_true",
                    help="list every timed application, newest first")
    args = ap.parse_args(argv)

    s = clock.summary(args.user, days=args.days)
    target = _mmss(s["target_seconds"])

    print(f"Timed applications, last {s['days']} day(s): {s['timed']}")
    if not s["timed"]:
        print("\nNothing timed yet. A session needs the form opened *and* Filed")
        print("from a build that sends marks — older builds file without them.")
        return 1

    hit = s["under_target"]
    pct = round(100 * hit / s["timed"])
    print(f"Under {target}:              {hit} of {s['timed']}  ({pct}%)")
    print()
    print(f"  median   {_mmss(s['median_seconds'])}"
          f"      <- the Phase 1 number")
    print(f"  p90      {_mmss(s['p90_seconds'])}")
    print(f"  fastest  {_mmss(s['fastest_seconds'])}")
    print(f"  slowest  {_mmss(s['slowest_seconds'])}")
    print()
    print("  where the time goes (medians):")
    print(f"    open -> fill    {_mmss(s['median_open_to_fill'])}"
          f"   page load + Autofill")
    print(f"    fill -> Filed   {_mmss(s['median_fill_to_filed'])}"
          f"   attach, dropdowns, review, Submit")
    if s["reopened_sessions"]:
        print(f"\n  {s['reopened_sessions']} of these were opened more than once "
              f"— an abandoned attempt costs time the lap does not show.")

    best = clock.best_sitting(args.user)
    if best["day"]:
        print(f"\nBusiest day: {best['day']} — {best['filed']} filed, "
              f"median {_mmss(best['median_seconds'])}")
        if best["filed"] >= 8 and (best["median_seconds"] or 0) <= s["target_seconds"]:
            print("  ^ that is the north star: eight fitted files, typical lap "
                  "under target.")

    if args.sessions:
        print(f"\n{'FILED AT':21} {'POSTING':>8} {'TOTAL':>7} {'->FILL':>7} "
              f"{'->FILED':>7} {'REOPEN':>7}")
        for row in clock.sessions(args.user, days=args.days, limit=200):
            print(f"{row['filed_at']:21} {row['posting_id']:>8} "
                  f"{_mmss(row['open_to_filed']):>7} "
                  f"{_mmss(row['open_to_fill']):>7} "
                  f"{_mmss(row['fill_to_filed']):>7} "
                  f"{row['reopens']:>7}")

    # Exit code is the gate: 0 only when a typical application is under target.
    median = s["median_seconds"]
    return 0 if median is not None and median <= s["target_seconds"] else 1


if __name__ == "__main__":
    print(f"database: {get_settings().database_path}\n", file=sys.stderr)
    raise SystemExit(main())
