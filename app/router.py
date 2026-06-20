"""SMS -> structured intent router.

Two interchangeable backends:
  * HeuristicRouter  - zero-dependency, runs offline, used when no ANTHROPIC_API_KEY.
  * AnthropicRouter  - Claude-backed structured extraction when a key is present.

Both return a ParsedMessage. The engine layer is identical regardless of backend.
"""
from __future__ import annotations

import json
import re

from .config import get_settings
from .intents import CANONICAL_STATUSES, Intent, ParsedMessage
from .ratelimit import TokenBucket

# ---------------------------------------------------------------------------
# Shared status normalization
# ---------------------------------------------------------------------------

_STATUS_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(oa|online assessment|assessment|hackerrank|codesignal)\b", re.I), "OA received"),
    (re.compile(r"\b(phone screen|recruiter (call|screen)|phone call)\b", re.I), "Phone screen"),
    (re.compile(r"\b(onsite|on-site|final round|super day)\b", re.I), "Onsite"),
    (re.compile(r"\b(interview|technical|tech screen)\b", re.I), "Interview"),
    (re.compile(r"\b(offer|got the job|accepted)\b", re.I), "Offer"),
    (re.compile(r"\b(reject|rejected|denied|turned down|no thanks)\b", re.I), "Rejected"),
    (re.compile(r"\b(ghost|ghosted|no response|silence)\b", re.I), "Ghosted"),
    (re.compile(r"\b(applied|submitted|application)\b", re.I), "Applied"),
]


def clean_company(text: str) -> str:
    """Best-effort company name from a bare reply ('spotify' -> 'Spotify')."""
    company, _ = _extract_company_role(text)
    return company or _titlecase_company(text.strip())


def clean_role(text: str) -> str:
    """Title-case a bare role reply ('swe ii' -> 'SWE II')."""
    return _titlecase_role(text.strip())


def normalize_status(text: str | None) -> str | None:
    """Map free text toward a canonical stage; otherwise title-case it as-is."""
    if not text:
        return None
    text = text.strip()
    for pattern, canonical in _STATUS_PATTERNS:
        if pattern.search(text):
            return canonical
    # Unknown but non-empty: keep it (never discard user input).
    return text if text not in CANONICAL_STATUSES else text


# ---------------------------------------------------------------------------
# Heuristic (no-key) backend
# ---------------------------------------------------------------------------

_APPLY_RE = re.compile(r"\b(applied|apply|application to|just applied)\b", re.I)
_UPDATE_RE = re.compile(r"\b(update|moved to|now at|status|got an?|received|reject|interview|offer|onsite|oa)\b", re.I)
_NOTE_RE = re.compile(r"\bnote\b", re.I)
_LIST_RE = re.compile(r"^\s*(list|show|show me|what have i applied)\b", re.I)
_REMIND_RE = re.compile(r"\b(remind|reminder|follow up in|ping me)\b", re.I)
_OUTREACH_RE = re.compile(r"\b(reach out|outreach|recruiter|contact|message the)\b", re.I)
_QUERY_RE = re.compile(r"\b(what should i|who should i|what do i|follow up on|what's overdue|whats overdue)\b", re.I)
_STATS_RE = re.compile(r"\b(stats|statistics|how am i doing|how'?s it going|my numbers|funnel|progress|summary|overview)\b", re.I)
_DEADLINE_RE = re.compile(r"\b(due|deadline|deadlines|upcoming|coming up|agenda|calendar)\b", re.I)
_CHECK_RE = re.compile(
    r"\b(status of|where (am i|do i stand)|what'?s (the )?(latest|status|news|happening) "
    r"(with|on|for|at)|latest on|update on|tell me about|do i have|did i (already )?apply "
    r"(to|for)?|have i applied|when did i apply|what about)\b",
    re.I,
)
_DELETE_RE = re.compile(
    r"\b(delete|remove|get rid of|take .* off|forget about|never applied|"
    r"didn'?t (actually |really )?apply)\b",
    re.I,
)
_EDIT_RE = re.compile(
    r"\b(rename|change|fix|correct|is actually|should (be|say)|wrong (role|name|title)|"
    r"got the (role|name|title) wrong)\b",
    re.I,
)
# Bulk needs BOTH a quantifier and an action verb, so "list all applications" stays LIST.
_BULK_QUANT = re.compile(
    r"\b(everything|anything|every ?one|anyone|any of|all|every|each|the rest)\b", re.I
)
_BULK_VERB = re.compile(r"\b(reject|rejected|ghost|ghosted|mark|move|set|withdraw|close|archive)\b", re.I)

# --- Job discovery (Phase 1) -----------------------------------------------
# TRACK: add/remove a company's job board, or list tracked boards. "watch"/
# "monitor" included; "follow" is deliberately excluded so "follow up on" stays
# QUERY.
_TRACK_RE = re.compile(r"\b(untrack|unwatch|track|watch|monitor|tracking|tracked)\b", re.I)
_TRACK_REMOVE_RE = re.compile(r"\b(untrack|unwatch)\b|\bstop (tracking|watching)\b", re.I)
_TRACK_FEED_RE = re.compile(r"\btrack\s+feed\s+([\w-]+)", re.I)
_TRACK_LIST_RE = re.compile(
    r"\b(what|which|list|show)\b[^.?!]*\btrack(?:ing|ed)\b"
    r"|\btracked (companies|boards|jobs)\b"
    r"|^\s*tracking\s*\??$",
    re.I,
)
# JOBS: browse postings discovery surfaced. Requires discovery-y phrasing so a
# stray "job" in "applied to a job" doesn't hijack it (APPLY guard also applies).
_JOBS_REVIEW_RE = re.compile(
    r"\breview\b[^.?!]*\b(jobs?|matches?|queue|roles?)\b"
    r"|\b(go through|walk through|start reviewing)\b[^.?!]*\bjobs?\b"
    r"|\b(let'?s|start)\b[^.?!]*\b(go through|review)\b[^.?!]*\bjobs?\b"
    r"|\bgo through (the )?(new )?(jobs?|matches?|queue)\b",
    re.I,
)
_JOBS_RE = re.compile(
    r"\b(openings?|postings?)\b"
    r"|\bnew (jobs?|roles?|gigs?)\b"
    r"|\b(show|see|list|any|what'?s?|latest|recent|find|anything)\b[^.?!]*\bjobs?\b"
    r"|^\s*jobs?\s*\??$",
    re.I,
)
# APPLY_JOB: apply to a posting discovery surfaced (the alert prints "#<id>" and
# "reply apply <id>"). Matches a numeric id ("apply 2", "apply to #2") or a
# company reference ("apply to the stripe one"). Present-tense "apply" only —
# "applied to a job at google" stays APPLY (\bapply\b never matches "applied").
_APPLY_JOB_RE = re.compile(
    r"\bapply\b\s+(?:to\s+)?#(\d+)\b"                              # apply #2 / apply to #2
    r"|\bapply\s+(\d+)\b"                                          # apply 2
    r"|\bapply\s+to\s+the\s+(.+?)\s+(?:one|posting|role|job|opening)\b",  # apply to the stripe one
    re.I,
)

