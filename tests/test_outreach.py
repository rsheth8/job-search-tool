"""Phase 3: Apollo recruiter discovery + outreach drafting (all offline).

The real Apollo HTTP call is never made — tests monkeypatch the safe wrapper
``apollo.discover_recruiters`` (or rely on the no-key path returning []).
"""
from __future__ import annotations

from app import apollo, outreach, scoring, store
from app.engine import handle_sms


def _people():
    return [
        {"name": "Jane Doe", "title": "Technical Recruiter",
         "email": "jane@stripe.com", "linkedin_url": "https://linkedin.com/in/janedoe",
         "company": "Stripe"},
        {"name": "John Smith", "title": "Sourcer",
         "email": None, "linkedin_url": None, "company": "Stripe"},
    ]


# --- Apollo client (no network) ---------------------------------------------

def test_discover_returns_empty_without_key():
    # conftest leaves APOLLO_API_KEY unset → no client, graceful empty list.
    assert apollo.get_apollo() is None
    assert apollo.discover_recruiters("Stripe") == []


def test_normalize_strips_locked_email():
    p = apollo._normalize_person(
        {"name": "Jane Doe", "title": "Recruiter",
         "email": "email_not_unlocked@domain.com",
         "linkedin_url": "x", "organization": {"name": "Stripe"}},
        "Stripe",
    )
    assert p["email"] is None
    assert p["company"] == "Stripe"
    assert p["name"] == "Jane Doe"


def test_normalize_uses_first_name_when_name_missing():
    p = apollo._normalize_person(
        {"first_name": "Andrea", "last_name_obfuscated": "M.",
         "title": "Talent Acquisition", "organization": {"name": "Stripe"}},
        "Stripe",
    )
    assert p["name"] == "Andrea M."


def test_discover_swallows_apollo_errors(monkeypatch):
    class Boom:
        def find_people(self, *a, **k):
            raise RuntimeError("apollo down")

    monkeypatch.setattr(apollo, "get_apollo", lambda: Boom())
    assert apollo.discover_recruiters("Stripe") == []  # never raises


# --- persistence ------------------------------------------------------------

def test_store_recruiters_dedupes_by_name():
    outreach.store_recruiters("u", "Stripe", _people())
    # Re-storing the same people (plus a dup) adds nothing new.
    rows = outreach.store_recruiters("u", "Stripe", _people())
    assert len(rows) == 2
    assert {r["name"] for r in rows} == {"Jane Doe", "John Smith"}


def test_discover_for_company_stores_and_caches(monkeypatch):
    calls = {"n": 0}

    def fake(company, *, limit=3):
        calls["n"] += 1
        return _people()

    monkeypatch.setattr(apollo, "discover_recruiters", fake)
    first = outreach.discover_for_company("u", "Stripe")
    assert len(first.recruiters) == 2
    assert calls["n"] == 1
    # Second call is served from the DB — Apollo is not hit again.
    second = outreach.discover_for_company("u", "Stripe")
    assert len(second.recruiters) == 2
    assert second.from_cache is True
    assert calls["n"] == 1


def test_discover_for_company_empty_when_apollo_finds_nobody(monkeypatch):
    monkeypatch.setattr(apollo, "discover_recruiters", lambda c, **k: [])
    assert outreach.discover_for_company("u", "Stripe").recruiters == []


def test_apollo_footnote_cached():
    r = outreach.DiscoveryResult(recruiters=[], from_cache=True)
    assert "no API call" in outreach.apollo_footnote(r)


def test_apollo_footnote_free_search():
    r = outreach.DiscoveryResult(recruiters=[], people_search=True)
    assert "no credits" in outreach.apollo_footnote(r)


def test_apollo_footnote_org_credits():
    r = outreach.DiscoveryResult(recruiters=[], people_search=True, org_credits=1)
    foot = outreach.apollo_footnote(r)
    assert "org lookup" in foot
    assert "cached" in foot


# --- drafting ---------------------------------------------------------------

def test_draft_template_mentions_company_role_and_first_name():
    rec = {"name": "Jane Doe", "title": "Recruiter"}
    msg = outreach.draft_outreach("Stripe", rec, role="SWE")
    assert "Stripe" in msg
    assert "SWE" in msg
    assert "Jane" in msg  # first name only
    assert "Doe" not in msg


def test_draft_template_without_role_omits_role_phrase():
    msg = outreach.draft_outreach("Stripe", {"name": "Jane"})
    assert "Stripe" in msg
    assert "role" not in msg.lower()


# --- scorer integration -----------------------------------------------------

def test_recruiter_table_drives_followup_bonus():
    app = store.create_application("u", "Stripe", "SWE")
    assert store.has_recruiter_signal(app["id"]) is False
    outreach.store_recruiters("u", "Stripe", _people())
    assert store.has_recruiter_signal(app["id"]) is True
    # And the bonus shows up in the ranking breakdown.
    ranked = scoring.rank_followups("u")
    breakdown = ranked[0][2]
    assert breakdown["recruiter_component"] == scoring.RECRUITER_BONUS


# --- engine wiring (OUTREACH intent) ----------------------------------------

def test_outreach_offline_asks_for_apollo_key():
    reply = handle_sms("ou", "reach out to a recruiter at stripe")
    assert "Apollo" in reply
    assert "stripe" in reply.lower()


def test_outreach_surfaces_apollo_plan_issue(monkeypatch):
    monkeypatch.setattr(
        apollo,
        "discover_recruiters",
        lambda c, **k: [],
    )
    monkeypatch.setattr(
        apollo,
        "discovery_issue",
        lambda: "Apollo people search isn't available on your current API plan",
    )
    reply = handle_sms("ou", "reach out to a recruiter at stripe")
    assert "API plan" in reply or "free-plan" in reply.lower() or "trial" in reply.lower()


def test_outreach_drafts_when_recruiters_found(monkeypatch):
    monkeypatch.setattr(apollo, "discover_recruiters", lambda c, **k: _people())
    store.create_application("ou", "Stripe", "Backend Engineer")
    reply = handle_sms("ou", "reach out to a recruiter at stripe")
    assert "Jane Doe" in reply
    assert "Technical Recruiter" in reply
    assert "Backend Engineer" in reply  # role threaded into the draft
    assert "(+1 more" in reply
    assert "no credits" in reply.lower()
    # Honest about not sending anything automatically.
    assert "won't send" in reply.lower()
    # Recruiters were persisted.
    assert outreach.has_recruiters("ou", "Stripe") is True
