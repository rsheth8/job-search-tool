"""Resume / GitHub / LinkedIn profile import — fill empty fields only."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import applicant, knowledge, profile, profile_import as pi
from app.main import app

RESUME = """
Ada Lovelace
ada@example.com | (555) 010-0192 | Chicago, IL
https://linkedin.com/in/ada-lovelace | https://github.com/ada
University of Illinois  B.S. Computer Science  2026
Software Engineering Intern

SKILLS
Python, React, SQL, AWS

PROJECTS
Engine — A C compiler for a subset of C used in coursework.
PantryPal — Track groceries and recipes in Swift.
"""


def test_heuristic_resume_extracts_the_usual_header():
    got = pi.parse_document(RESUME)
    ident = got["identity"]
    assert ident["first_name"] == "Ada"
    assert ident["last_name"] == "Lovelace"
    assert ident["email"] == "ada@example.com"
    assert "555" in ident["phone"]
    assert ident["city"] == "Chicago" and ident["state"] == "IL"
    assert ident["linkedin"].endswith("ada-lovelace")
    assert ident["github"].endswith("/ada")
    assert "Illinois" in ident["school"]
    assert ident["grad_year"] == "2026"
    assert ident["discipline"] == "Computer Science"
    assert any("compiler" in i["text"].lower() for i in got["knowledge"])
    assert "python" in got["profile"]["keywords"]


def test_heuristic_splits_experience_from_projects():
    text = """
Ada Lovelace
ada@example.com

EXPERIENCE
Software Engineering Intern at Acme — Austin, TX (Summer 2025). Shipped production APIs.
Teaching Assistant at State University — Chicago, IL (2024). Ran weekly labs.

PROJECTS
Engine — A C compiler for a subset of C used in coursework.
PantryPal — Track groceries and recipes in Swift.
"""
    got = pi.parse_document(text)
    exp = [i for i in got["knowledge"] if i["category"] == "experience"]
    proj = [i for i in got["knowledge"] if i["category"] == "project"]
    assert any("Austin" in i["text"] for i in exp)
    assert any("compiler" in i["text"].lower() for i in proj)
    assert not any("Austin" in i["text"] for i in proj)


def test_resume_import_fills_empty_fields_and_projects():
    body = TestClient(app).post("/apply/import/resume", json={
        "user": "u1", "text": RESUME,
    }).json()
    assert body["ok"] is True
    assert body["source"] == "resume"
    assert body["knowledge_added"] >= 1
    assert body["draft"]["project"] or body["draft"]["about"]
    ident = applicant.get_identity("u1")
    assert ident["email"] == "ada@example.com"
    assert ident["school"]
    assert knowledge.list_all("u1", category="project")
    assert profile.has_profile("u1")


def test_resume_import_does_not_overwrite_existing_identity():
    applicant.set_identity("u1", {"email": "keep@x.com", "first_name": "Keep"})
    pi.import_resume("u1", text=RESUME)
    ident = applicant.get_identity("u1")
    assert ident["email"] == "keep@x.com"
    assert ident["first_name"] == "Keep"
    assert ident["last_name"] == "Lovelace"  # was empty, filled


def test_empty_resume_is_rejected():
    r = TestClient(app).post("/apply/import/resume", json={"user": "u1", "text": "   "})
    assert r.status_code == 400


def test_pdf_bytes_are_read():
    pdf = (
        b"%PDF-1.1\n"
        b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n"
        b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n"
        b"3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources<< /Font<< /F1 5 0 R >> >> >>endobj\n"
        b"4 0 obj<< /Length 80 >>stream\n"
        b"BT /F1 12 Tf 72 720 Td (Ada Lovelace ada@example.com Chicago, IL) Tj ET\n"
        b"endstream\nendobj\n"
        b"5 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>endobj\n"
        b"trailer<< /Root 1 0 R >>\n%%EOF\n"
    )
    text = pi._text_from_bytes("ada.pdf", pdf)
    assert "Ada" in text
    assert "ada@example.com" in text


def test_github_username_from_url_or_handle():
    assert pi.github_username("https://github.com/octocat") == "octocat"
    assert pi.github_username("octocat") == "octocat"
    assert pi.github_username("https://github.com/features") == ""
    assert pi.linkedin_url("linkedin.com/in/ada-lovelace/") == (
        "https://www.linkedin.com/in/ada-lovelace"
    )


def test_github_import_maps_profile_and_repos(monkeypatch):
    def fake_get(path: str):
        if path.startswith("/users/ada") and "/repos" not in path:
            return {
                "login": "ada", "name": "Ada Lovelace",
                "location": "Chicago, IL", "blog": "ada.dev",
                "bio": "Math and machines.", "company": "@Analytical",
                "email": "ada@x.com",
            }
        if "/repos" in path:
            return [
                {
                    "name": "engine", "description": "A compiler",
                    "html_url": "https://github.com/ada/engine",
                    "stargazers_count": 12, "language": "C",
                    "fork": False, "archived": False,
                },
                {
                    "name": "noise", "description": "a fork",
                    "fork": True, "stargazers_count": 99,
                },
            ]
        return {"_status": 404}

    monkeypatch.setattr(pi, "_github_get", fake_get)
    body = TestClient(app).post("/apply/import/github", json={
        "user": "u1", "username": "https://github.com/ada",
    }).json()
    assert body["ok"] is True
    ident = applicant.get_identity("u1")
    assert ident["github"] == "https://github.com/ada"
    assert ident["first_name"] == "Ada"
    assert ident["city"] == "Chicago"
    assert ident["portfolio"].startswith("https://")
    assert ident["current_company"] == "Analytical"
    projects = knowledge.list_all("u1", category="project")
    assert len(projects) == 1
    assert "compiler" in projects[0]["text"].lower()
    assert "engine" in projects[0]["text"].lower()
    assert profile.get_profile("u1")["resume_summary"] == "Math and machines."


def test_github_unknown_user(monkeypatch):
    monkeypatch.setattr(pi, "_github_get", lambda path: {"_status": 404})
    r = TestClient(app).post("/apply/import/github", json={"user": "u1", "username": "nope"})
    assert r.status_code == 400


def test_linkedin_url_saves_without_scraping():
    body = TestClient(app).post("/apply/import/linkedin", json={
        "user": "u1", "url": "https://www.linkedin.com/in/ada-lovelace",
    }).json()
    assert body["ok"] is True
    assert applicant.get_identity("u1")["linkedin"].endswith("/ada-lovelace")
    assert "PDF" in body["note"] or "profile page" in body["note"].lower()


def test_linkedin_accepts_country_subdomain_and_query():
    assert pi.linkedin_url("https://uk.linkedin.com/in/ada-lovelace?trk=x") == (
        "https://www.linkedin.com/in/ada-lovelace"
    )
    assert pi.linkedin_url("https://www.linkedin.com/mwlite/in/ada") == (
        "https://www.linkedin.com/in/ada"
    )


def test_github_saves_url_when_api_is_unreachable(monkeypatch):
    monkeypatch.setattr(pi, "_github_get", lambda path: None)
    body = TestClient(app).post("/apply/import/github", json={
        "user": "u1", "username": "octocat",
    }).json()
    assert body["ok"] is True
    assert applicant.get_identity("u1")["github"] == "https://github.com/octocat"
    assert "GitHub URL" in body["note"] or "couldn't load" in body["note"].lower()


def test_school_header_is_not_glued_onto_the_university():
    text = """