# DISMISS_JOB / SNOOZE_JOB: manage a surfaced posting by # (or company ref).
_DISMISS_JOB_RE = re.compile(
    r"\b(?:dismiss|hide|ignore|not interested(?: in)?)\b\s*#?(\d+)\b"
    r"|\b(?:dismiss|hide|ignore|not interested(?: in)?)\b\s+the\s+(.+?)\s+(?:one|posting|role|job|opening)\b",
    re.I,
)
_SNOOZE_JOB_RE = re.compile(
    r"\bsnooze\b\s*#?(\d+)\b"
    r"|\bsnooze\b\s+the\s+(.+?)\s+(?:one|posting|role|job|opening)\b",
    re.I,
)
# QUEUE_JOB: stage a surfaced posting into the apply queue ("queue 5", "stage #5",
# "queue the stripe one"). Distinct from "apply" — staging just prepares the
# application package; it never logs an application.
_QUEUE_JOB_RE = re.compile(
    r"\b(?:queue|stage)\b\s*(?:up\s+)?#?(\d+)\b"
    r"|\b(?:queue|stage)\b\s+(?:up\s+)?the\s+(.+?)\s+(?:one|posting|role|job|opening)\b",
    re.I,
)

# PROFILE: set search criteria, or show the saved profile.
_PROFILE_SET_RE = re.compile(
    r"\b(looking for|i want|i'?m looking|interested in|search(?:ing)? for|"
    r"set (?:my )?profile|update (?:my )?profile|find me)\b",
    re.I,
)
_PROFILE_SHOW_RE = re.compile(
    r"\b(show|what'?s?|see|view)\b[^.?!]*\bprofile\b|\bmy profile\b|^\s*profile\s*\??$",
    re.I,
)


# TUNE: adjust how picky the matcher is. Encodes the request in `message`:
#   "set:<0..1>" (explicit), "loosen", "tighten", "all", or "reset".
def _parse_tune(low: str) -> ParsedMessage | None:
    """Detect a request to change the alert threshold; None if it isn't one."""
    pct = re.search(r"(\d{1,3})\s*%", low)
    talks_match = bool(re.search(
        r"\b(match|matches|matching|picky|selective|strict|threshold|bar|filter|"
        r"alerts?|jobs?|relevant|relevance)\b", low
    ))
    if re.search(r"\b(reset|default)\b", low) and talks_match:
        return ParsedMessage(intent=Intent.TUNE, message="reset", confidence=0.85)
    if re.search(r"\bshow me everything\b|\bno (filter|threshold)\b|\b(everything|all jobs)\b", low) and talks_match:
        return ParsedMessage(intent=Intent.TUNE, message="all", confidence=0.8)
    if pct and (talks_match or re.search(r"\b(only|at least|and up|or (higher|better|more)|\+)\b", low)):
        val = max(0.0, min(1.0, int(pct.group(1)) / 100))
        return ParsedMessage(intent=Intent.TUNE, message=f"set:{val}", confidence=0.85)
    if re.search(r"\b(less (picky|selective|strict)|lower the (bar|threshold)|"
                 r"show me more|more jobs|be less picky|loosen)\b", low):
        return ParsedMessage(intent=Intent.TUNE, message="loosen", confidence=0.8)
    if re.search(r"\b(more (picky|selective|strict)|stricter|raise the (bar|threshold)|"
                 r"only (the )?(best|top|high(est)?)|be more picky|tighten)\b", low):
        return ParsedMessage(intent=Intent.TUNE, message="tighten", confidence=0.8)
    return None


def _parse_track_company(low: str) -> str | None:
    """Pull the company out of 'track openings at stripe' / 'watch figma'."""
    s = _TRACK_RE.sub(" ", low)
    s = re.sub(
        r"\b(stop|watching|keep|eye|openings?|postings?|jobs?|roles?|new|at|for|"
        r"the|company|board|on|me|please)\b",
        " ", s, flags=re.I,
    )
    company, _ = _extract_company_role(s)
    return company

_TIME_RE = re.compile(
    r"\b(in \d+ \w+|today|tonight|tomorrow|next week|next month|\d+ days?|\d+ weeks?|"
    r"(?:this |next )?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b",
    re.I,
)

# Reverse the last committed action.
_UNDO_RE = re.compile(
    r"^\s*(undo|revert|roll ?back|take (that|it) back|put (that|it) back|"
    r"scratch that|nvm that last)\b",
    re.I,
)

# A past-looking time window for "what did I apply to this week" style queries.
_WINDOW_RE = re.compile(
    r"\b(today|yesterday|this week|last week|this month|last month|this year|"
    r"recently|lately|past \d+ days?|last \d+ days?|in the last \d+ days?|"
    r"since (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b",
    re.I,
)

# A question about what was logged in some recent window (history, not a new apply).
_RECENT_RE = re.compile(
    r"\b(what (?:did|have) i (?:appl|add|log|do)|"
    r"appl\w*\s+(?:this|last|past|recently|lately|today|yesterday)|"
    r"(?:this|last)\s+(?:week|month|year)\b.*\bappl|"
    r"what'?s new|anything new|what did i do)",
    re.I,
)

