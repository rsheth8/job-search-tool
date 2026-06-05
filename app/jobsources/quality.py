"""Reputability filter for discovered postings.

First-party ATS boards (Greenhouse / Lever / Ashby, and the directory which polls
them) are inherently trustworthy — postings come straight from companies' own
career systems. This filter mainly cleans up the **aggregator** (Google Jobs) and
**RSS** feeds, which can surface placeholder / spam / staffing-churn listings
("Top Company", "Confidential", "Earn $$$ from home").

Deliberately conservative: it drops only clearly-junk postings (placeholder
company, no apply link, obvious spam title) — never a real employer. Anything
borderline still flows through; the relevance scorer and the user's
dismiss/snooze handle the rest.
"""
from __future__ import annotations

import re

from .base import JobPosting

# Sources whose results are first-party (real company career systems) → trusted.
# Directory postings carry the underlying ATS source (greenhouse/lever/ashby).
FIRST_PARTY_SOURCES = frozenset({"greenhouse", "lever", "ashby"})

# Company values that signal a placeholder / non-real listing.
_GENERIC_COMPANY = frozenset({
    "", "unknown", "n/a", "na", "none", "null", "various", "confidential",
    "company confidential", "undisclosed", "stealth", "stealth startup",
    "top company", "hiring company", "hiring now", "private", "private company",
    "recruiter", "staffing", "staffing agency", "talent", "hr", "company",
    "employer", "client",
})

# Obvious job-spam phrasing in titles.
_SPAM_TITLE = re.compile(
    r"(earn \$|\$\d+\s*/?\s*(hr|hour|week|day)\b|work from home today|"
    r"no experience needed|sign[- ]?up bonus|immediate start|apply now!!|"
    r"click here|crypto|giveaway|be your own boss)",
    re.I,
)


def is_reputable(p: JobPosting) -> bool:
    """True if a posting looks like a real, actionable role."""
    if (p.source or "").lower() in FIRST_PARTY_SOURCES:
        return True
    company = (p.company or "").strip().lower()
    if company in _GENERIC_COMPANY:
        return False
    if not (p.url or "").strip():
        return False  # no apply link → can't verify or act on it
    if _SPAM_TITLE.search(p.title or ""):
        return False
    return True


def filter_reputable(postings: list[JobPosting]) -> tuple[list[JobPosting], int]:
    """Return (kept, dropped_count)."""
    kept = [p for p in postings if is_reputable(p)]
    return kept, len(postings) - len(kept)
