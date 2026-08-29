#!/usr/bin/env python3
"""Probe ATS board slugs and report which return live job listings.

Usage:
    python scripts/validate_ats_boards.py              # validate data/ats_boards.json
    python scripts/validate_ats_boards.py --probe slug # test one slug on all ATS types
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "data" / "ats_boards.json"

PROBES = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
    "lever": "https://api.lever.co/v0/postings/{token}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=false",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{token}",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=1",
}


def _count(source: str, token: str) -> int | None:
    url = PROBES[source].format(token=token)
    try:
        resp = httpx.get(url, timeout=10.0, follow_redirects=True)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None
    if source == "greenhouse":
        jobs = data.get("jobs") if isinstance(data, dict) else None
        return len(jobs) if jobs else 0
    if source == "lever":
        return len(data) if isinstance(data, list) else 0
    if source == "ashby":
        jobs = data.get("jobs") if isinstance(data, dict) else None
        return len(jobs) if jobs else 0
    if source == "workable":
        jobs = data.get("jobs") if isinstance(data, dict) else None
        return len(jobs) if jobs else 0
    if source == "smartrecruiters":
        if not isinstance(data, dict):
            return 0
        total = data.get("totalFound")
        if isinstance(total, int):
            return total
        jobs = data.get("content")
        return len(jobs) if isinstance(jobs, list) else 0
    return 0


def probe_slug(slug: str, sources: tuple[str, ...] | None = None) -> list[tuple[str, int]]:
    hits: list[tuple[str, int]] = []
    for source in sources or (
        "greenhouse", "lever", "ashby", "workable", "smartrecruiters"
    ):
        n = _count(source, slug)
        if n is not None and n > 0:
            hits.append((source, n))
    return hits


def validate_file(path: Path) -> dict[str, list[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    keys = ("greenhouse", "lever", "ashby", "workable", "smartrecruiters")
    out: dict[str, list[str]] = {k: [] for k in keys}
    for source in out:
        for token in data.get(source) or []:
            n = _count(source, token)
            if n is not None and n > 0:
                out[source].append(token)
                print(f"  ok  {source:11} {token:30} ({n} jobs)", flush=True)
            else:
                print(f"  skip {source:11} {token:30}", flush=True)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--probe", metavar="SLUG", help="find ATS for a company slug")
    parser.add_argument("--write", action="store_true", help="rewrite JSON with valid boards only")
    args = parser.parse_args()

    if args.probe:
        hits = probe_slug(args.probe)
        if not hits:
            print(f"No public board found for '{args.probe}'")
            return 1
        for source, n in hits:
            print(f"{source}: {args.probe} ({n} jobs)")
        return 0

    print(f"Validating {args.path} ...", flush=True)
    valid = validate_file(args.path)
    total = sum(len(v) for v in valid.values())
    print(f"\n{total} valid boards total")
    if args.write:
        args.path.write_text(json.dumps(valid, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {args.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
