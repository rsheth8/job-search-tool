"""Pipeline analytics (offline)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import stats, store
from app.engine import handle_sms


def _seed(user="u"):
    # 6 apps spanning the funnel + one ghosted + one rejected.
    store.create_application(user, "Applied1", "SWE", status="Applied")
    store.create_application(user, "OaCo", "SWE", status="OA received")
    store.create_application(user, "PhoneCo", "SWE", status="Phone screen")
    store.create_application(user, "OnsiteCo", "SWE", status="Onsite")
    store.create_application(user, "OfferCo", "SWE", status="Offer")
    store.create_application(user, "RejectCo", "SWE", status="Rejected")
    store.create_application(user, "GhostCo", "SWE", status="Ghosted")


def test_funnel_counts_and_rates():
    _seed()
    s = stats.compute_stats("u")
    assert s["total"] == 7
    # Active excludes Offer/Rejected/Ghosted (terminal).
    assert s["active"] == 4
    assert s["by_stage"]["Onsite"] == 1
    # responded = everything except Applied + Ghosted = 5/7 = 71%.
    assert s["responded"] == 5
    assert s["response_rate"] == 71
    # interviewing = Phone + Onsite + Offer = 3/7 = 43%.
    assert s["interviewing"] == 3
    assert s["offers"] == 1
    assert s["ghosted"] == 1


def test_empty_stats_render():
    out = stats.render(stats.compute_stats("nobody"))
    assert "No applications" in out


def test_stale_count_uses_last_updated(monkeypatch):
    app = store.create_application("u", "StaleCo", "SWE", status="Applied")
    # Backdate last_updated_at 20 days.
    old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    from app.db import connect
    with connect() as conn:
        conn.execute(
            "UPDATE applications SET last_updated_at = ? WHERE id = ?",
            (old, app["id"]),
        )
    s = stats.compute_stats("u")
    assert s["stale"] == 1


def test_render_includes_funnel_and_rates():
    _seed()
    out = stats.render(stats.compute_stats("u"))
    assert "7 apps" in out
    assert "Response 71%" in out
    assert "Offer" in out


# --- routing ----------------------------------------------------------------

def test_stats_intent_routes_offline():
    _seed("su")
    reply = handle_sms("su", "how am I doing")
    assert "apps" in reply and "Response" in reply


def test_stats_keywords_route():
    _seed("kw")
    for phrase in ("stats", "show me my funnel", "progress"):
        reply = handle_sms("kw", phrase)
        assert "apps" in reply, phrase