# Words that are never part of a company/role name.
_NOISE = {
    "i", "just", "applied", "apply", "to", "the", "a", "an", "for", "at",
    "update", "note", "remind", "me", "in", "got", "an", "received", "status",
    "reach", "out", "yeah", "yea", "yes", "that", "thing", "did", "please",
    "my", "of", "as", "is", "was", "now", "moved", "seemed", "was", "were",
    "from", "with", "about", "regarding", "re", "on", "ping",
    "recruiter", "contact", "outreach", "message",
    # question / command filler so "what's the status of X" → company is just X
    "what", "whats", "where", "when", "how", "who", "do", "does", "did", "have",
    "had", "tell", "latest", "status", "news", "happening", "stand", "there",
    "delete", "remove", "forget", "actually", "really", "get", "rid", "off",
    "this", "up", "going", "stuff", "thing",
    # leftover contraction fragments after apostrophe stripping (what's→what s)
    "s", "t", "ll", "ve", "d", "m",
    # edit / bulk filler
    "change", "fix", "correct", "rename", "should", "be", "wrong", "called",
    "every", "everything", "all", "each", "rest", "ones", "mark", "move", "set",
}

# Time-expression fragments that should never be read as a company/role.
_TIME_NOISE = re.compile(
    r"^(today|tomorrow|tonight|next|week|weeks|day|days|hour|hours|soon|\d+)$", re.I
)

# Common role keywords help us split "spotify swe ii" into company + role.
_ROLE_HINTS = re.compile(
    r"\b(swe|sde|engineer|engineering|developer|backend|frontend|fullstack|full[- ]stack|"
    r"data|ml|ai|pm|product|designer|design|analyst|scientist|intern|manager|ii|iii|"
    r"i{1,3}|l\d|sr|senior|junior|staff|principal|lead)\b",
    re.I,
)


def _clean_tokens(text: str) -> list[str]:
    text = re.sub(r"[^\w\s\-/]", " ", text)
    return [t for t in text.split() if t]


def _extract_company_role(text: str) -> tuple[str | None, str | None]:
    """Best-effort split of leftover text into (company, role).

    Heuristic: the first content token(s) before any role-hint word is the
    company; from the first role hint onward is the role.
    """
    tokens = _clean_tokens(text)
    content = [
        t for t in tokens
        if t.lower() not in _NOISE and not _TIME_NOISE.match(t)
    ]
    if not content:
        return None, None

    role_start = None
    for i, tok in enumerate(content):
        if _ROLE_HINTS.fullmatch(tok) or _ROLE_HINTS.match(tok):
            role_start = i
            break

    if role_start is None:
        # No role hint: treat only the first token as the company. Conservative,
        # but avoids swallowing trailing words ("recruiter seemed positive").
        return _titlecase_company(content[0]), None

    if role_start == 0:
        # Role words but no company before them.
        return None, _titlecase_role(" ".join(content)) or None

    company = " ".join(content[:role_start])
    role = " ".join(content[role_start:])
    return _titlecase_company(company), _titlecase_role(role) or None


def _titlecase_company(name: str) -> str:
    # Preserve all-caps acronyms (IBM, AWS); title-case normal words.
    parts = []
    for w in name.split():
        parts.append(w if w.isupper() and len(w) <= 4 else w.capitalize())
    return " ".join(parts)


# Role tokens that should render uppercase rather than title-cased.
_ROLE_ACRONYMS = {
    "swe", "sde", "ml", "ai", "pm", "ii", "iii", "iv", "ux", "ui", "qa",
}


def _titlecase_role(role: str) -> str:
    out = []
    for w in role.split():
        lw = w.lower()
        if lw in _ROLE_ACRONYMS or re.fullmatch(r"l\d|i{1,3}", lw):
            out.append(w.upper())
        else:
            out.append(w.capitalize())
    return " ".join(out).strip()


def _company_from(text: str) -> str | None:
    return _extract_company_role(text)[0]


def _parse_edit(low: str) -> ParsedMessage:
    """Correction to a stored entry: rename, role fix, or applied-date fix.

    A "change X to <status>" where the target is a real stage is actually an
    UPDATE, so we redirect those to keep stage changes in one place.
    """
    # 1) rename A to B  → company=A (locator), message=B (new name)
    m = re.search(r"\brename\s+(.+?)\s+to\s+(.+)$", low)
    if m:
        return ParsedMessage(
            intent=Intent.EDIT, company=_company_from(m.group(1)),
            message=_titlecase_company(m.group(2).strip()),
            confidence=0.85,
        )
    # 2) "<A> is actually (a|an) <B> [role]"  → role fix (unless B is a stage)
    m = re.search(r"^(.*?)\bis actually\b\s*(?:an? )?(.+?)(?:\s+role)?$", low)
    if m:
        company = _company_from(m.group(1))
        val = m.group(2).strip()
        status = normalize_status(val) if any(p.search(val) for p, _ in _STATUS_PATTERNS) else None
        if status:
            return ParsedMessage(intent=Intent.UPDATE, company=company, status=status, confidence=0.8)
        return ParsedMessage(intent=Intent.EDIT, company=company,
                             role=_titlecase_role(val) or None, confidence=0.8)
    # 3) explicit role correction: "...role ... (to|is|should be) <B>"
    m = re.search(r"\brole\b.*?\b(?:to|is|should be|:)\s+(.+)$", low)
    if m:
        head = low[: m.start()]
        return ParsedMessage(
            intent=Intent.EDIT, company=_company_from(head),
            role=_titlecase_role(m.group(1).strip()) or None, confidence=0.78,
        )
    # 4) generic "change/fix/correct <A> to <B>" — B may be a stage (→ UPDATE)
    m = re.search(r"\b(?:change|fix|correct)\s+(.+?)\s+to\s+(.+)$", low)
    if m:
        company = _company_from(m.group(1))
        val = m.group(2).strip()
        if any(p.search(val) for p, _ in _STATUS_PATTERNS):
            return ParsedMessage(intent=Intent.UPDATE, company=company,
                                 status=normalize_status(val), confidence=0.78)
        return ParsedMessage(intent=Intent.EDIT, company=company,
                             role=_titlecase_role(val) or None, confidence=0.7)
    return ParsedMessage(intent=Intent.EDIT, company=_company_from(low), confidence=0.5)


_DATEISH = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|"
    r"\d{1,2}[/-]\d{1,2}|yesterday|today|last \w+|\d+ days? ago|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.I,
)


