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
