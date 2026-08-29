"""Pipeline analytics — "how am I doing?" over the whole job search.

Pure-ish: ``compute_stats`` reads the DB and returns a plain dict; ``render``
turns it into a compact SMS/CLI summary. No LLM, no network — instant and free.

Rate definitions (all over total applications):
  * **response rate** — got *any* reply: anything past "Applied" that isn't
    "Ghosted" (a rejection still counts as a response).
  * **interview rate** — reached Phone screen or later (Phone screen, Interview,
    Onsite, Offer).
  * **offer / ghost rate** — self-explanatory.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import scoring, store
from .intents import CANONICAL_STATUSES

# Stages that count as "the company responded".
_RESPONDED = {"OA received", "Phone screen", "Interview", "Onsite", "Offer", "Rejected"}
_INTERVIEWING = {"Phone screen", "Interview", "Onsite", "Offer"}
# An active app whose last activity is older than this is "going stale".
STALE_AFTER_DAYS = 10


def compute_stats(user_id: str, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    apps = store.list_applications(user_id, limit=10_000)
    total = len(apps)

    by_stage = {s: 0 for s in CANONICAL_STATUSES}
    responded = interviewing = offers = ghosted = 0
    new_7 = new_30 = stale = 0

    for a in apps:
        status = a["status"]
        by_stage[status] = by_stage.get(status, 0) + 1
        if status in _RESPONDED:
            responded += 1
        if status in _INTERVIEWING:
            interviewing += 1
        if status == "Offer":
            offers += 1
        if status == "Ghosted":
            ghosted += 1

        age = scoring.days_since(a["applied_at"], now)
        if age <= 7:
            new_7 += 1
        if age <= 30:
            new_30 += 1

        if status not in scoring.TERMINAL_STATUSES:
            if scoring.days_since(a["last_updated_at"], now) >= STALE_AFTER_DAYS:
                stale += 1

    active = sum(
        c for s, c in by_stage.items() if s not in scoring.TERMINAL_STATUSES
    )

    def pct(n: int) -> int:
        return round(100 * n / total) if total else 0

    return {
        "total": total,
        "active": active,
        "by_stage": by_stage,
        "responded": responded,
        "response_rate": pct(responded),
        "interviewing": interviewing,
        "interview_rate": pct(interviewing),
        "offers": offers,
        "offer_rate": pct(offers),
        "ghosted": ghosted,
        "ghost_rate": pct(ghosted),
        "new_7": new_7,
        "new_30": new_30,
        "stale": stale,
    }


# Short labels keep the funnel line inside a single SMS.
_STAGE_LABELS = {
    "Applied": "Applied",
    "OA received": "OA",
    "Phone screen": "Phone",
    "Interview": "Interview",
    "Onsite": "Onsite",
    "Offer": "Offer",
    "Rejected": "Rejected",
    "Ghosted": "Ghosted",
}


def render(stats: dict) -> str:
    if stats["total"] == 0:
        return (
            "No applications tracked yet. Mark Filed on Apply, or say "
            "“applied Stripe SWE”."
        )
    funnel = ", ".join(
        f"{stats['by_stage'][s]} {_STAGE_LABELS[s]}"
        for s in CANONICAL_STATUSES
        if stats["by_stage"].get(s)
    )
    offer_word = "offer" if stats["offers"] == 1 else "offers"
    parts = [
        f"You've logged {stats['total']} apps, {stats['active']} still active "
        f"({funnel}).",
        f"Response {stats['response_rate']}% · "
        f"Interview+ {stats['interview_rate']}% · "
        f"{stats['offers']} {offer_word}.",
    ]
    tail = f"{stats['new_7']} new in the last 7 days."
    if stats["stale"]:
        tail += f" {stats['stale']} going stale."
    if stats["ghost_rate"]:
        tail += f" Ghosted {stats['ghost_rate']}%."
    parts.append(tail)
    return " ".join(parts)
