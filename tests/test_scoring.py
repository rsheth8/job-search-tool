"""Follow-up scoring tests (offline)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import scoring, store


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age(app_id: int, days: float) -> None:
    """Backdate an application's last activity so staleness is testable."""
    ts = (_now() - timedelta(days=days)).isoformat()
    with store.connect() as conn:
        conn.execute(
            "UPDATE applications SET last_updated_at = ? WHERE id = ?",
            (ts, app_id),
        )


def test_days_since_handles_missing_and_naive():
    now = _now()
    assert scoring.days_since(None, now) == 0.0
    naive = (now - timedelta(days=3)).replace(tzinfo=None).isoformat()
    assert round(scoring.days_since(naive, now)) == 3


def test_stage_weight_breaks_ties_on_equal_staleness():
    now = _now()
    applied = {"last_updated_at": now.isoformat(), "status": "Applied"}
    onsite = {"last_updated_at": now.isoformat(), "status": "Onsite"}
    s_applied, _ = scoring.score_application(applied, now=now)
    s_onsite, _ = scoring.score_application(onsite, now=now)
    assert s_onsite > s_applied


def test_recruiter_bonus_applied():
    now = _now()
    app = {"last_updated_at": now.isoformat(), "status": "Applied"}
    base, _ = scoring.score_application(app, now=now, has_recruiter=False)
    boosted, _ = scoring.score_application(app, now=now, has_recruiter=True)
    assert boosted - base == scoring.RECRUITER_BONUS


def test_staleness_dominates_ranking():
    stale = store.create_application("u", "StaleCo", None)
    fresh = store.create_application("u", "FreshCo", None)
    _age(stale["id"], days=30)
    _age(fresh["id"], days=0)
    ranked = scoring.rank_followups("u")
    assert ranked[0][0]["company"] == "StaleCo"


def test_terminal_statuses_excluded():
    keep = store.create_application("u", "OpenCo", None)
    gone = store.create_application("u", "ClosedCo", None)
    store.update_status(gone["id"], "Rejected")
    companies = [a["company"] for a, _, _ in scoring.rank_followups("u")]
    assert "OpenCo" in companies
    assert "ClosedCo" not in companies


def test_recruiter_signal_from_notes():
    app = store.create_application("u", "RecruiterCo", None)
    assert store.has_recruiter_signal(app["id"]) is False
    store.add_note(app["id"], "their recruiter reached out on LinkedIn")
    assert store.has_recruiter_signal(app["id"]) is True
