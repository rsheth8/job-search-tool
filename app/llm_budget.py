"""Per-user daily cap on paid Anthropic calls, sliced per feature.

The global token bucket (``app.ratelimit``) still limits burst. This keeps one
beta tester from burning the shared key and starving everyone else's discovery
scoring. Callers fail open to heuristics/templates when the cap is hit.

Seven modules call Claude (matcher, profile_import, onboarding, outreach,
coverletter, resume_tailor, and Horizon's chat answers). They used to share one
undifferentiated daily pool, so a chatty chat session could spend the whole
budget and silently drop every later job score onto heuristics. ``consume`` now
takes a ``feature`` and charges it against both the global cap and that
feature's slice; a call must clear both. Slices may sum past the global cap on
purpose -- the global number stays the hard ceiling, and the slices only stop
any one feature from eating it all.

Set the current user with ``for_user`` (chat, discovery) or ``set_user``
(request handlers). ``consume`` records one call if the caps allow it.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

from .config import get_settings
from .db import connect

_user: ContextVar[str] = ContextVar("llm_budget_user", default="")

#: Feature keys, each mapping to a ``llm_cap_<key>`` setting. An unknown or
#: empty feature is charged against the global cap only, which is what every
#: pre-existing ``consume()`` call site does until it names itself.
FEATURES = ("chat", "discovery", "draft", "parse", "quiz")


def feature_cap(feature: str) -> int:
    """Daily slice for ``feature``. 0 means "global cap only"."""
    key = (feature or "").strip().lower()
    if key not in FEATURES:
        return 0
    return int(getattr(get_settings(), f"llm_cap_{key}", 0) or 0)


def set_user(user_id: str | None) -> None:
    _user.set((user_id or "").strip())


@contextmanager
def for_user(user_id: str | None):
    token = _user.set((user_id or "").strip())
    try:
        yield
    finally:
        _user.reset(token)


def current_user() -> str:
    return _user.get() or "_anon"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def calls_today(user_id: str | None = None, *, feature: str | None = None) -> int:
    """Paid calls this user has made today; ``feature`` narrows to one slice."""
    uid = (user_id or current_user()).strip() or "_anon"
    sql = "SELECT COUNT(*) AS n FROM llm_usage WHERE user_id = ? AND day = ?"
    params: tuple = (uid, _today())
    # None = every feature; "" = only the unnamed slice. Testing truthiness here
    # would collapse those two, reporting the global total for the "" slice.
    if feature is not None:
        sql += " AND feature = ?"
        params += (feature.strip().lower(),)
    with connect() as conn:
        row = conn.execute(sql, params).fetchone()
    return int(row["n"] if row is not None else 0)


def consume(user_id: str | None = None, *, feature: str = "") -> bool:
    """Reserve one paid call. False means the caller should skip the LLM.

    Charged against the global daily cap *and* ``feature``'s slice; both must
    have room. An unnamed feature is charged globally only.
    """
    s = get_settings()
    cap = int(s.llm_max_calls_per_user_per_day or 0)
    slice_cap = feature_cap(feature)
    if cap <= 0 and slice_cap <= 0:
        return True
    uid = (user_id or current_user()).strip() or "_anon"
    if uid:
        set_user(uid)
    key = (feature or "").strip().lower()
    if cap > 0 and calls_today(uid) >= cap:
        return False
    if slice_cap > 0 and calls_today(uid, feature=key) >= slice_cap:
        return False
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with connect() as conn:
        conn.execute(
            "INSERT INTO llm_usage (user_id, day, feature, created_at) "
            "VALUES (?, ?, ?, ?)",
            (uid, _today(), key, now),
        )
    return True


def remaining(feature: str = "", user_id: str | None = None) -> int:
    """Calls left today under the tighter of the global cap and the slice."""
    cap = int(get_settings().llm_max_calls_per_user_per_day or 0)
    uid = (user_id or current_user()).strip() or "_anon"
    left = cap - calls_today(uid) if cap > 0 else 1 << 30
    slice_cap = feature_cap(feature)
    if slice_cap > 0:
        left = min(left, slice_cap - calls_today(uid, feature=feature.strip().lower()))
    return max(0, left)
