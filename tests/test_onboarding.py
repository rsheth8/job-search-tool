"""First-run setup: profile endpoint + coverage status."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import applicant, knowledge, profile
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
    assert body["needs_setup"] is False


def test_setup_wizard_clears_once_profile_exists():
    profile.set_profile("u1", roles="intern", keywords="intern", locations="remote")
    body = TestClient(app).get("/apply/setup?user=u1").json()
    assert body["needs_setup"] is False
    assert body["complete"] is False  # identity still thin