def parse_edit_change(text: str) -> tuple[str | None, str | None, str | None]:
    """Interpret a follow-up reply to "what should I change about X?".

    Returns (new_role, new_name, new_date_phrase). Used when EDIT collected the
    company first and is now waiting on *what* to change.
    """
    raw = text.strip()
    low = raw.lower()
    # Rename: "call it X" / "name is X" / "rename to X".
    m = re.search(r"\b(?:call it|name (?:is|to|should be)|rename(?: to)?)\s+(.+)$", low)
    if m:
        return None, _titlecase_company(m.group(1).strip()), None
    # Explicit role correction.
    m = re.search(r"\brole\b\s*(?:to|is|should be|:)?\s*(.+)$", low)
    if m:
        return _titlecase_role(m.group(1).strip()) or None, None, None
    # Applied-date correction.
    m = re.search(r"\b(?:applied|apply|date)\b\s*(?:on|was|:)?\s*(.+)$", low)
    if m and _DATEISH.search(m.group(1)):
        return None, None, m.group(1).strip()
    if _DATEISH.search(low):
        return None, None, raw
    # Bare value → treat as the new role (the most common correction).
    return _titlecase_role(raw) or None, None, None


def _bulk_age(low: str) -> str | None:
    m = re.search(r"older than\s+(\d+\s*(?:day|week|month)s?)", low)
    if m:
        return m.group(1)
    m = re.search(r"(?:haven'?t heard|no (?:reply|response)).*?(\d+\s*(?:day|week|month)s?)", low)
    if m:
        return m.group(1)
    m = re.search(r"\b(?:last|past)\s+(month|week)\b", low)
    if m:
        return f"1 {m.group(1)}"
    m = re.search(r"\b(\d+\s*(?:day|week|month)s?)\b", low)
    return m.group(1) if m else None


def _parse_bulk(low: str) -> ParsedMessage:
    """A mass stage change with an optional current-stage filter and age filter.

    Convention shared with the LLM path: status=new stage to apply,
    message=current-stage filter (or None for all open), time_reference=age.
    """
    stages = "applied|oa received|oa|phone screen|interview|onsite|offer"
    filt = None
    m = re.search(rf"\b(?:still in|stuck (?:in|at)|sitting in|that are in|in|from)\s+({stages})\b", low)
    if not m:
        m = re.search(rf"\b({stages})\s+(?:ones|apps|applications)\b", low)
    if m:
        filt = normalize_status(m.group(1))

    new = None
    # "... as <status>" names the target; the stage before it is the filter.
    a = re.search(r"\bas\s+([a-z ]+?)\s*$", low)
    if a and any(p.search(a.group(1)) for p, _ in _STATUS_PATTERNS):
        new = normalize_status(a.group(1))
        if filt is None:
            fm = re.search(rf"\b({stages})\b", low[: a.start()])
            if fm:
                filt = normalize_status(fm.group(1))
    if not new:
        rest = (low[: m.start()] + " " + low[m.end():]) if m else low
        if any(p.search(rest) for p, _ in _STATUS_PATTERNS):
            new = normalize_status(rest)
    return ParsedMessage(
        intent=Intent.BULK, status=new, message=filt,
        time_reference=_bulk_age(low),
        confidence=0.8 if new else 0.5,
    )


