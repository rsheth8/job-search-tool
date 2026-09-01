#!/usr/bin/env python3
"""Probe the curated Workday boards and report which actually return jobs.

``data/workday_boards.json`` holds careers URLs, and a Workday careers URL is
two guessable-looking parts — a tenant and a site path — that cannot be
inferred from a company name. ``https://tesla.wd5.myworkdayjobs.com/Tesla``
reads exactly like a real one and 404s.

That matters more here than it looks. The rotation only tries
``JOB_WORKDAY_BOARDS_PER_TICK`` boards per pass, so a file that is half wrong
doesn't half-work: it burns half of every tick on requests that were never
going to return anything, and because the adapter fails open, the only symptom
is that nothing shows up.

This is the Workday twin of ``validate_ats_boards.py``:

    python -m scripts.validate_workday_boards            # report
    python -m scripts.validate_workday_boards --write    # drop the dead ones

Never guesses. It only checks what the file already claims.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.jobsources import workday  # noqa: E402
from app.jobsources.base import USER_AGENT  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "data" / "workday_boards.json"
PAYLOAD = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}


def probe(url: str, timeout: float = 20.0) -> tuple[int | None, str]:
    """(job count, detail). None means the board did not answer usefully."""
    board = workday.parse_board(url)
    if board is None:
        return None, "not a Workday careers URL"
    try:
        resp = httpx.post(
            board.jobs_api, json=PAYLOAD, timeout=timeout, follow_redirects=True,
            headers={"Accept": "application/json",
                     "Content-Type": "application/json",
                     "User-Agent": USER_AGENT},
        )
    except httpx.HTTPError as exc:
        return None, type(exc).__name__
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}"
    try:
        postings = (resp.json() or {}).get("jobPostings") or []
    except ValueError:
        return None, "not JSON"
    return len(postings), "ok" if postings else "no postings"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", type=Path, default=DEFAULT_PATH)
    ap.add_argument("--write", action="store_true",
                    help="rewrite the file with only the boards that answered")
    args = ap.parse_args(argv)

    data = json.loads(args.path.read_text(encoding="utf-8"))
    boards = data.get("boards") or []
    live, dead = [], []
    for board in boards:
        count, detail = probe(board.get("url") or "")
        name = board.get("name") or board.get("url") or "?"
        if count:
            live.append(board)
            print(f"[ ok ] {name:20} {count} jobs")
        else:
            dead.append((name, detail))
            print(f"[FAIL] {name:20} {detail}")

    print(f"\n{len(live)}/{len(boards)} live")
    if dead:
        print("dead: " + ", ".join(f"{n} ({d})" for n, d in dead))

    if args.write and dead:
        data["boards"] = live
        args.path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"\nrewrote {args.path} with {len(live)} boards")
    return 0 if not dead else 1


if __name__ == "__main__":
    raise SystemExit(main())
