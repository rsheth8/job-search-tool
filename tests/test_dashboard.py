"""Read-only web dashboard (offline render + route)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import dashboard, deadlines, outreach, store
from app.main import app


def _now() -> datetime:
    return datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _seed(user="local"):
    store.create_application(user, "Stripe", "SWE", status="Onsite")
    store.create_application(user, "Notion", "PM", status="Applied")
    store.create_application(user, "Google", "SWE", status="Offer")
    deadlines.create_deadline(
        user, "Stripe", "Onsite", _now() + timedelta(days=2), schedule_reminder=False
    )
    outreach.store_recruiters(
        user, "Stripe",
        [{"name": "Jane Doe", "title": "Recruiter",
          "linkedin_url": "https://linkedin.com/in/jane", "company": "Stripe"}],
    )


def test_render_contains_all_sections():
    _seed()
    html = dashboard.render("local", now=_now())
    assert "<html" in html
    assert "Stripe" in html and "Notion" in html
    assert "Follow up next" in html
    assert "Upcoming" in html
    assert "Pipeline" in html
    assert "Recruiters" in html and "Jane Doe" in html
    # Stat cards present.
    assert "Response" in html and "Offers" in html
    # New: applications section with a search box + funnel bar.
    assert "Applications" in html
    assert "id='q'" in html and "funnel" in html


def test_application_history_expands():
    store.create_application("local", "Acme", "SWE")
    app = store.find_application("local", "Acme")
    store.update_status(app["id"], "OA received")
    store.add_note(app["id"], "recruiter call went well")
    html = dashboard.render("local", now=_now())
    assert "<details" in html
    assert "recruiter call went well" in html  # note shows in the timeline
    assert "Applied to Acme" in html  # created event


def test_pending_reminders_section():
    from datetime import timedelta
    from app import reminders
    reminders.create_reminder("local", _now() + timedelta(days=2), "ping Stripe")
    html = dashboard.render("local", now=_now())
    assert "Reminders" in html and "ping Stripe" in html


def test_search_input_data_attributes_present():
    store.create_application("local", "Datadog", "SRE")
    html = dashboard.render("local", now=_now())
    assert "data-search='datadog sre'" in html


def test_render_empty_user():
    html = dashboard.render("nobody", now=_now())
    assert "No applications" in html


def test_default_user_picks_busiest():
    store.create_application("+15550001111", "A", None)
    store.create_application("+15550001111", "B", None)
    store.create_application("solo", "C", None)
    assert dashboard.default_user() == "+15550001111"


def test_escapes_user_data():
    store.create_application("local", "<script>evil</script>", None)
    html = dashboard.render("local", now=_now())
    assert "<script>evil" not in html
    assert "&lt;script&gt;" in html


def test_dashboard_route_serves_html():
    _seed()
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Job Search" in resp.text


def test_dashboard_route_user_param():
    store.create_application("+1999", "ParamCo", None)
    client = TestClient(app)
    resp = client.get("/?user=%2B1999")
    assert "ParamCo" in resp.text
