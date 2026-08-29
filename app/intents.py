"""Structured intent representation shared by the router and the engine."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Intent(str, Enum):
    APPLY = "APPLY"
    UPDATE = "UPDATE"
    NOTE = "NOTE"
    LIST = "LIST"
    REMIND = "REMIND"
    QUERY = "QUERY"
    STATS = "STATS"
    DEADLINE = "DEADLINE"
    CHECK = "CHECK"
    DELETE = "DELETE"
    EDIT = "EDIT"
    BULK = "BULK"
    UNDO = "UNDO"
    # Job discovery (Phase 1)
    TRACK = "TRACK"      # track / untrack / list a company's job board
    JOBS = "JOBS"        # browse postings discovery has surfaced
    JOBS_REVIEW = "JOBS_REVIEW"  # interactive one-by-one review of queued matches
    PROFILE = "PROFILE"  # set or show the job-search profile
    # Assisted apply (Phase 2)
    APPLY_JOB = "APPLY_JOB"  # apply to a discovered posting (by # or company)
    QUEUE_JOB = "QUEUE_JOB"  # stage a discovered posting into the apply queue
    # Discovery polish
    DISMISS_JOB = "DISMISS_JOB"  # hide a discovered posting for good
    SNOOZE_JOB = "SNOOZE_JOB"    # mute a posting for a while, then resurface
    TUNE = "TUNE"                # adjust the match/alert threshold
    # Personal knowledge — what makes a drafted answer specific to you
    REMEMBER = "REMEMBER"          # store a project/achievement/strength/answer
    KNOWLEDGE = "KNOWLEDGE"        # show what's known + what's missing
    # In-app assistant (how-to + form identity edits)
    HELP_APP = "HELP_APP"          # how Autofill / screens / submit work; open a dest
    SET_IDENTITY = "SET_IDENTITY"  # patch applicant identity (phone, email, …)
    UNKNOWN = "UNKNOWN"


# Canonical application stages. Free-text statuses are normalized toward these
# but never rejected — the doc's rule is "store something useful, even if imperfect".
CANONICAL_STATUSES = [
    "Applied",
    "OA received",
    "Phone screen",
    "Interview",
    "Onsite",
    "Offer",
    "Rejected",
    "Ghosted",
]


class ParsedMessage(BaseModel):
    """The router's view of one inbound SMS."""

    intent: Intent = Intent.UNKNOWN
    company: str | None = None
    role: str | None = None
    status: str | None = None
    message: str | None = None  # note / outreach body
    time_reference: str | None = None  # e.g. "in 3 days", "today"
    confidence: float = Field(0.0, ge=0.0, le=1.0)

    def is_high(self) -> bool:
        return self.confidence >= 0.8

    def is_medium(self) -> bool:
        return 0.4 <= self.confidence < 0.8

    def is_low(self) -> bool:
        return self.confidence < 0.4