class HeuristicRouter:
    name = "heuristic"

    def parse_actions(self, text: str) -> list[ParsedMessage]:
        # The offline heuristic doesn't split combined messages; multi-action is
        # an LLM-only capability. Always one action here.
        return [self.parse(text)]

    def parse(self, text: str) -> ParsedMessage:
        raw = text.strip()
        low = raw.lower()
        time_ref = (_TIME_RE.search(low) or [None])
        time_ref = time_ref.group(0) if hasattr(time_ref, "group") else None

        # Intent detection (order matters: explicit verbs win).
        if _UNDO_RE.search(low):
            return ParsedMessage(intent=Intent.UNDO, confidence=0.9)

        # A history question ("what did I apply to this week") must be caught
        # before APPLY/CHECK, both of which would otherwise grab the word "apply".
        if _RECENT_RE.search(low):
            w = _WINDOW_RE.search(low)
            window = w.group(0) if w else "recently"
            return ParsedMessage(
                intent=Intent.LIST, time_reference=window, confidence=0.85
            )

        # --- Job discovery (before APPLY/QUERY/LIST, which share keywords) ----
        # APPLY_JOB first: "apply 2" must beat the generic APPLY ("applied …").
        m = _APPLY_JOB_RE.search(low)
        if m:
            pid = m.group(1) or m.group(2)
            if pid:
                return ParsedMessage(
                    intent=Intent.APPLY_JOB, message=str(int(pid)), confidence=0.9
                )
            company = _company_from(m.group(3))
            return ParsedMessage(
                intent=Intent.APPLY_JOB, company=company,
                confidence=0.85 if company else 0.5,
            )

        m = _DISMISS_JOB_RE.search(low)
        if m:
            pid = m.group(1)
            if pid:
                return ParsedMessage(intent=Intent.DISMISS_JOB, message=str(int(pid)),
                                     confidence=0.9)
            return ParsedMessage(intent=Intent.DISMISS_JOB,
                                 company=_company_from(m.group(2)), confidence=0.8)

        m = _SNOOZE_JOB_RE.search(low)
        if m:
            pid = m.group(1)
            company = None if pid else _company_from(m.group(2))
            return ParsedMessage(
                intent=Intent.SNOOZE_JOB,
                message=str(int(pid)) if pid else None,
                company=company, time_reference=time_ref,
                confidence=0.9 if pid else 0.8,
            )

        m = _QUEUE_JOB_RE.search(low)
        if m:
            pid = m.group(1)
            if pid:
                return ParsedMessage(intent=Intent.QUEUE_JOB, message=str(int(pid)),
                                     confidence=0.9)
            return ParsedMessage(intent=Intent.QUEUE_JOB,
                                 company=_company_from(m.group(2)), confidence=0.8)

        tuned = _parse_tune(low)
        if tuned is not None:
            return tuned

        if _TRACK_RE.search(low):
            if _TRACK_LIST_RE.search(low):
                return ParsedMessage(intent=Intent.TRACK, message="list", confidence=0.85)
            feed_m = _TRACK_FEED_RE.search(low)
            if feed_m:
                return ParsedMessage(
                    intent=Intent.TRACK,
                    company=feed_m.group(1).lower(),
                    message="feed",
                    confidence=0.9,
                )
            company = _parse_track_company(low)
            remove = bool(_TRACK_REMOVE_RE.search(low))
            return ParsedMessage(
                intent=Intent.TRACK, company=company,
                message="remove" if remove else None,
                confidence=0.85 if company else 0.5,
            )

        if _PROFILE_SET_RE.search(low):
            return ParsedMessage(intent=Intent.PROFILE, message=raw.strip(), confidence=0.8)
        if _PROFILE_SHOW_RE.search(low):
            return ParsedMessage(intent=Intent.PROFILE, message=None, confidence=0.8)

        if _JOBS_REVIEW_RE.search(low) and not _APPLY_RE.search(low):
            return ParsedMessage(intent=Intent.JOBS_REVIEW, confidence=0.9)

        if _JOBS_RE.search(low) and not _APPLY_RE.search(low):
            return ParsedMessage(intent=Intent.JOBS, confidence=0.85)

        if _NOTE_RE.search(low):
            body = re.sub(r"^\s*note\b", "", raw, flags=re.I).strip()
            company, _ = _extract_company_role(body)
            # The note text is the body minus the leading company mention, so
            # "note figma" yields an empty note (the engine then asks for one).
            note_text = body
            if company:
                note_text = re.sub(
                    rf"^\s*{re.escape(company)}\b[:,]?\s*", "", body, flags=re.I
                ).strip()
            return ParsedMessage(
                intent=Intent.NOTE, company=company, message=note_text or None,
                time_reference=time_ref, confidence=0.7 if company else 0.45,
            )

        if _REMIND_RE.search(low):
            company, _ = _extract_company_role(low)
            return ParsedMessage(
                intent=Intent.REMIND, company=company, time_reference=time_ref,
                confidence=0.75 if (company and time_ref) else 0.5,
            )

        if _OUTREACH_RE.search(low) and not _APPLY_RE.search(low):
            company, _ = _extract_company_role(low)
            return ParsedMessage(
                intent=Intent.OUTREACH, company=company,
                confidence=0.7 if company else 0.45,
            )

        if _DELETE_RE.search(low):
            stripped = _DELETE_RE.sub(" ", low)
            company, role = _extract_company_role(stripped)
            return ParsedMessage(
                intent=Intent.DELETE, company=company, role=role,
                confidence=0.85 if company else 0.5,
            )

        if _BULK_QUANT.search(low) and _BULK_VERB.search(low):
            return _parse_bulk(low)

        if _EDIT_RE.search(low):
            return _parse_edit(low)

        if _CHECK_RE.search(low):
            stripped = _CHECK_RE.sub(" ", low)
            company, role = _extract_company_role(stripped)
            return ParsedMessage(
                intent=Intent.CHECK, company=company, role=role,
                confidence=0.85 if company else 0.6,
            )

        if _DEADLINE_RE.search(low):
            # A deadline *set* needs a date; without one, this is a request to
            # see upcoming deadlines (no company → engine lists the calendar).
            if time_ref:
                has_status = any(p.search(low) for p, _ in _STATUS_PATTERNS)
                status = normalize_status(low) if has_status else None
                stripped = _DEADLINE_RE.sub(" ", low)
                stripped = re.sub(r"\bby\b", " ", stripped, flags=re.I)
                stripped = stripped.replace(time_ref, " ")
                company, _ = _extract_company_role(stripped)
                return ParsedMessage(
                    intent=Intent.DEADLINE, company=company, status=status,
                    time_reference=time_ref,
                    confidence=0.8 if company else 0.55,
                )
            return ParsedMessage(intent=Intent.DEADLINE, confidence=0.8)

        if _STATS_RE.search(low):
            return ParsedMessage(intent=Intent.STATS, confidence=0.85)

        if _QUERY_RE.search(low):
            return ParsedMessage(intent=Intent.QUERY, confidence=0.85)

        if _LIST_RE.search(low):
            status = normalize_status(low) if any(
                p.search(low) for p, _ in _STATUS_PATTERNS
            ) else None
            return ParsedMessage(intent=Intent.LIST, status=status, confidence=0.8)

        if _APPLY_RE.search(low):
            company, role = _extract_company_role(low)
            conf = 0.9 if company else 0.3
            return ParsedMessage(
                intent=Intent.APPLY, company=company, role=role,
                time_reference=time_ref, confidence=conf,
            )

        # UPDATE: a recognizable status word present.
        has_status = any(p.search(low) for p, _ in _STATUS_PATTERNS)
        if has_status or _UPDATE_RE.search(low):
            status = normalize_status(low)
            # strip the word "update" and status phrasing to find the company
            stripped = re.sub(r"\bupdate\b", "", low, flags=re.I)
            company, role = _extract_company_role(stripped)
            if has_status and company:
                conf = 0.85
            elif company:
                conf = 0.55  # company but no clear target status
                status = None if not has_status else status
            else:
                conf = 0.3
            return ParsedMessage(
                intent=Intent.UPDATE, company=company, role=role,
                status=status if has_status else None,
                time_reference=time_ref, confidence=conf,
            )

        # Bare affirmations like "yes" / "yea applied to that" handled by engine
        # via context; mark low-confidence APPLY if "applied"-ish, else UNKNOWN.
        company, role = _extract_company_role(low)
        if company:
            return ParsedMessage(
                intent=Intent.UNKNOWN, company=company, role=role, confidence=0.35
            )
        return ParsedMessage(intent=Intent.UNKNOWN, confidence=0.1)


# ---------------------------------------------------------------------------
# Anthropic (Claude) backend
# ---------------------------------------------------------------------------

