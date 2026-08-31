"""Filing an application, and fixing it afterwards.

Three faults met in one session on a real phone:

* filing a job left it in the queue. The phone reads ``queue.first`` as "up
  next", so the job just applied to stayed at the top of the dashboard and the
  queue never advanced;
* tapping Filed twice filed it twice -- ``create_application`` is a plain
  INSERT with no natural key;
* and nothing could then remove the duplicate. ``store`` has been able to edit,
  restage and delete an application all along; no endpoint exposed any of it,
  so a filed row was permanent from the phone.
"""
from __future__ import annotations

import pytest

from app import jobstore, store
from app.jobsources import JobPosting


def _save(user="u1", ext="1", title="Software Engineer", company="Acme") -> int:
    row = jobstore.save_posting(
        user,
        JobPosting(source="greenhouse", external_id=ext, title=title,
                   url="https://x/apply", company=company, location="Remote",
                   description="Build software."),
        relevance_score=0.7, status="queued",
    )
    return row["id"]


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _file(client, pid, user="u1"):
    return client.post("/apply/applied",
                       json={"user": user, "posting_id": pid}).json()


# --- the queue advances ---------------------------------------------------

def test_filing_a_job_takes_it_out_of_the_queue(client):
    pid = _save()
    client.post("/apply/stage", json={"user": "u1", "posting_id": pid})
    assert client.get("/apply/data?user=u1").json()["queue"]

    assert _file(client, pid)["ok"]
    data = client.get("/apply/data?user=u1").json()
    assert [q["posting_id"] for q in data["queue"]] == [], "still in the queue"


def test_a_filed_job_is_not_offered_as_a_fresh_match_either(client):
    """It leaves the queue, but it must not reappear on the other side."""
    pid = _save()
    client.post("/apply/stage", json={"user": "u1", "posting_id": pid})
    _file(client, pid)
    data = client.get("/apply/data?user=u1").json()
    assert [q["posting_id"] for q in data["queued"]] == []


def test_the_next_job_moves_up(client):
    """The whole point: something else becomes "up next"."""
    first, second = _save(ext="1", company="Acme"), _save(ext="2", company="Beta")
    for pid in (first, second):
        client.post("/apply/stage", json={"user": "u1", "posting_id": pid})
    _file(client, first)
    queue = client.get("/apply/data?user=u1").json()["queue"]
    assert [q["posting_id"] for q in queue] == [second]


# --- filing twice ---------------------------------------------------------

def test_filing_the_same_job_twice_files_it_once(client):
    pid = _save(company="Acme")
    assert _file(client, pid) == {"ok": True, "duplicate": False}
    assert _file(client, pid) == {"ok": True, "duplicate": True}
    acme = [a for a in store.list_applications("u1") if a["company"] == "Acme"]
    assert len(acme) == 1


def test_filing_twice_still_reports_success(client):
    """The second tap is a no-op, not an error -- the user did nothing wrong."""
    pid = _save()
    _file(client, pid)
    assert _file(client, pid)["ok"] is True


def test_an_unknown_posting_is_refused(client):
    assert _file(client, 999999)["ok"] is False


# --- editing what was filed ----------------------------------------------

def _one(client, pid, user="u1") -> int:
    _file(client, pid, user=user)
    return client.get(f"/apply/applications?user={user}").json()["applications"][0]["id"]


def test_a_filed_application_can_be_deleted(client):
    """The fix for the duplicate the user could see but not remove."""
    app_id = _one(client, _save())
    assert client.post("/apply/applications/delete",
                       json={"user": "u1", "application_id": app_id}).json()["ok"]
    assert client.get("/apply/applications?user=u1").json()["applications"] == []


def test_a_filed_application_can_be_restaged(client):
    app_id = _one(client, _save())
    got = client.post("/apply/applications/status",
                      json={"user": "u1", "application_id": app_id,
                            "status": "Interview"}).json()
    assert got["ok"] and got["application"]["status"] == "Interview"


def test_company_and_role_can_be_corrected(client):
    app_id = _one(client, _save(company="Acme"))
    got = client.post("/apply/applications/edit",
                      json={"user": "u1", "application_id": app_id,
                            "company": "Acme Corp", "role": "Backend Engineer"}).json()
    assert got["application"]["company"] == "Acme Corp"
    assert got["application"]["role"] == "Backend Engineer"


def test_editing_one_field_leaves_the_other_alone(client):
    app_id = _one(client, _save(company="Acme", title="Software Engineer"))
    got = client.post("/apply/applications/edit",
                      json={"user": "u1", "application_id": app_id,
                            "company": "Acme Corp"}).json()
    assert got["application"]["role"] == "Software Engineer"


def test_a_blank_correction_is_refused_not_stored(client):
    app_id = _one(client, _save(company="Acme"))
    got = client.post("/apply/applications/edit",
                      json={"user": "u1", "application_id": app_id,
                            "company": "   "}).json()
    assert got["ok"] is False
    apps = client.get("/apply/applications?user=u1").json()["applications"]
    assert apps[0]["company"] == "Acme"


def test_an_empty_status_is_refused(client):
    app_id = _one(client, _save())
    assert client.post("/apply/applications/status",
                       json={"user": "u1", "application_id": app_id,
                             "status": ""}).json()["ok"] is False


def test_the_stage_vocabulary_ships_with_the_list(client):
    """So the phone's picker is not a second copy of the server's vocabulary."""
    body = client.get("/apply/applications?user=u1").json()
    assert "Applied" in body["statuses"] and "Offer" in body["statuses"]


# --- none of it reaches across users -------------------------------------

@pytest.mark.parametrize("path,extra", [
    ("/apply/applications/delete", {}),
    ("/apply/applications/status", {"status": "Offer"}),
    ("/apply/applications/edit", {"company": "Stolen"}),
])
def test_another_user_cannot_touch_your_application(client, path, extra):
    """Application ids are one shared AUTOINCREMENT sequence, so this is the
    difference between scoping the write and handing over whoever holds that id."""
    app_id = _one(client, _save(user="u1"), user="u1")
    got = client.post(path, json={"user": "u2", "application_id": app_id, **extra})
    assert got.json()["ok"] is False
    row = store.get_application("u1", app_id)
    assert row is not None and row["company"] == "Acme"
