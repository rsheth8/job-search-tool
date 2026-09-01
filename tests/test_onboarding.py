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


def test_profile_save_leaves_out_what_it_was_not_given():
    """The quiz's last step saves only a résumé summary. Re-posting the search
    fields from local state there wiped the locations and seniority of anyone who
    reached it without them loaded — a retake, or a setup refresh that failed."""
    c = TestClient(app)
    c.post("/apply/profile", json={"user": "p1", "fields": {
        "roles": "software engineer", "keywords": "python",
        "locations": "Remote, Chicago", "seniority": "New grad",
    }})
    c.post("/apply/profile", json={
        "user": "p1", "fields": {"resume_summary": "CS senior at UIC."}})
    fields = c.get("/apply/profile?user=p1").json()["fields"]
    assert fields["locations"] == "Remote, Chicago"
    assert fields["seniority"] == "New grad"
    assert fields["keywords"] == "python"
    assert fields["resume_summary"] == "CS senior at UIC."


def test_profile_save_still_clears_a_field_asked_to_clear():
    """Absent means "leave it"; an explicit empty string still clears."""
    c = TestClient(app)
    c.post("/apply/profile", json={"user": "p2", "fields": {
        "roles": "backend", "keywords": "go", "locations": "NYC",
    }})
    c.post("/apply/profile", json={"user": "p2", "fields": {"locations": ""}})
    assert c.get("/apply/profile?user=p2").json()["fields"]["locations"] == ""


def test_a_skipped_quiz_reports_itself_as_not_complete():
    """Roles saved, nothing else. The gate lets them into the app, but `complete`
    stays false so the Done step can say Autofill has nothing to work with
    instead of handing over a dead feature."""
    c = TestClient(app)
    c.post("/apply/setup", json={"user": "thin", "action": "start"})
    c.post("/apply/profile", json={
        "user": "thin", "fields": {"roles": "swe", "keywords": "swe"}})
    body = c.post("/apply/setup", json={"user": "thin", "action": "complete"}).json()
    assert body["needs_setup"] is False
    assert body["complete"] is False
    assert body["identity_score"] == 0.0
    assert applicant.autofill_map("thin") == {}


def test_coverage_alone_is_not_enough_without_a_name_and_email():
    """School, links and location add up to half the coverage bar while leaving
    no name and no email — and every application form opens with those. Reaching
    50% that way used to report `complete`, which is the app promising Autofill
    works when it can't fill the first three boxes on the page."""
    applicant.set_identity("nameless", {
        "city": "Chicago", "state": "IL", "zip": "60601",
        "country": "United States", "school": "UIC", "degree": "BS",
        "discipline": "CS", "grad_year": "2027", "years_experience": "1",
        "linkedin": "https://linkedin.com/in/x", "github": "https://github.com/x",
        "work_authorized": True, "needs_sponsorship": False, "over_18": True,
    })
    profile.set_profile("nameless", roles="swe", keywords="swe", locations="remote")
    body = TestClient(app).get("/apply/setup?user=nameless").json()
    assert body["identity_score"] >= 0.5          # the fraction is satisfied
    assert body["complete"] is False              # but the core is not
    assert body["identity_core_missing"] == ["first name", "last name", "email"]

    applicant.set_identity("nameless", {
        "first_name": "Ada", "last_name": "Lovelace", "email": "ada@x.com"})
    body = TestClient(app).get("/apply/setup?user=nameless").json()
    assert body["identity_core_missing"] == []
    assert body["complete"] is True


def test_setup_education_entries_carry_every_key():
    """Ragged education entries broke the iOS quiz at its first page.

    Storage drops empty values, so a degree with no GPA has no ``gpa`` key.
    The iOS ``EducationEntry`` declares eight non-optional strings, and Swift's
    synthesised decoder throws ``keyNotFound`` on a missing one — default
    property values do not rescue it. Education rides inside the setup payload,
    so the whole response failed to decode and ``markSetup`` threw before the
    quiz could save anything. The user saw only "Something went wrong."
    """
    c = TestClient(app)
    r = c.post("/apply/identity", json={"user": "edu_wire", "fields": {
        "education": [
            # No gpa, no start_year, no grad_month — exactly the shape a
            # resume import produces for a degree in progress.
            {"school": "University of Minnesota", "degree": "M.S.",
             "discipline": "Data Science", "grad_year": "2026",
             "status": "in_progress"},
        ],
    }})
    assert r.status_code == 200

    entries = c.get("/apply/setup?user=edu_wire").json()["education"]
    assert entries, "the degree should survive to the setup payload"
    for entry in entries:
        assert set(entry) == set(applicant.EDUCATION_FIELDS), (
            f"ragged entry {sorted(entry)} — a strict client cannot decode it")
        assert all(isinstance(v, str) for v in entry.values())
    # The blanks are blank, not invented.
    assert entries[0]["gpa"] == ""
    assert entries[0]["school"] == "University of Minnesota"