# Few-shot examples live *inside* the system prompt (not as separate message
# turns) so the entire instruction block is one static, cacheable prefix. Only
# the single inbound SMS varies per request — the most token-efficient packaging.
#
# Each example maps an input to a LIST of action objects. Most messages produce
# exactly one; a combined message ("X and also Y") produces several.
_FEWSHOTS = [
    ("applied spotify swe ii",
     [{"intent": "APPLY", "company": "Spotify", "role": "SWE II", "status": None,
       "message": None, "time_reference": None, "confidence": 0.95}]),
    ("i just applied to figma backend engineer",
     [{"intent": "APPLY", "company": "Figma", "role": "Backend Engineer", "status": None,
       "message": None, "time_reference": None, "confidence": 0.95}]),
    ("spotify update oa received",
     [{"intent": "UPDATE", "company": "Spotify", "role": None, "status": "OA received",
       "message": None, "time_reference": None, "confidence": 0.93}]),
    ("note spotify recruiter seemed positive",
     [{"intent": "NOTE", "company": "Spotify", "role": None, "status": None,
       "message": "recruiter seemed positive", "time_reference": None, "confidence": 0.9}]),
    ("remind me about spotify in 3 days",
     [{"intent": "REMIND", "company": "Spotify", "role": None, "status": None,
       "message": None, "time_reference": "in 3 days", "confidence": 0.92}]),
    ("what should I follow up on",
     [{"intent": "QUERY", "company": None, "role": None, "status": None,
       "message": None, "time_reference": None, "confidence": 0.9}]),
    ("how am I doing",
     [{"intent": "STATS", "company": None, "role": None, "status": None,
       "message": None, "time_reference": None, "confidence": 0.9}]),
    ("what did I apply to this week",
     [{"intent": "LIST", "company": None, "role": None, "status": None,
       "message": None, "time_reference": "this week", "confidence": 0.9}]),
    ("anything new since monday",
     [{"intent": "LIST", "company": None, "role": None, "status": None,
       "message": None, "time_reference": "since monday", "confidence": 0.82}]),
    ("track openings at stripe",
     [{"intent": "TRACK", "company": "Stripe", "role": None, "status": None,
       "message": None, "time_reference": None, "confidence": 0.9}]),
    ("stop tracking databricks",
     [{"intent": "TRACK", "company": "Databricks", "role": None, "status": None,
       "message": "remove", "time_reference": None, "confidence": 0.9}]),
    ("what am i tracking",
     [{"intent": "TRACK", "company": None, "role": None, "status": None,
       "message": "list", "time_reference": None, "confidence": 0.88}]),
    ("any new jobs",
     [{"intent": "JOBS", "company": None, "role": None, "status": None,
       "message": None, "time_reference": None, "confidence": 0.88}]),
    ("i'm looking for new grad swe roles, remote or nyc",
     [{"intent": "PROFILE", "company": None, "role": None, "status": None,
       "message": "new grad swe roles, remote or nyc", "time_reference": None,
       "confidence": 0.9}]),
    ("show my profile",
     [{"intent": "PROFILE", "company": None, "role": None, "status": None,
       "message": None, "time_reference": None, "confidence": 0.85}]),
    ("apply 2",
     [{"intent": "APPLY_JOB", "company": None, "role": None, "status": None,
       "message": "2", "time_reference": None, "confidence": 0.92}]),
    ("apply to #5",
     [{"intent": "APPLY_JOB", "company": None, "role": None, "status": None,
       "message": "5", "time_reference": None, "confidence": 0.92}]),
    ("apply to the stripe one",
     [{"intent": "APPLY_JOB", "company": "Stripe", "role": None, "status": None,
       "message": None, "time_reference": None, "confidence": 0.85}]),
    ("dismiss 3",
     [{"intent": "DISMISS_JOB", "company": None, "role": None, "status": None,
       "message": "3", "time_reference": None, "confidence": 0.9}]),
    ("not interested in #4",
     [{"intent": "DISMISS_JOB", "company": None, "role": None, "status": None,
       "message": "4", "time_reference": None, "confidence": 0.88}]),
    ("snooze 5 for a week",
     [{"intent": "SNOOZE_JOB", "company": None, "role": None, "status": None,
       "message": "5", "time_reference": "a week", "confidence": 0.9}]),
    ("only show me 80%+ matches",
     [{"intent": "TUNE", "company": None, "role": None, "status": None,
       "message": "set:0.8", "time_reference": None, "confidence": 0.88}]),
    ("be less picky about job matches",
     [{"intent": "TUNE", "company": None, "role": None, "status": None,
       "message": "loosen", "time_reference": None, "confidence": 0.82}]),
    ("reset my match threshold",
     [{"intent": "TUNE", "company": None, "role": None, "status": None,
       "message": "reset", "time_reference": None, "confidence": 0.85}]),
    ("undo that",
     [{"intent": "UNDO", "company": None, "role": None, "status": None,
       "message": None, "time_reference": None, "confidence": 0.95}]),
    ("actually revert my last change",
     [{"intent": "UNDO", "company": None, "role": None, "status": None,
       "message": None, "time_reference": None, "confidence": 0.9}]),
    ("stripe oa due friday",
     [{"intent": "DEADLINE", "company": "Stripe", "role": None, "status": "OA received",
       "message": None, "time_reference": "friday", "confidence": 0.9}]),
    ("what's coming up this week",
     [{"intent": "DEADLINE", "company": None, "role": None, "status": None,
       "message": None, "time_reference": None, "confidence": 0.85}]),
    ("what's the status of stripe",
     [{"intent": "CHECK", "company": "Stripe", "role": None, "status": None,
       "message": None, "time_reference": None, "confidence": 0.9}]),
    ("where do I stand with google",
     [{"intent": "CHECK", "company": "Google", "role": None, "status": None,
       "message": None, "time_reference": None, "confidence": 0.88}]),
    ("actually I never applied to ramp, delete it",
     [{"intent": "DELETE", "company": "Ramp", "role": None, "status": None,
       "message": None, "time_reference": None, "confidence": 0.9}]),
    ("the stripe role is actually backend engineer",
     [{"intent": "EDIT", "company": "Stripe", "role": "Backend Engineer", "status": None,
       "message": None, "time_reference": None, "confidence": 0.9}]),
    ("rename databricks to Databricks Inc",
     [{"intent": "EDIT", "company": "Databricks", "role": None, "status": None,
       "message": "Databricks Inc", "time_reference": None, "confidence": 0.88}]),
    ("reject everything still in applied from over a month ago",
     [{"intent": "BULK", "company": None, "role": None, "status": "Rejected",
       "message": "Applied", "time_reference": "1 month", "confidence": 0.88}]),
    ("ghost anything I haven't heard from in 30 days",
     [{"intent": "BULK", "company": None, "role": None, "status": "Ghosted",
       "message": None, "time_reference": "30 days", "confidence": 0.85}]),
    ("reach out to a recruiter at stripe",
     [{"intent": "OUTREACH", "company": "Stripe", "role": None, "status": None,
       "message": None, "time_reference": None, "confidence": 0.9}]),
    ("applied",
     [{"intent": "APPLY", "company": None, "role": None, "status": None,
       "message": None, "time_reference": None, "confidence": 0.3}]),
    ("spotify update",
     [{"intent": "UPDATE", "company": "Spotify", "role": None, "status": None,
       "message": None, "time_reference": None, "confidence": 0.5}]),
    # Typos + casual spelling — normalize obvious company names.
    ("applid to databrikcs swe intern",
     [{"intent": "APPLY", "company": "Databricks", "role": "SWE Intern", "status": None,
       "message": None, "time_reference": None, "confidence": 0.9}]),
    # Multi-word company + role.
    ("submitted my app to two sigma for a quant researcher position",
     [{"intent": "APPLY", "company": "Two Sigma", "role": "Quant Researcher",
       "status": None, "message": None, "time_reference": None, "confidence": 0.93}]),
    # Multi-word company, expand a partial name; "final round" == Onsite stage.
    ("heard back from goldman, moving to the final round",
     [{"intent": "UPDATE", "company": "Goldman Sachs", "role": None, "status": "Onsite",
       "message": None, "time_reference": None, "confidence": 0.9}]),
    # Combined message: emit ONE action per distinct request, in the order said.
    ("got the oa from stripe and also applied to ramp last night",
     [{"intent": "UPDATE", "company": "Stripe", "role": None, "status": "OA received",
       "message": None, "time_reference": None, "confidence": 0.9},
      {"intent": "APPLY", "company": "Ramp", "role": None, "status": None,
       "message": None, "time_reference": "last night", "confidence": 0.9}]),
    # Combined: two applications in one message.
    ("applied to notion and also airtable, both pm roles",
     [{"intent": "APPLY", "company": "Notion", "role": "PM", "status": None,
       "message": None, "time_reference": None, "confidence": 0.9},
      {"intent": "APPLY", "company": "Airtable", "role": "PM", "status": None,
       "message": None, "time_reference": None, "confidence": 0.9}]),
    # Recruiter wanting to talk/call is the Phone screen stage (not a reminder).
    ("the airbnb recruiter wants to hop on a call thursday",
     [{"intent": "UPDATE", "company": "Airbnb", "role": None, "status": "Phone screen",
       "message": None, "time_reference": None, "confidence": 0.85}]),
    # Rejection with emoji / casual tone.
    ("mongodb rejected me :(",
     [{"intent": "UPDATE", "company": "MongoDB", "role": None, "status": "Rejected",
       "message": None, "time_reference": None, "confidence": 0.93}]),
    # Offer.
    ("stripe just gave me an offer!!",
     [{"intent": "UPDATE", "company": "Stripe", "role": None, "status": "Offer",
       "message": None, "time_reference": None, "confidence": 0.95}]),
]


