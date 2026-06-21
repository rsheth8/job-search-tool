"""Applicant identity + application-autofill endpoints (Track C)."""
from __future__ import annotations

from app import applicant, jobstore, outreach
from app.jobsources import JobPosting


def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


# --- identity model --------------------------------------------------------

def test_identity_partial_update_and_full_name_derivation():
    applicant.set_identity("u1", {"first_name": "Ada", "last_name": "Lovelace",
                                  "email": "ada@x.com"})
    d = applicant.get_identity("u1")
    assert d["full_name"] == "Ada Lovelace"      # derived
    assert d["email"] == "ada@x.com"
    # Partial update keeps prior fields.
    applicant.set_identity("u1", {"phone": "555-1234"})
    d = applicant.get_identity("u1")
    assert d["email"] == "ada@x.com" and d["phone"] == "555-1234"


def test_location_derived_from_city_state():
    applicant.set_identity("u1", {"city": "Chicago", "state": "IL"})
    assert applicant.get_identity("u1")["location"] == "Chicago, IL"
    # An explicit location wins over the derived one.
    applicant.set_identity("u1", {"location": "Remote (US)"})
    assert applicant.get_identity("u1")["location"] == "Remote (US)"
    assert applicant.autofill_map("u1")["location"] == "Remote (US)"


def test_expanded_application_fields_round_trip():
    applicant.set_identity("u1", {
        "preferred_name": "Ace", "pronouns": "she/her", "address": "1 Main St",
        "zip": "60601", "degree": "B.S.", "discipline": "Computer Science",
        "gpa": "3.9", "current_company": "Acme", "current_title": "SWE",
        "salary_expectation": "$120,000", "start_date": "Immediately",
        "willing_to_relocate": "yes",
    })
    d = applicant.get_identity("u1")
    assert d["discipline"] == "Computer Science" and d["gpa"] == "3.9"
    assert d["salary_expectation"] == "$120,000"
    assert d["willing_to_relocate"] is True
    # Bools render Yes/No for select/radio matching in the extension.
    assert applicant.autofill_map("u1")["willing_to_relocate"] == "Yes"


def test_identity_bools_and_unknown_keys():
    applicant.set_identity("u1", {"work_authorized": "yes", "needs_sponsorship": False,
                                  "favourite_color": "blue"})
    d = applicant.get_identity("u1")
    assert d["work_authorized"] is True and d["needs_sponsorship"] is False
    assert "favourite_color" not in d              # unknown key dropped


def test_autofill_map_renders_bools_as_yes_no():
    applicant.set_identity("u1", {"first_name": "Ada", "work_authorized": True})
    m = applicant.autofill_map("u1")
    assert m["work_authorized"] == "Yes" and m["first_name"] == "Ada"


def test_empty_string_clears_a_field():
    applicant.set_identity("u1", {"phone": "555"})
    applicant.set_identity("u1", {"phone": ""})
    assert "phone" not in applicant.get_identity("u1")


# --- question answering (offline template path) ----------------------------

def test_answer_question_template_is_grounded_and_safe():
    ans = outreach.answer_application_question(
        "Why do you want to work here?", "Stripe", "Backend Engineer", "payments",
    )
    assert "Stripe" in ans and "[" not in ans   # no placeholders, never empty
    assert ans.strip()


def test_draft_question_answers_one_per_question_offline():
    qs = ["Why do you want to work at Stripe?",
          "Why are you a strong fit for the Backend Engineer role?",
          "Tell us about a relevant project."]
    answers = outreach.draft_question_answers(qs, "Stripe", "Backend Engineer", "payments")
    assert len(answers) == len(qs)               # always one answer per question
    assert all(a and a.strip() for a in answers)
    assert outreach.draft_question_answers([], "Stripe", "X", "") == []


# --- endpoints -------------------------------------------------------------

def test_identity_endpoints_roundtrip():
    c = _client()
    r = c.post("/apply/identity", json={"user": "u1",
               "fields": {"first_name": "Ada", "email": "ada@x.com"}})
    assert r.json()["saved"]["email"] == "ada@x.com"
    got = c.get("/apply/identity?user=u1").json()["fields"]
    assert got["email"] == "ada@x.com" and got["full_name"] == "Ada"


def test_answer_endpoint_uses_posting_context_and_stages_it():
    from app import apply_queue

    row = jobstore.save_posting(
        "u1", JobPosting("greenhouse", "1", "Backend Engineer", "https://x/1",
                         company="Stripe", location="Remote", description="payments apis"),
        relevance_score=0.8, status="queued")
    c = _client()
    r = c.post("/apply/answer", json={"user": "u1", "posting_id": row["id"],
               "question": "Why do you want to work here?"})
    body = r.json()
    assert "Stripe" in body["answer"]
    # Answering against a posting also stages it into the apply queue.
    assert [it["posting_id"] for it in apply_queue.list_queue("u1")] == [row["id"]]


def test_answer_endpoint_requires_a_question():
    assert _client().post("/apply/answer", json={"user": "u1"}).json()["error"]


def test_apply_token_gate(monkeypatch):
    monkeypatch.setenv("APPLY_API_TOKEN", "secret")
    from app.config import get_settings
    get_settings.cache_clear()
    c = _client()
    assert c.get("/apply/identity?user=u1").status_code == 401      # no token
    assert c.get("/apply/identity?user=u1",
                 headers={"X-Apply-Token": "secret"}).status_code == 200
    get_settings.cache_clear()
