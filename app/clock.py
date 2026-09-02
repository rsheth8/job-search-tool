"""How long an application actually takes, from opening the form to Filed.

The product's north star is a number it never recorded: *under three minutes,
open form to Filed, eight times in an evening*. Without this module that target
cannot be passed or failed — and "cut whatever stole seconds" has nothing to
read, because it needs to know **which** seconds.

Three marks per application:

    opened  the ATS form appeared on screen (client)
    filled  Autofill finished a pass        (client)
    filed   they tapped Filed               (server, in /apply/applied)

``filed`` is recorded server-side on purpose. It is the one mark that decides
whether a session counts, so it should not depend on a client remembering to
send it.

Two deliberate choices about what gets measured:

*Marks, not columns.* People open a form, wander off, and come back. Storing
``opened_at`` on ``apply_queue`` would keep either the first open (making every
interrupted application look like an hour) or the last (silently discarding
that it took three attempts). An event log keeps both, so a session can be
measured from the open that actually finished it while ``reopens`` still says
how many it took.

*Median and p90, never mean.* One application abandoned overnight and finished
at breakfast would drag a mean past the target on its own and hide eight fast
ones. The question is "is a typical application under three minutes", and that
is a median question.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .db import connect

#: The plan's north star, in seconds. A session at or under this counts.
TARGET_SECONDS = 180

#: Marks the client is allowed to send. ``filed`` is server-side only — see the
#: module docstring — so accepting it here would let a client fake a fast lap.
CLIENT_MARKS = frozenset({"opened", "filled"})

MARKS = frozenset({"opened", "filled", "filed"})

_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _now() -> str:
    return datetime.now(timezone.utc).strftime(_FMT)


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.strptime(ts, _FMT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def mark(user_id: str, posting_id: int, name: str, *,
         at: str | None = None) -> bool:
    """Record one mark. Returns False for an unknown mark rather than raising.

    An unrecognised mark is a client that is ahead of (or behind) the server;
    that is a reason to ignore a row, not to fail someone's application.
    """
    uid = (user_id or "").strip()
    if name not in MARKS or not uid:
        return False
    try:
        pid = int(posting_id)
    except (TypeError, ValueError):
        return False
    with connect() as conn:
        conn.execute(
            "INSERT INTO apply_marks (user_id, posting_id, mark, at) "
            "VALUES (?, ?, ?, ?)", (uid, pid, name, at or _now()))
    return True


def _rows(user_id: str):
    """Every mark for this user, deliberately unfiltered by date.

    The window is applied to the ``filed`` mark in ``sessions``, not here. A
    form opened at 11:58pm and filed at 12:03am belongs to the session that
    finished it; filtering marks by date in SQL would drop its ``opened`` row
    and make a five-minute application look untimed.
    """
    with connect() as conn:
        return list(conn.execute(
            "SELECT posting_id, mark, at FROM apply_marks WHERE user_id = ? "
            "ORDER BY posting_id, at", ((user_id or "").strip(),)))


def sessions(user_id: str, *, days: int | None = None,
             limit: int = 50) -> list[dict]:
    """One row per completed application, newest first.

    A session is anchored on a ``filed`` mark and measured back to the last
    ``opened`` at or before it — the attempt that actually finished the job.
    An application filed with no preceding open (filed from chat, or from a
    build too old to send marks) is not a timed session and is left out
    rather than counted as instantaneous.
    """
    cutoff = None
    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    by_posting: dict[int, list[tuple[str, datetime]]] = {}
    for pid, name, at in _rows(user_id):
        when = _parse(at)
        if when is None:
            continue
        by_posting.setdefault(int(pid), []).append((name, when))

    out: list[dict] = []
    for pid, marks in by_posting.items():
        marks.sort(key=lambda m: m[1])
        filed = next((w for n, w in marks if n == "filed"), None)
        if filed is None or (cutoff is not None and filed < cutoff):
            continue
        opens = [w for n, w in marks if n == "opened" and w <= filed]
        if not opens:
            continue                      # filed, but never timed
        opened = opens[-1]
        fills = [w for n, w in marks if n == "filled" and opened <= w <= filed]
        filled = fills[-1] if fills else None
        out.append({
            "posting_id": pid,
            "opened_at": opened.strftime(_FMT),
            "filed_at": filed.strftime(_FMT),
            "open_to_filed": int((filed - opened).total_seconds()),
            "open_to_fill": (int((filled - opened).total_seconds())
                             if filled else None),
            "fill_to_filed": (int((filed - filled).total_seconds())
                              if filled else None),
            # How many times they opened this form in total. More than one
            # means an attempt was abandoned, which is worth seeing even when
            # the finishing lap was fast.
            "reopens": max(0, len([1 for n, _ in marks if n == "opened"]) - 1),
        })

    out.sort(key=lambda s: s["filed_at"], reverse=True)
    return out[:limit]


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2


def _percentile(values: list[int], pct: float) -> int | None:
    """Nearest-rank percentile. No numpy, and no interpolation to argue about."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(round(pct / 100 * len(ordered)))))
    return ordered[rank - 1]


def summary(user_id: str, *, days: int = 7) -> dict:
    """Whether a typical application is under the target, and where time goes.

    ``under_target`` over ``timed`` is the Phase 1 exit test. The two leg
    medians are what "cut whatever stole seconds" reads: a slow ``open_to_fill``
    is the page or the fill itself, a slow ``fill_to_filed`` is everything a
    person still has to do by hand — attachments, exotic dropdowns, review.
    """
    rows = sessions(user_id, days=days, limit=10_000)
    totals = [s["open_to_filed"] for s in rows]
    to_fill = [s["open_to_fill"] for s in rows if s["open_to_fill"] is not None]
    to_filed = [s["fill_to_filed"] for s in rows if s["fill_to_filed"] is not None]
    return {
        "days": days,
        "timed": len(rows),
        "target_seconds": TARGET_SECONDS,
        "under_target": sum(1 for t in totals if t <= TARGET_SECONDS),
        "median_seconds": _median(totals),
        "p90_seconds": _percentile(totals, 90),
        "fastest_seconds": min(totals) if totals else None,
        "slowest_seconds": max(totals) if totals else None,
        "median_open_to_fill": _median(to_fill),
        "median_fill_to_filed": _median(to_filed),
        "reopened_sessions": sum(1 for s in rows if s["reopens"]),
    }


def best_sitting(user_id: str, *, days: int = 30) -> dict:
    """The most applications filed in one local-ish day, and how fast they were.

    The plan's north star is eight in an evening, so the count alone is not the
    claim — eight slow ones is a different evening from eight fast ones.
    """
    rows = sessions(user_id, days=days, limit=10_000)
    by_day: dict[str, list[int]] = {}
    for s in rows:
        by_day.setdefault(s["filed_at"][:10], []).append(s["open_to_filed"])
    if not by_day:
        return {"day": None, "filed": 0, "median_seconds": None}
    day, times = max(by_day.items(), key=lambda kv: len(kv[1]))
    return {"day": day, "filed": len(times), "median_seconds": _median(times)}