def _build_system_prompt() -> str:
    examples = "\n".join(
        f'  IN: {t!r}\n  OUT: {json.dumps({"actions": acts}, separators=(",", ":"))}'
        for t, acts in _FEWSHOTS
    )
    return (
        "You are the intent router for a personal job-search SMS assistant.\n"
        "Convert one inbound SMS into a JSON object {\"actions\": [...]}, where "
        "each element is one structured action. Most messages contain exactly "
        "one action. Be decisive: it is better to infer and store than to "
        "discard the user's message.\n\n"
        "Intents (per action):\n"
        "- APPLY: user applied to a job. Extract company and role.\n"
        "- UPDATE: a stage change. Put the new stage in `status`. Canonical "
        "stages: Applied, OA received, Phone screen, Interview, Onsite, Offer, "
        "Rejected, Ghosted.\n"
        "- NOTE: a freeform note about a company; put the note text in `message`.\n"
        "- REMIND: schedule a follow-up; put timing in `time_reference`.\n"
        "- LIST: user wants to see applications. If they scope it to a time "
        "window ('what did I apply to this week', 'anything new since monday'), "
        "put the window phrase in `time_reference` (e.g. 'this week', 'today', "
        "'last month', 'since monday').\n"
        "- QUERY: user asks what to follow up on / what's overdue.\n"
        "- STATS: user asks how they're doing overall / pipeline summary / "
        "their numbers (not about one company).\n"
        "- DEADLINE: a dated event for a company (OA due, interview, onsite on a "
        "date) — put the date in `time_reference` and the kind in `status` when "
        "it maps to a stage. With no company/date it's a request to see upcoming "
        "deadlines.\n"
        "- CHECK: user asks about the state of ONE specific application (status, "
        "latest, when applied, 'do I have X'). Set `company`; do not change anything.\n"
        "- DELETE: user wants to remove an application ('delete X', 'I didn't "
        "actually apply to X'). Set `company`.\n"
        "- EDIT: correct a stored entry's ROLE, NAME, or APPLIED DATE (not its "
        "stage). `company`=which app; `role`=new role; for a rename put the new "
        "name in `message`; `time_reference`=corrected applied date. A stage "
        "change is UPDATE, not EDIT.\n"
        "- BULK: a mass stage change over many apps ('reject everything still in "
        "Applied', 'ghost anything older than 30 days'). `status`=new stage to "
        "apply; `message`=current-stage filter (or null for all open); "
        "`time_reference`=age filter (e.g. '30 days', 'last month').\n"
        "- UNDO: user wants to reverse their last action ('undo', 'undo that', "
        "'revert my last change', 'put it back'). No entities.\n"
        "- OUTREACH: user wants recruiter contact / a drafted message.\n"
        "- TRACK: user wants to start/stop watching a company's job board for new "
        "openings, or see what they're tracking. Set `company`; for removal put "
        "'remove' in `message`; for 'what am I tracking' put 'list' in `message`.\n"
        "- JOBS: user wants to browse the new job postings the assistant has found "
        "(not their applications). No entities.\n"
        "- JOBS_REVIEW: user wants to walk through queued job matches one at a time "
        "('review jobs', 'let's go through them', 'go through the queue'). No entities.\n"
        "- PROFILE: user states what roles/locations they're after (set) or asks "
        "to see their profile (show). For a set, put the full criteria in `message`; "
        "for a show, leave `message` null.\n"
        "- APPLY_JOB: user wants to apply to a posting the assistant surfaced (job "
        "alerts print a '#<id>'). For a numeric reference ('apply 2', 'apply to "
        "#5') put just the number in `message`. For a company reference ('apply to "
        "the stripe one') set `company` and leave `message` null. NOTE: past-tense "
        "'applied to X' is APPLY (logging a job they found elsewhere), not APPLY_JOB.\n"
        "- DISMISS_JOB: user wants to permanently hide a surfaced posting ('dismiss "
        "3', 'not interested in #4', 'hide the stripe one'). Put the number in "
        "`message`, or set `company` for a company reference.\n"
        "- SNOOZE_JOB: user wants to mute a posting for a while ('snooze 5', 'snooze "
        "5 for a week'). Put the number in `message` and any duration in "
        "`time_reference`.\n"
        "- TUNE: user wants to change how strict job matching/alerting is. Put one "
        "of these in `message`: 'set:<0..1>' for an explicit threshold ('only 80%+ "
        "matches' -> 'set:0.8'), 'loosen' (less picky / show more), 'tighten' (more "
        "picky / only the best), 'all' (show everything), or 'reset' (back to "
        "default).\n"
        "- UNKNOWN: none of the above.\n\n"
        "Rules:\n"
        "- `confidence` is 0.0-1.0: your certainty about the intent + entities.\n"
        "- If the company is omitted but implied by prior context, leave it null "
        "and lower the confidence so the assistant resolves it from memory.\n"
        "- Never invent a company or role that isn't present or clearly implied.\n"
        "- Tolerate typos and casual spelling; normalize obvious company names "
        "(e.g. 'databrikcs' -> 'Databricks') and expand well-known partial names "
        "(e.g. 'goldman' -> 'Goldman Sachs') only when unambiguous.\n"
        "- Stage mapping: 'final round' / 'super day' is Onsite. A recruiter "
        "wanting to chat/call is Phone screen.\n"
        "- If one SMS contains several distinct requests (e.g. 'applied to X and "
        "got an OA from Y'), emit one action per request, in the order stated. "
        "Do NOT split a single request into multiple actions.\n\n"
        "Examples:\n" + examples
    )


