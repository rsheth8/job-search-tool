"""Job alert formatting: digest summaries and interactive review cards."""
from __future__ import annotations

from collections import Counter

from .jobsources import JobPosting

# Friendly provenance labels so the user can judge a posting's source.
_SOURCE_LABELS = {
    "greenhouse": "Greenhouse",
    "lever": "Lever",
    "ashby": "Ashby",
    "workable": "Workable",
    "smartrecruiters": "SmartRecruiters",
    "rss": "an RSS feed",
    "directory": "an ATS board",
    "swelist": "an internship / new-grad list",
    "yc": "Y Combinator",
}


def _source_label(source: str) -> str:
    return _SOURCE_LABELS.get((source or "").lower(), source or "a job board")


def _digest_mention(posting: JobPosting, score: float) -> str:
    title = posting.title or "a role"
    co = posting.company or posting.source or "a company"
    loc = (posting.location or "").strip()
    if loc.lower() in ("remote", "hybrid", "anywhere", "worldwide"):
        place = f", {loc.lower()}"
    elif loc:
        place = f" in {loc}"
    else:
        place = ""
    return f"{title} at {co}{place} — {round(score * 100)}% match"


def build_digest(
    matches: list[tuple[JobPosting, float, int]],
    *,
    user_id: str | None = None,
) -> str:
    """One in-app message summarizing new matches from a discovery tick."""
    n = len(matches)
    if n == 0:
        return ""
    from . import shortlist

    sorted_matches = shortlist.rank_scored(matches)
    by_company: Counter[str] = Counter()
    for posting, _, _ in sorted_matches:
        by_company[posting.company or posting.source or "Unknown"] += 1

    noun = "match" if n == 1 else "matches"
    top_posting, top_score, _pid = sorted_matches[0]
    mention = _digest_mention(top_posting, top_score)
    if n == 1:
        lead = f"One new match: {mention}."
    else:
        lead = f"{n} new {noun} since last check. The strongest is {mention}."
        if n >= 2:
            p2, s2, _ = sorted_matches[1]
            lead += f" Next is {_digest_mention(p2, s2)}."
        remaining = n - min(n, 2)
        if remaining > 0:
            lead += f" {remaining} more {'is' if remaining == 1 else 'are'} waiting on Apply."
    if user_id:
        from . import voice
        who = voice.first_name(user_id)
        if who:
            lead = f"{who} — {lead}"
    if by_company:
        names = [name for name, _ in by_company.most_common(3)]
        if len(names) == 1:
            lead += f" They're at {names[0]}."
        elif names:
            lead += f" Mostly {', '.join(names[:-1])}, and {names[-1]}."
    lead += " Want to go through them one by one?"
    return lead


def build_review_card(
    posting: JobPosting,
    score: float,
    posting_id: int,
    *,
    position: int,
    total: int,
    profile=None,
) -> str:
    """One job in the interactive review walkthrough — prose, not a command sheet."""
    pct = round((score or 0) * 100)
    co = posting.company or posting.source or "?"
    title = posting.title or "Role"
    lines = [
        f"Here's {position} of {total} — {pct}% match.",
        "",
        f"{title} at {co}.",
    ]
    _ = posting_id
    if profile is not None:
        from . import fit

        detail = fit.explain(posting, profile, score=score)
        if detail["reasons"]:
            lines.append("Fits " + ", ".join(detail["reasons"]) + ".")
        for concern in detail["concerns"][:2]:
            lines.append(str(concern).rstrip(".") + ".")
    if posting.location:
        lines.append(str(posting.location).rstrip(".") + ".")
    lines.append(f"From {_source_label(posting.source)}.")
    return "\n".join(lines)


def build_review_done(*, dismissed: int, applied: int) -> str:
    parts = ["That's all for now."]
    if applied:
        parts.append(f"Queued {applied}.")
    if dismissed:
        parts.append(f"Skipped {dismissed}.")
    return " ".join(parts)
