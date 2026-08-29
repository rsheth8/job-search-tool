"""Multi-turn conversation state + natural-language helpers.

The engine is otherwise stateless per message. This module gives it a short
memory of *what it just asked*, so a bare reply ("spotify", "yes", "interview")
can be threaded back into the question that prompted it.

State lifetime: a pending exchange is dropped after EXPIRY_MINUTES of silence so
an abandoned half-finished command never hijacks a fresh, unrelated message.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .db import connect

EXPIRY_MINUTES = 30


@dataclass
class Pending:
    intent: str | None = None
    slots: dict = field(default_factory=dict)
    awaiting: str | None = None

    @property
    def active(self) -> bool:
        return self.intent is not None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_pending(user_id: str) -> Pending:
    with connect() as conn:
        row = conn.execute(
            "SELECT pending_intent, slots, awaiting, updated_at "
            "FROM conversation_state WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if row is None or not row["pending_intent"]:
        return Pending()
    # Expire stale exchanges.
    try:
        updated = datetime.fromisoformat(row["updated_at"])
        if (_now() - updated).total_seconds() > EXPIRY_MINUTES * 60:
            clear_pending(user_id)
            return Pending()
    except (TypeError, ValueError):
        pass
    return Pending(
        intent=row["pending_intent"],
        slots=json.loads(row["slots"] or "{}"),
        awaiting=row["awaiting"],
    )


def set_pending(user_id: str, pending: Pending) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO conversation_state
                (user_id, pending_intent, slots, awaiting, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                pending_intent = excluded.pending_intent,
                slots = excluded.slots,
                awaiting = excluded.awaiting,
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                pending.intent,
                json.dumps(pending.slots),
                pending.awaiting,
                _now().isoformat(),
            ),
        )


def clear_pending(user_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "DELETE FROM conversation_state WHERE user_id = ?", (user_id,)
        )


# ---------------------------------------------------------------------------
# Natural-language micro-classifiers (backend-agnostic)
# ---------------------------------------------------------------------------

_AFFIRM = re.compile(
    r"^\s*(y|ya|ya+h?|ye[aps]+|yup|yep|yes+|sure|ok(ay)?|correct|right|"
    r"that'?s? (right|correct|it)|do it|send it|go( ahead)?|confirm(ed)?|"
    r"sounds good|please do|absolutely|definitely|👍)\s*[.!]*\s*$",
    re.I,
)

_NEGATE = re.compile(
    r"^\s*(n|no+|nope|nah|negative|don'?t|do not|not? (that|right|it))\s*[.!]*\s*$",
    re.I,
)

_CANCEL = re.compile(
    r"\b(cancel|never ?mind|nvm|forget it|scrap (it|that)|stop|drop it|"
    r"start over|reset)\b",
    re.I,
)

_CORRECTION = re.compile(
    r"^\s*(no[,. ]+|nope[,. ]+|actually[,. ]*|i meant[,. ]*|not? .*?,?\s*i meant\s*)",
    re.I,
)

_GREETING = re.compile(
    r"^\s*(hi+|hey+|hello+|yo|sup|howdy|good (morning|afternoon|evening))\b",
    re.I,
)

_HELP = re.compile(
    r"\b(help|what can you do|how (do|does) (this|you) work|commands?|"
    r"what do you do|menu|options|get started|examples?)\b"
    r"|^\s*\?+\s*$",  # a lone "?" — but not a question that merely contains "?"
    re.I,
)


def is_affirmation(text: str) -> bool:
    return bool(_AFFIRM.match(text))


def is_negation(text: str) -> bool:
    return bool(_NEGATE.match(text))


def is_cancel(text: str) -> bool:
    return bool(_CANCEL.search(text))


def is_correction(text: str) -> bool:
    return bool(_CORRECTION.match(text))


def is_greeting(text: str) -> bool:
    return bool(_GREETING.match(text))


def is_help(text: str) -> bool:
    return bool(_HELP.search(text))


def is_hop_go(text: str) -> bool:
    """Accept a proposed tab hop (BofA-style)."""
    return bool(_HOP_GO.match(text)) or is_affirmation(text)


def is_hop_stay(text: str) -> bool:
    """Decline a proposed tab hop."""
    return bool(_HOP_STAY.search(text)) or is_negation(text) or is_cancel(text)


_HOP_GO = re.compile(
    r"^\s*(take me there|take me|let'?s go|go there|head(ing)? there|"
    r"open it|yes please)\s*[!.]*\s*$",
    re.I,
)
_HOP_STAY = re.compile(
    r"^\s*(stay here|stay|not now|no thanks|maybe later|i'?ll stay|"
    r"don'?t (take|open|go))\b",
    re.I,
)


# --- smalltalk: thanks / acknowledgements / sign-off / compliments ----------
# Handled only when there's no pending exchange and no actionable intent, so a
# bare "ok"/"cool" reads as a friendly ack rather than the confused fallback.

_THANKS = re.compile(r"\b(thanks|thank you|thank u|thx|tysm|ty|appreciate(d| it)?|cheers)\b", re.I)
_BYE = re.compile(
    r"\b(bye+|goodbye|good ?night|night|nite|gn|see (ya|you)|talk later|"
    r"that'?s all|that is all|i'?m done|done for now|no(thing)?( else| more)?( for now)?)\b",
    re.I,
)
_COMPLIMENT = re.compile(
    r"(you('?re| are) (the best|awesome|great|amazing|helpful|so good|so helpful)|"
    r"good (bot|job|work)|nice work|well done|love (you|this|it|ya)|you rock)",
    re.I,
)
_ACK = re.compile(
    r"^\s*(ok(ay)?|k|cool|nice|great|sweet|awesome|perfect|gotcha|got it|word|bet|"
    r"sounds good|makes sense|fair|right on|👍|🙏|❤️|🔥|✅)\s*[.!]*\s*$",
    re.I,
)


def smalltalk_reply(text: str, *, name: str = "") -> str | None:
    """A warm reply for conversational filler, or None if it's not smalltalk."""
    who = (name or "").strip()
    if _COMPLIMENT.search(text):
        if who:
            return f"🙏 Happy to help, {who} — that's what I'm here for. What's next?"
        return "🙏 Happy to help — that's what I'm here for. What's next?"
    if _THANKS.search(text):
        return "You got it. Want to look at jobs, or change a detail?"
    if _BYE.search(text):
        if who:
            return f"Catch you later, {who} — good luck out there."
        return "Catch you later — good luck out there."
    if _ACK.match(text):
        return "Standing by — ask whenever."
    return None


def strip_correction_prefix(text: str) -> str:
    """Drop a leading 'no, ' / 'actually ' / 'i meant ' so the remainder can be
    re-parsed as the corrected value."""
    return _CORRECTION.sub("", text, count=1).strip()