Rahil Sheth
rahilsheth05@gmail.com | Vernon Hills, IL
https://linkedin.com/in/rsheth8 | https://github.com/rsheth8

EDUCATION
University of Minnesota Twin Cities
B.S. Computer Science, May 2026, GPA 3.5
M.S. Data Science (expected December 2027)

LEADERSHIP
Event Director, University of Minnesota — 400+ members

EXPERIENCE
Software Developer Intern — HCSC, Chicago, IL (Jun–Aug 2025)
Software/ML intern at printpal.io, Chicago, IL (May–Aug 2023)

SKILLS
Python, JavaScript, TypeScript, React, AI, SQL, AWS

PROJECTS
Distill — Chrome extension with multi-provider LLM integration.
"""
    got = pi.parse_document(text)
    ident = got["identity"]
    assert ident["school"].startswith("University of Minnesota")
    assert "B.S" not in ident["school"]
    assert "LEADERSHIP" not in ident["school"]
    assert ident["grad_year"] == "2027"
    assert "M.S." in ident["degree"] or "B.S." in ident["degree"]
    assert ident.get("gpa") == "3.5"
    loc = got["profile"].get("locations") or ""
    assert "React" not in loc and "AI" not in loc.split()
    assert "Vernon Hills" in loc or "IL" in loc
    roles = (got["profile"].get("roles") or "").lower()
    assert roles != "intern"
    assert "software" in roles
    seniority = got["profile"].get("seniority") or ""
    assert "Internship" in seniority
    assert "New grad" in seniority


def test_skill_list_is_not_used_as_locations():
    messy = {
        "identity": {"city": "Chicago", "state": "IL", "school": "LEADERSHIP University of Minnesota"},
        "profile": {"locations": "React, AI", "roles": "intern", "keywords": "python, react"},
        "knowledge": [],
    }
    got = pi._sanitize_extracted(messy)
    assert "LEADERSHIP" not in got["identity"]["school"]
    assert "University of Minnesota" in got["identity"]["school"]
    assert "React" not in (got["profile"].get("locations") or "")
    assert "Chicago" in got["profile"]["locations"]
    assert got["profile"]["roles"] != "intern"


def test_quiz_draft_uses_stored_knowledge():
    knowledge.add("u1", "project", "Built Distill, a Chrome LLM extension.")
    knowledge.add("u1", "strength", "Full-stack product engineering")
    knowledge.add(
        "u1", "answer", "I'm Rahil.", label="Tell us about yourself"
    )
    applicant.set_identity("u1", {"first_name": "Rahil", "school": "UMN"})
    profile.set_profile("u1", roles="software engineer", locations="Chicago")
    body = TestClient(app).get("/apply/quiz/draft?user=u1").json()
    draft = body["draft"]
    assert "Distill" in draft["project"]
    assert "Full-stack" in draft["strength"]
    assert draft["about"] == "I'm Rahil."
    assert "software engineer" in draft["roles"]


def test_linkedin_pdf_text_fills_like_a_resume():
    pi.import_linkedin("u1", url="https://linkedin.com/in/ada", text=RESUME)
    ident = applicant.get_identity("u1")
    assert ident["linkedin"].endswith("/ada")
    assert ident["email"] == "ada@example.com"


def test_knowledge_is_not_duplicated_on_reimport():
    pi.import_resume("u1", text=RESUME)
    first = len(knowledge.list_all("u1"))
    pi.import_resume("u1", text=RESUME)
    assert len(knowledge.list_all("u1")) == first
