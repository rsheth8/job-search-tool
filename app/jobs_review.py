"""Interactive one-by-one review of queued job matches."""
from __future__ import annotations

import re

from . import conversation as convo
from . import job_alerts, jobstore, store
from .conversation import Pending
from .intents import Intent
from .jobsources import JobPosting

_DISMISS_ALL = re.compile(r"\b(dismiss all|clear (the )?queue|skip all)\b", re.I)
_SKIP = re.compile(r"^\s*(skip|next|pass)\s*[.!]*\s*$", re.I)
_APPLY = re.compile(
    r"^\s*(apply|yes|interested|log it|apply to this)\s*[.!]*\s*$", re.I
)
_APPLY_ID = re.compile(r"^\s*apply\s+#?(\d+)\s*$", re.I)


def _row_to_posting(row) -> JobPosting:
    return JobPosting(
        source=row["source"],
        external_id=row["external_id"],
        title=row["title"] or "",
        url=row["url"] or "",
        company=row["company"] or "",
        location=row["location"] or "",
        description=row["description"] or "",
        posted_at=row["posted_at"] or "",
    )


def start_review(user_id: str) -> str:
    """Begin or resume the review walkthrough."""
    queue = jobstore.list_review_queue(user_id)
    if not queue:
        if not jobstore.list_tracked(user_id):
            return ("You're not tracking any boards yet. Try "
                    "'track openings at <company>' first.")
        return ("No jobs waiting in your queue. I'll send a digest when new "
                "matches land — or say 'any new jobs' to browse.")

    convo.set_pending(
        user_id,
        Pending(
            intent=Intent.JOBS_REVIEW.value,
            slots={"index": 0, "dismissed": 0, "applied": 0},
            awaiting="choice",
        ),
    )
    return _card_for_queue(user_id, queue, index=0)


def _card_for_queue(user_id: str, queue: list, index: int) -> str:
    from . import profile as profile_mod

    row = queue[index]
    posting = _row_to_posting(row)
    return job_alerts.build_review_card(
        posting,
        row["relevance_score"] or 0,
        row["id"],
        position=index + 1,
        total=len(queue),
        profile=profile_mod.get_profile(user_id),
    )


def continue_review(user_id: str, pending: Pending, text: str) -> str:
    """Handle skip / apply / stop during an active review session."""
    if _DISMISS_ALL.search(text):
        n = jobstore.dismiss_all_queued(user_id)
        convo.clear_pending(user_id)
        return f"Cleared {n} job(s) from your queue. Say 'review jobs' when more arrive."

    if convo.is_cancel(text) or re.search(r"^\s*(stop|quit|done|exit)\s*$", text, re.I):
        d, a = pending.slots.get("dismissed", 0), pending.slots.get("applied", 0)
        convo.clear_pending(user_id)
        if d or a:
            return job_alerts.build_review_done(dismissed=d, applied=a)
        return "Stopped reviewing. Your queue is still there — say 'review jobs' anytime."

    queue = jobstore.list_review_queue(user_id)
    if not queue:
        convo.clear_pending(user_id)
        return job_alerts.build_review_done(
            dismissed=pending.slots.get("dismissed", 0),
            applied=pending.slots.get("applied", 0),
        )

    index = min(int(pending.slots.get("index", 0)), len(queue) - 1)
    row = queue[index]
    posting = _row_to_posting(row)

    apply_m = _APPLY_ID.match(text.strip())
    if apply_m:
        target_id = int(apply_m.group(1))
        if target_id != row["id"]:
            return f"You're on #{row['id']} — reply apply for this one, or skip first."
    elif _APPLY.match(text.strip()):
        pass
    elif _SKIP.match(text.strip()):
        jobstore.mark_posting_status(row["id"], "dismissed")
        pending.slots["dismissed"] = pending.slots.get("dismissed", 0) + 1
        return _advance(user_id, pending)
    else:
        return (
            f"On #{row['id']} — reply skip, apply, or stop. "
            "(dismiss all clears the rest.)"
        )

    # apply current card
    store.create_application(
        user_id,
        posting.company or "Unknown",
        posting.title or "Role",
        source=f"discovery:{posting.source}",
    )
    jobstore.mark_posting_status(row["id"], "applied")
    pending.slots["applied"] = pending.slots.get("applied", 0) + 1
    link = f"\n🔗 {posting.url}" if posting.url else ""
    next_msg = _advance(user_id, pending)
    return f"✅ Logged {posting.title} @ {posting.company or '?'}{link}\n\n{next_msg}"


def _advance(user_id: str, pending: Pending) -> str:
    queue = jobstore.list_review_queue(user_id)
    if not queue:
        convo.clear_pending(user_id)
        return job_alerts.build_review_done(
            dismissed=pending.slots.get("dismissed", 0),
            applied=pending.slots.get("applied", 0),
        )
    pending.slots["index"] = 0
    convo.set_pending(user_id, pending)
    return _card_for_queue(user_id, queue, index=0)