# One action's schema. Structured outputs guarantee valid JSON (no retry waste).
_ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": [i.value for i in Intent],
        },
        "company": {"type": ["string", "null"]},
        "role": {"type": ["string", "null"]},
        "status": {"type": ["string", "null"]},
        "message": {"type": ["string", "null"]},
        "time_reference": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
    },
    "required": [
        "intent", "company", "role", "status",
        "message", "time_reference", "confidence",
    ],
    "additionalProperties": False,
}

# The response wraps one or more actions in an array.
_SCHEMA = {
    "type": "object",
    "properties": {"actions": {"type": "array", "items": _ACTION_SCHEMA}},
    "required": ["actions"],
    "additionalProperties": False,
}

# Safety cap on actions per message.
_MAX_ACTIONS = 4


class AnthropicRouter:
    """Claude-backed router. Defaults to Haiku 4.5 (cheapest capable model).

    Token-efficiency measures:
      * static prompt + few-shots in a cached `system` block (prefix caching)
      * only the SMS varies per request; `max_tokens` capped at 256
      * structured outputs guarantee valid JSON (no retry waste)
      * inbound SMS length-capped before sending
      * a token-bucket rate limit; over-limit calls fall back to the heuristic
        router rather than erroring
    """

    name = "anthropic"

    def __init__(self) -> None:
        import anthropic  # lazy import so the offline path needs no install

        settings = get_settings()
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self._model = settings.anthropic_model
        self._max_chars = settings.llm_max_sms_chars
        # Cache the static instruction block. cache_control only *engages* once
        # the prefix exceeds the model's minimum (~4096 tokens for Haiku); below
        # that it's a harmless no-op. Marked anyway so it caches for free if the
        # prompt grows.
        self._system = [{
            "type": "text",
            "text": _build_system_prompt(),
            "cache_control": {"type": "ephemeral"},
        }]
        self._fallback = HeuristicRouter()
        self._limiter = TokenBucket(settings.llm_rate_limit_per_min)
        # Lightweight usage accounting, surfaced via /health.
        self.usage = {
            "calls": 0, "fallbacks": 0,
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
        }

    def parse(self, text: str) -> ParsedMessage:
        """The single most-likely action (for the slot-filling / repair path)."""
        return self.parse_actions(text)[0]

    def parse_actions(self, text: str) -> list[ParsedMessage]:
        """One or more actions; a combined SMS yields several."""
        if not self._limiter.allow():
            self.usage["fallbacks"] += 1
            return self._fallback.parse_actions(text)

        try:
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=512,  # room for a few actions; output is still small
                system=self._system,
                messages=[{"role": "user", "content": text.strip()[: self._max_chars]}],
                output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            )
        except Exception:
            # Rate limit, network, auth, bad request — never block the user.
            self.usage["fallbacks"] += 1
            return self._fallback.parse_actions(text)

        u = resp.usage
        self.usage["calls"] += 1
        self.usage["input_tokens"] += getattr(u, "input_tokens", 0) or 0
        self.usage["output_tokens"] += getattr(u, "output_tokens", 0) or 0
        self.usage["cache_read_tokens"] += getattr(u, "cache_read_input_tokens", 0) or 0
        self.usage["cache_write_tokens"] += getattr(u, "cache_creation_input_tokens", 0) or 0

        try:
            payload = next(b.text for b in resp.content if b.type == "text")
            actions = json.loads(payload).get("actions") or []
            out: list[ParsedMessage] = []
            for a in actions[:_MAX_ACTIONS]:
                a["status"] = normalize_status(a.get("status"))
                out.append(ParsedMessage(**a))
            return out or [ParsedMessage(intent=Intent.UNKNOWN, confidence=0.1)]
        except (StopIteration, json.JSONDecodeError, TypeError, ValueError, KeyError):
            self.usage["fallbacks"] += 1
            return self._fallback.parse_actions(text)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_router_singleton = None


def get_router():
    global _router_singleton
    if _router_singleton is not None:
        return _router_singleton
    if get_settings().use_llm_router:
        try:
            _router_singleton = AnthropicRouter()
        except Exception:  # missing package / bad key -> graceful offline fallback
            _router_singleton = HeuristicRouter()
    else:
        _router_singleton = HeuristicRouter()
    return _router_singleton
