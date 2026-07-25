"""Why a posting was recommended — in words, not just a percentage.

A match score answers "how much?" but never "why?", so a 78% and a 79% look
identical and a bad recommendation is impossible to argue with. This turns the
signals the pipeline already computed into a short, honest line:

    "87% · matches 'backend engineer' · Python, Go · remote · apply direct"

Deliberately **heuristic and free**: every reason is derived from the profile and
the posting, so it costs nothing, can't hallucinate a reason, and works with no
API key. Where a cached LLM summary already exists (``insights``), its verdict is
folded in — but it's never fetched on purpose just to explain a card.

The honesty rule: reasons must be checkable against the posting. We say "mentions
Python" only when the description does, and we surface *concerns* too — a stale
posting or a seniority gap is exactly what the user needs to see before spending
an application on it.
"""
from __future__ import annotations

import re

_WORD = re.compile(r"[a-z0-9+#.]+")


def _terms(raw: str | None) -> list[str]:
    """Split a profile field ("python, go; rust") into comparable terms."""
    if not raw:
        return []
    return [t.strip().lower() for t in re.split(r"[,;/|\n]+", raw) if t.strip()]


def _get(obj, key, default=None):
    """One accessor for both shapes a posting arrives in: a ``JobPosting``
    dataclass (fresh from a source) and a ``sqlite3.Row`` (read back from the
    store). Profiles are Rows too, and neither Row nor dataclass has ``.get()``."""
    if obj is None:
        return default
    if hasattr(obj, key):
        return getattr(obj, key, default)
    try:
        value = obj[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _haystack(posting) -> str:
    return " ".join(str(_get(posting, k, "") or "")
                    for k in ("title", "description", "company")).lower()


def explain(posting, profile=None, *, summary: dict | None = None,
            score: float | None = None) -> dict:
    """``{score, reasons, concerns, line}`` for one posting.

    ``reasons`` are why it surfaced; ``concerns`` are what to check before
    applying. ``line`` is the one-liner for an alert card. Pass ``score`` when the
    caller holds it separately (a fresh ``JobPosting`` has no stored score).
    """
    text = _haystack(posting)
    title = str(_get(posting, "title", "") or "").lower()
    reasons: list[str] = []
    concerns: list[str] = []

    score = score if score is not None else (_get(posting, "relevance_score") or 0.0)

    # 1. The role itself — the profile's target roles appearing in the title is the
    #    strongest, most checkable reason there is.
    roles = _terms(_get(profile, "roles")) if profile is not None else []
    hit_role = next((r for r in roles if r and r in title), None)
    if hit_role:
        reasons.append(f"matches “{hit_role}”")
    elif roles:
        concerns.append("title doesn't match your target roles")

    # 2. Skills you asked for that the posting actually names.
    keywords = _terms(_get(profile, "keywords")) if profile is not None else []
    hits = [k for k in keywords if k and _mentions(text, k)]
    if hits:
        reasons.append(", ".join(hits[:4]))

    # 3. Location / remote.
    location = (_get(posting, "location") or "").lower()
    if "remote" in location or "remote" in title:
        reasons.append("remote")
    else:
        wanted = _terms(_get(profile, "locations")) if profile is not None else []
        hit_loc = next((w for w in wanted if w and w in location), None)
        if hit_loc:
            reasons.append(f"in {hit_loc}")
        elif wanted and location:
            concerns.append(f"location is {location}")

    # 4. Applying directly on the company's own ATS beats an aggregator redirect.
    if (_get(posting, "source") or "").lower() in ("greenhouse", "lever", "ashby"):
        reasons.append("apply direct")

    # 5. A cached LLM read, if one already exists — never fetched just for this.
    if summary:
        stretch = summary.get("stretch")
        if stretch:
            concerns.append(f"stretch: {stretch}" if isinstance(stretch, str)
                            else "a stretch on seniority")
        tech = summary.get("tech_overlap")
        if isinstance(tech, str) and tech and not hits:
            reasons.append(tech[:60])

    return {
        "score": score,
        "reasons": reasons,
        "concerns": concerns,
        "line": _line(score, reasons, concerns),
    }


def _mentions(text: str, term: str) -> bool:
    """Whole-term match, so "go" doesn't fire on "going" and "r" on "for".

    Terms with regex-special characters (c++, c#, .net) are matched literally with
    boundaries relaxed, since \\b behaves badly around punctuation.
    """
    term = term.strip().lower()
    if not term:
        return False
    if re.fullmatch(r"[a-z0-9 ]+", term):
        return re.search(rf"\b{re.escape(term)}\b", text) is not None
    return term in text


def _line(score: float, reasons: list[str], concerns: list[str]) -> str:
    pct = f"{round((score or 0) * 100)}%"
    body = " · ".join(reasons) if reasons else "matched your profile"
    out = f"{pct} · {body}"
    if concerns:
        out += f"  ⚠️ {concerns[0]}"
    return out


def explain_line(posting, profile=None, *, summary: dict | None = None,
                 score: float | None = None) -> str:
    """Just the one-liner — what an alert card shows under the title."""
    return explain(posting, profile, summary=summary, score=score)["line"]
