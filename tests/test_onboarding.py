"""First-run setup: profile endpoint + coverage status + quiz gate."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import applicant, knowledge, onboarding, profile
from app.main import app


def test_profile_round_trip():
    c = TestClient(app)
    r = c.post("/apply/profile", json={
        "user": "u1", "fields": {"roles": "new grad SWE", "locations": "NYC, remote"},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["has_profile"] is True
    assert "SWE" in body["fields"]["roles"]
    assert "NYC" in body["fields"]["locations"]
    got = c.get("/apply/profile?user=u1").json()
    assert got["has_profile"] is True


def test_setup_incomplete_without_profile():
    body = TestClient(app).get("/apply/setup?user=fresh").json()
    assert body["needs_setup"] is True
    assert body["has_profile"] is False
    assert body["complete"] is False


def test_setup_complete_with_profile_and_identity():
    profile.set_profile("u1", roles="backend", keywords="python", locations="chicago")
    applicant.set_identity("u1", {
        "first_name": "Ada", "last_name": "Lovelace", "email": "ada@x.com",
        "phone": "555-0100", "city": "Chicago", "state": "IL",
        "school": "UIUC", "degree": "BS", "grad_year": "2026",
        "years_experience": "3", "work_authorized": True,
        "needs_sponsorship": False, "linkedin": "https://linkedin.com/in/ada",
    })
    knowledge.add("u1", "project", "Built a compiler")
    body = TestClient(app).get("/apply/setup?user=u1").json()
    assert body["has_profile"] is True
    assert body["identity_score"] >= 0.5
    assert body["complete"] is True
    assert body["needs_setup"] is False  # grandfathered — already had a profile


def test_setup_wizard_clears_once_profile_exists():
    profile.set_profile("u1", roles="intern", keywords="intern", locations="remote")
    body = TestClient(app).get("/apply/setup?user=u1").json()
    assert body["needs_setup"] is False
    assert body["complete"] is False  # identity still thin


def test_new_member_stays_in_quiz_after_saving_roles():
    """Saving roles used to dismiss setup. The quiz has to keep going."""
    c = TestClient(app)
    started = c.post("/apply/setup", json={"user": "fresh", "action": "start"}).json()
    assert started["needs_setup"] is True
    assert started["onboarding"] == "started"

    c.post("/apply/profile", json={
        "user": "fresh", "fields": {"roles": "SWE intern", "locations": "remote"},
    })
    body = c.get("/apply/setup?user=fresh").json()
    assert body["has_profile"] is True
    assert body["needs_setup"] is True

    done = c.post("/apply/setup", json={"user": "fresh", "action": "complete"}).json()
    assert done["needs_setup"] is False
    assert done["onboarding"] == "complete"
    assert c.get("/apply/setup?user=fresh").json()["needs_setup"] is False


def test_setup_start_is_noop_after_complete():
    profile.set_profile("u1", roles="SWE", locations="NYC")
    onboarding.mark_complete("u1")
    body = TestClient(app).post("/apply/setup", json={"user": "u1", "action": "start"}).json()
    assert body["onboarding"] == "complete"
    assert body["needs_setup"] is False


def test_setup_rejects_unknown_action():
    r = TestClient(app).post("/apply/setup", json={"user": "u1", "action": "shrug"})
    assert r.status_code == 400


def test_setup_identity_includes_extended_autofill_fields():
    applicant.set_identity("u1", {
        "first_name": "Ada", "github": "https://github.com/ada",
        "country": "United States", "zip": "60601", "discipline": "CS",
        "over_18": True, "willing_to_relocate": False,
        "work_arrangement": "Remote", "start_date": "June 2026",
    })
    identity = TestClient(app).get("/apply/setup?user=u1").json()["identity"]
    assert identity["github"].endswith("ada")
    assert identity["country"] == "United States"
    assert identity["over_18"] == "true"
    assert identity["willing_to_relocate"] == "false"


def test_quiz_can_reach_full_identity_coverage():
    applicant.set_identity("u1", {
        "first_name": "Ada", "last_name": "Lovelace", "email": "ada@x.com",
        "phone": "555-0100", "city": "Chicago", "state": "IL",
        "country": "United States", "zip": "60601",
        "linkedin": "https://linkedin.com/in/ada",
        "github": "https://github.com/ada",
        "school": "UIUC", "degree": "B.S.", "discipline": "Computer Science",
        "grad_year": "2026", "years_experience": "0",
        "work_authorized": True, "needs_sponsorship": False,
        "over_18": True, "willing_to_relocate": True,
        "work_arrangement": "Remote", "start_date": "June 2026",
    })
    report = knowledge.audit("u1")
    assert report["score"] == 1.0
    assert report["identity_missing"] == []


def test_setup_prefills_name_and_email_from_the_account():
    """Apple only sends name/email on first sign-in — surface them in the quiz."""
    from app import auth

    auth._insert_user(
        user_id="usr_ada", apple_sub="sub_ada",
        email="ada@x.com", display_name="Ada Lovelace",
    )
    identity = TestClient(app).get("/apply/setup?user=usr_ada").json()["identity"]
    assert identity["email"] == "ada@x.com"
    assert identity["first_name"] == "Ada"
    assert identity["last_name"] == "Lovelace"
    assert "email" not in applicant.get_identity("usr_ada")
