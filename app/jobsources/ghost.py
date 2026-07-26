"""Ghost-job / fake-listing filter (Matching v2, Phase 3).

Goes a step past ``quality.py`` (which drops obvious spam/placeholder listings):
this catches **ghost jobs** — reqs that are never really hiring. The classic
tells are evergreen "always accepting applications / talent pool" language,
listings reposted over and over, very stale postings, scam contact-by-personal-
email, and earn-big comp hype in the body.

Rules-first and deliberately **conservative** (same stance as ``quality``): a
posting is dropped only on strong evidence (one high-precision signal, or several
weaker ones). First-party ATS postings (Greenhouse/Lever/Ashby) are always
trusted — ghosts come from the aggregator + RSS feeds. The relevance scorer and
the user's dismiss/snooze still handle anything borderline that slips through.

Pure + stateless except for ``repost_count``, which the caller supplies from the
posting history (keeps this module trivially testable). An LLM-on-borderline pass
is a deliberate later step, not done here.
"""
from __future__ import annotations

import re

from . import quality
from .base import JobPosting

# Drop when accumulated ghost evidence reaches this. One strong signal (0.6) trips
# it; two medium ones (0.3 each) also do. Tuned to favor false negatives over
# false positives — never silently bury a real role.
GHOST_THRESHOLD = 0.6

# Evergreen / pipeline language — high-precision "not actually hiring now" tell.
_EVERGREEN_RE = re.compile(
    r"(always (?:hiring|accepting|looking)|evergreen|talent (?:pool|pipeline|"
    r"community|network)|join our talent|general application|future "
    r"opportunit|pipeline (?:req|role|position)|we are always|expression of "
    r"interest|keep your resume on file)",
    re.I,
)
# Personal free-mail contact in a JD body — real employers use their own domain.
_PERSONAL_EMAIL_RE = re.compile(
    r"[a-z0-9._%+-]+@(gmail|yahoo|hotmail|outlook|aol|proton(?:mail)?|icloud)\.",
    re.I,
)
# Earn-big comp hype in the body (quality.py only scans titles).
_COMP_HYPE_RE = re.compile(
    r"(unlimited (?:earning|income)|earn \$|\$\$\$|make \$\d|"
    r"up to \$\d[\d,]*\s*(?:/|per )?\s*(?:day|week)|six[- ]figure income)",
    re.I,
)
# The req is explicitly closed. Not a *ghost* so much as a corpse — recommending it
# wastes a real application, so it's the strongest tell we have.
_CLOSED_RE = re.compile(
    r"(no longer (?:accepting|available|open|active)|this (?:position|role|job|req)"
    r" (?:has been|is) (?:filled|closed)|position (?:filled|closed)|"
    r"applications? (?:are )?closed|we(?:'ve| have) (?:since )?filled|"
    r"posting (?:has )?expired|req(?:uisition)? closed)",
    re.I,
)
# Pay-to-play / not-really-a-job tells: MLM, commission-only, unpaid "opportunities".
_NOT_A_JOB_RE = re.compile(
    r"(100%\s*commission|commission[- ]only|unpaid (?:intern|position|role)|"
    r"must (?:purchase|invest|buy)|start[- ]?up (?:fee|cost)|"
    r"(?:training|starter) (?:kit|fee)|be your own boss|no salary)",
    re.I,
)
# "30+ days ago" / "45 days ago" style staleness from aggregator/RSS extensions,
# plus the week/month phrasings the same feeds use interchangeably.
_AGE_RE = re.compile(r"(\d+)\s*\+?\s*days?\s*ago", re.I)
_AGE_WEEKS_RE = re.compile(r"(\d+)\s*\+?\s*weeks?\s*ago", re.I)
_AGE_MONTHS_RE = re.compile(r"(\d+)\s*\+?\s*months?\s*ago", re.I)
_STALE_AGE_DAYS = 45
_THIN_DESC_CHARS = 120


def _is_stale(posted_at: str | None) -> bool:
    """Best-effort 'this has been up a long time' check; never raises."""
    if not posted_at:
        return False
    text = posted_at.strip().lower()
    if "30+" in text or "60+" in text:
        return True
    for rx, per_unit in ((_AGE_RE, 1), (_AGE_WEEKS_RE, 7), (_AGE_MONTHS_RE, 30)):
        m = rx.search(text)
        if m:
            try:
                return int(m.group(1)) * per_unit >= _STALE_AGE_DAYS
            except ValueError:
                return False
    # ISO date → days since.
    try:
        from datetime import datetime, timezone

        dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days >= 60
    except (ValueError, TypeError):
        return False


def is_closed(p: JobPosting) -> bool:
    """The posting says outright that it's filled/closed/expired.

    Checked for **every** source, first-party included: a Greenhouse board is
    trustworthy about who's hiring, but a closed req on it is still a wasted
    application, and that's the one thing worth overriding the trust rule for.
    """
    return bool(_CLOSED_RE.search(p.description or "")
                or _CLOSED_RE.search(p.title or ""))


def ghost_signals(p: JobPosting, *, repost_count: int = 0) -> list[tuple[str, float]]:
    """Weighted (reason, score) ghost tells for a posting. Empty for first-party,
    except an explicitly-closed req, which is never worth surfacing."""
    if is_closed(p):
        return [("closed / already filled", 1.0)]
    if quality.is_first_party(p):
        return []
    desc = p.description or ""
    signals: list[tuple[str, float]] = []
    if _NOT_A_JOB_RE.search(desc) or _NOT_A_JOB_RE.search(p.title or ""):
        signals.append(("commission-only / pay-to-play", 0.6))
    if _EVERGREEN_RE.search(desc) or _EVERGREEN_RE.search(p.title or ""):
        signals.append(("evergreen/pipeline language", 0.6))
    if _PERSONAL_EMAIL_RE.search(desc):
        signals.append(("personal-email contact", 0.6))
    if _COMP_HYPE_RE.search(desc):
        signals.append(("earn-big comp hype", 0.5))
    if repost_count >= 3:
        signals.append(("reposted many times", 0.6))
    elif repost_count == 2:
        signals.append(("reposted", 0.3))
    if _is_stale(p.posted_at):
        signals.append(("stale posting", 0.4))
    if 0 < len(desc) < _THIN_DESC_CHARS:
        signals.append(("thin description", 0.25))
    return signals


def ghost_score(p: JobPosting, *, repost_count: int = 0) -> float:
    """Total ghost evidence in [0, 1]."""
    return min(1.0, sum(w for _, w in ghost_signals(p, repost_count=repost_count)))


def is_ghost(p: JobPosting, *, repost_count: int = 0) -> bool:
    return ghost_score(p, repost_count=repost_count) >= GHOST_THRESHOLD
