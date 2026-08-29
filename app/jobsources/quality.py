"""Reputability filter for discovered postings.

First-party ATS boards (Greenhouse / Lever / Ashby / Workable / SmartRecruiters,
and the directory which polls them) are inherently trustworthy — postings come
straight from companies' own career systems. This filter mainly cleans up the
**aggregator** (Google Jobs) and **RSS** feeds, which can surface placeholder /
spam / staffing-churn listings ("Top Company", "Confidential", "Earn $$$ from home").

Deliberately conservative: it drops only clearly-junk postings (placeholder
company, no apply link, obvious spam title) — never a real employer. Anything
borderline still flows through; the relevance scorer and the user's
dismiss/snooze handle the rest.
"""
from __future__ import annotations

import re

from .base import JobPosting

# Sources whose results are first-party (real company career systems) → trusted.
# Directory postings carry the underlying ATS source (greenhouse/lever/ashby/…).
FIRST_PARTY_SOURCES = frozenset({
    "greenhouse", "lever", "ashby", "workable", "smartrecruiters",
})
# Prefer these over RSS/aggregator when the same job arrives twice. Swelist
# listings.json already stores company ATS URLs (not the README Simplify buttons).
PREFERRED_APPLY_SOURCES = FIRST_PARTY_SOURCES | {"swelist"}

# Company values that signal a placeholder / non-real listing.
_GENERIC_COMPANY = frozenset({
    "", "unknown", "n/a", "na", "none", "null", "various", "confidential",
    "company confidential", "undisclosed", "stealth", "stealth startup",
    "top company", "hiring company", "hiring now", "private", "private company",
    "recruiter", "staffing", "staffing agency", "talent", "hr", "company",
    "employer", "client",
    # placeholders the aggregator emits when the real employer is withheld —
    # unactionable: you can't research or address an application to them
    "our client", "confidential company", "leading company", "growing company",
    "fortune 500 company", "fortune 500", "major company", "tbd", "test",
    "recruiting agency", "recruitment agency", "consulting firm", "agency",
})

# Discussion-thread artifacts. RSS feeds of hiring threads (HN "Who is hiring?",
# subreddit roundups) emit one item per *comment*, which arrives as a posting titled
# "New comment by someone in 'Ask HN: Who is hiring? (July 2026)'" with a company
# scraped out of the byline. They're never applyable and they crowd out real matches.
# Caught live in the iOS app during testing.
_THREAD_ARTIFACT = re.compile(
    r"^\s*new comment\b|\bcomment by\b|\bask hn\b|\bwho is hiring\b|"
    r"\bhiring thread\b|\bmonthly thread\b|\[hiring\]\s*$|"
    r"^\s*re:\s|\bmegathread\b",
    re.I,
)

# Obvious job-spam phrasing in titles.
_SPAM_TITLE = re.compile(
    r"(earn \$|\$\d+\s*/?\s*(hr|hour|week|day)\b|work from home today|"
    r"no experience needed|sign[- ]?up bonus|immediate start|apply now!!|"
    r"click here|crypto|giveaway|be your own boss|"
    r"make money|urgent(?:ly)? hiring|hiring immediately|no interview|"
    r"daily pay|weekly pay|\$\$\$|start today)",
    re.I,
)

# Noise to strip when deciding whether two postings are the same job.
_TITLE_NOISE = re.compile(
    r"\b(senior|sr\.?|junior|jr\.?|staff|principal|lead|i{1,3}|iv|v|\d+)\b|"
    r"[\(\[].*?[\)\]]|[^a-z0-9 ]",
    re.I,
)
_SUFFIX_NOISE = re.compile(
    r"\b(inc|llc|ltd|corp|corporation|co|company|technologies|technology|labs|"
    r"group|holdings|the)\b", re.I)


def is_first_party(p: JobPosting) -> bool:
    """True for postings from companies' own ATS (inherently trustworthy)."""
    return (p.source or "").lower() in FIRST_PARTY_SOURCES


def is_reputable(p: JobPosting) -> bool:
    """True if a posting looks like a real, actionable role."""
    if is_first_party(p):
        return True
    company = (p.company or "").strip().lower()
    if company in _GENERIC_COMPANY:
        return False
    if not (p.url or "").strip():
        return False  # no apply link → can't verify or act on it
    if _SPAM_TITLE.search(p.title or ""):
        return False
    # A comment in a hiring thread is a discussion post, not an opening — and the
    # "company" is whatever the byline happened to say.
    if _THREAD_ARTIFACT.search(p.title or "") or _THREAD_ARTIFACT.search(p.company or ""):
        return False
    return True


def filter_reputable(postings: list[JobPosting]) -> tuple[list[JobPosting], int]:
    """Return (kept, dropped_count)."""
    kept = [p for p in postings if is_reputable(p)]
    return kept, len(postings) - len(kept)


def dedup_key(p: JobPosting) -> tuple[str, str]:
    """A (company, role) identity that survives cosmetic differences.

    The same job reaches us from several feeds with the title dressed differently
    — "Senior Software Engineer (Remote)" vs "Software Engineer II" at "Acme" vs
    "Acme, Inc." Seniority words and legal suffixes are stripped so those collapse
    together. Deliberately blunt: over-merging shows one of two near-identical
    listings, while under-merging shows the same job three times.
    """
    company = _SUFFIX_NOISE.sub(" ", (p.company or "").lower())
    company = re.sub(r"[^a-z0-9 ]", " ", company)
    title = _TITLE_NOISE.sub(" ", (p.title or "").lower())
    return (" ".join(company.split()), " ".join(title.split()))


def dedupe(postings: list[JobPosting]) -> tuple[list[JobPosting], int]:
    """Collapse the same job arriving from several *sources*, keeping the copy you
    can apply to directly (first-party ATS) over an aggregator redirect.

    Only ever merges **across** sources. Two reqs on the same board with the same
    title are genuinely two openings — different teams, same job ad — and their
    distinct external ids say so. Merging those would hide real jobs, which is
    exactly the failure this pipeline can't afford. Postings missing a company or
    title are never merged either.

    Order is preserved. Returns (kept, dropped_count).
    """
    # Per duplicate group, the one source we'll keep: a first-party board if any
    # of the copies came from one, else whichever source we saw first.
    winner: dict[tuple[str, str], str] = {}
    for p in postings:
        key = dedup_key(p)
        if not key[0] or not key[1]:
            continue
        source = (p.source or "").lower()
        if key not in winner or (
            source in PREFERRED_APPLY_SOURCES
            and winner[key] not in PREFERRED_APPLY_SOURCES
        ):
            winner[key] = source

    kept = []
    for p in postings:
        key = dedup_key(p)
        if not key[0] or not key[1] or winner.get(key) == (p.source or "").lower():
            kept.append(p)
    return kept, len(postings) - len(kept)
