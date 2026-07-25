"""Job alert formatting: digest summaries and interactive review cards."""
from __future__ import annotations

from collections import Counter

from .config import get_settings
from .jobsources import JobPosting

# Shown at the bottom of every review card.
_REVIEW_HELP = (
    "Reply: skip · apply · stop (or dismiss all to clear the queue)"
)

# Friendly provenance labels so the user can judge a posting's source.
_SOURCE_LABELS = {
    "greenhouse": "company board (Greenhouse)",
    "lever": "company board (Lever)",
    "ashby": "company board (Ashby)",
    "rss": "RSS feed",
    "aggregator": "Google Jobs",
}


def _source_label(source: str) -> str:
    return _SOURCE_LABELS.get((source or "").lower(), source or "unknown")


def build_digest(
    matches: list[tuple[JobPosting, float, int]],
) -> str:
    """One Slack message summarizing new matches from a discovery tick."""
    n = len(matches)
    if n == 0:
        return ""
    top_n = get_settings().job_digest_top_n
    sorted_matches = sorted(matches, key=lambda m: m[1], reverse=True)
    by_company: Counter[str] = Counter()
    for posting, _, _ in sorted_matches:
        by_company[posting.company or posting.source or "Unknown"] += 1

    lines = [f"📬 {n} new job match{'es' if n != 1 else ''} since last check"]
    if by_company:
        breakdown = ", ".join(
            f"{name} {count}" for name, count in by_company.most_common(5)
        )
        extra = len(by_company) - 5
        if extra > 0:
            breakdown += f", +{extra} more"
        lines.append(f"• {breakdown}")

    lines.append("")
    lines.append("Top matches:")
    for posting, score, pid in sorted_matches[:top_n]:
        pct = round(score * 100)
        co = posting.company or posting.source
        title = posting.title or "Role"
        loc = f" — {posting.location}" if posting.location else ""
        lines.append(f"• {title} @ {co}{loc} ({pct}% · #{pid})")

    remaining = n - min(n, top_n)
    if remaining > 0:
        lines.append(f"• …and {remaining} more in your queue")

    lines.append("")
    lines.append(
        'Say "review jobs" to go through them one by one, or "any new jobs" for a quick list.'
    )
    return "\n".join(lines)


def build_review_card(
    posting: JobPosting,
    score: float,
    posting_id: int,
    *,
    position: int,
    total: int,
    profile=None,
) -> str:
    """One job in the interactive review walkthrough."""
    pct = round((score or 0) * 100)
    co = posting.company or posting.source or "?"
    title = posting.title or "Role"
    lines = [
        f"📋 Job {position} of {total} · {pct}% match · #{posting_id}",
        f"{title} @ {co}",
    ]
    # Why this one surfaced, in words — a percentage alone can't be argued with.
    if profile is not None:
        from . import fit

        detail = fit.explain(posting, profile, score=score)
        if detail["reasons"]:
            lines.append("✅ " + " · ".join(detail["reasons"]))
        for concern in detail["concerns"][:2]:
            lines.append(f"⚠️ {concern}")
    if posting.location:
        lines.append(f"📍 {posting.location}")
    lines.append(f"🔎 via {_source_label(posting.source)}")
    if posting.url:
        lines.append(f"🔗 {posting.url}")
    lines.append("")
    lines.append(_REVIEW_HELP)
    return "\n".join(lines)


def build_review_done(*, dismissed: int, applied: int) -> str:
    parts = ["✅ Done reviewing."]
    if applied:
        parts.append(f"Logged {applied} application(s).")
    if dismissed:
        parts.append(f"Skipped {dismissed}.")
    parts.append('Say **"review jobs"** anytime more are queued.')
    return " ".join(parts)
