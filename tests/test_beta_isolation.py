"""Two testers on one deployment must never see or touch each other's data.

Most per-user tables are read by primary key (``WHERE id = ?``) with no user
filter, which is safe only because every caller resolves ownership first. That's
a convention, and conventions rot. These tests hold the line from the outside:
real HTTP requests, one session per user, B's integer ids handed to A.

Also pins the documented rule that a session always wins over a ``user=``
parameter -- otherwise any signed-in tester could read any account by guessing
a user id.
"""
from __future__ import annotations

from app import auth, config, jobstore, profile, store
from app.jobsources import JobPosting


def _session(uid, monkeypatch):
    monkeypatch.setenv("AUTH_ALLOW_DEV_LOGIN", "true")
    config.get_settings.cache_clear()
    return auth.sign_in_dev(user_id=uid)["token"]


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


def _posting(ext, title, company):
    return JobPosting("greenhouse", ext, title, f"https://x/{ext}",
                      company=company, location="Remote",
                      description="python kubernetes aws")


def _seed(uid, company, title):
    profile.set_profile(uid, roles="software engineer", keywords="python",
                        locations="Remote")
    row = jobstore.save_posting(uid, _posting(f"{uid}-1", title, company),
                                relevance_score=0.9, status="queued")
    store.create_application(uid, company, title, status="Applied")
    return row["id"]


def _two_users(monkeypatch):
    a_tok = _session("usr_a", monkeypatch)
    b_tok = _session("usr_b", monkeypatch)
    a_pid = _seed("usr_a", "Alpha Corp", "A Engineer")
    b_pid = _seed("usr_b", "Bravo Corp", "B Engineer")
    return a_tok, b_tok, a_pid, b_pid


def _client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


# --- reads ---------------------------------------------------------------

def test_apply_data_shows_only_your_own_matches(monkeypatch):
    a_tok, b_tok, _, _ = _two_users(monkeypatch)
    c = _client()
    a = c.get("/apply/data", headers=_hdr(a_tok))
    assert a.status_code == 200, a.text
    assert a.json()["user"] == "usr_a"
    assert "Bravo Corp" not in a.text, "user A can see user B's postings"
    b = c.get("/apply/data", headers=_hdr(b_tok))
    assert "Alpha Corp" not in b.text, "user B can see user A's postings"


def test_applications_list_is_per_user(monkeypatch):
    a_tok, b_tok, _, _ = _two_users(monkeypatch)
    c = _client()
    a = c.get("/apply/applications", headers=_hdr(a_tok)).text
    b = c.get("/apply/applications", headers=_hdr(b_tok)).text
    assert "Alpha Corp" in a and "Bravo Corp" not in a
    assert "Bravo Corp" in b and "Alpha Corp" not in b


def test_chat_history_is_per_user(monkeypatch):
    a_tok, b_tok, _, _ = _two_users(monkeypatch)
    c = _client()
    c.post("/chat", json={"text": "applied to alpha corp"}, headers=_hdr(a_tok))
    b_hist = c.get("/chat/history", headers=_hdr(b_tok))
    assert b_hist.status_code == 200
    assert "alpha" not in b_hist.text.lower(), "user B can read user A's chat"


def test_identity_is_per_user(monkeypatch):
    a_tok, b_tok, _, _ = _two_users(monkeypatch)
    c = _client()
    c.post("/apply/identity", json={"fields": {"first_name": "Aaa", "email": "a@x.com"}},
           headers=_hdr(a_tok))
    b = c.get("/apply/identity", headers=_hdr(b_tok))
    assert b.status_code == 200
    assert "Aaa" not in b.text and "a@x.com" not in b.text


# --- writes against someone else's integer id ----------------------------

def test_a_cannot_dismiss_bs_posting(monkeypatch):
    a_tok, _, _, b_pid = _two_users(monkeypatch)
    before = jobstore.get_posting("usr_b", b_pid)["status"]
    r = _client().post("/apply/pass", json={"posting_id": b_pid}, headers=_hdr(a_tok))
    assert r.json().get("ok") is False, "reported success on someone else's posting"
    assert jobstore.get_posting("usr_b", b_pid)["status"] == before


def test_a_cannot_snooze_bs_posting(monkeypatch):
    a_tok, _, _, b_pid = _two_users(monkeypatch)
    before = jobstore.get_posting("usr_b", b_pid)["status"]
    r = _client().post("/apply/snooze", json={"posting_id": b_pid, "days": 7},
                       headers=_hdr(a_tok))
    assert r.json().get("ok") is False
    row = jobstore.get_posting("usr_b", b_pid)
    assert row["status"] == before and row["snoozed_until"] is None


def test_a_cannot_mark_bs_posting_applied(monkeypatch):
    a_tok, _, _, b_pid = _two_users(monkeypatch)
    before = len(store.list_applications("usr_b"))
    r = _client().post("/apply/applied", json={"posting_id": b_pid},
                       headers=_hdr(a_tok))
    assert r.json().get("ok") is False
    assert len(store.list_applications("usr_b")) == before
    assert len(store.list_applications("usr_a")) == 1, "logged B's job under A"


def test_a_cannot_remove_or_promote_bs_posting(monkeypatch):
    a_tok, _, _, b_pid = _two_users(monkeypatch)
    c = _client()
    c.post("/apply/remove", json={"posting_id": b_pid}, headers=_hdr(a_tok))
    c.post("/apply/promote", json={"posting_id": b_pid}, headers=_hdr(a_tok))
    assert jobstore.get_posting("usr_b", b_pid) is not None


def test_a_cannot_save_an_answer_onto_bs_posting(monkeypatch):
    a_tok, _, _, b_pid = _two_users(monkeypatch)
    r = _client().post("/apply/answer/save",
                       json={"posting_id": b_pid, "index": 0, "answer": "pwned"},
                       headers=_hdr(a_tok))
    # Either refused outright or a no-op; what matters is B's data is untouched.
    assert r.status_code != 200 or r.json().get("ok") is not True


def test_a_cannot_download_bs_resume_or_cover(monkeypatch):
    a_tok, _, _, b_pid = _two_users(monkeypatch)
    c = _client()
    for path in ("/apply/resume", "/apply/cover"):
        r = c.get(f"{path}?id={b_pid}", headers=_hdr(a_tok))
        assert r.status_code != 200 or not r.content, \
            f"{path} served another user's document"


# --- the session-wins rule ----------------------------------------------

def test_a_session_cannot_be_overridden_by_a_user_parameter(monkeypatch):
    """auth.resolve_user documents "session always wins". If a query parameter
    could override it, every signed-in tester could read any account."""
    a_tok, _, _, _ = _two_users(monkeypatch)
    c = _client()
    r = c.get("/apply/data?user=usr_b", headers=_hdr(a_tok))
    assert r.status_code == 200, r.text
    assert r.json()["user"] == "usr_a"
    assert "Bravo Corp" not in r.text


def test_a_session_cannot_be_overridden_by_a_body_user(monkeypatch):
    a_tok, _, _, _ = _two_users(monkeypatch)
    r = _client().post("/apply/identity",
                       json={"user": "usr_b", "fields": {"first_name": "Aaa"}},
                       headers=_hdr(a_tok))
    assert r.status_code == 200, r.text
    assert r.json()["user"] == "usr_a"
    from app import applicant
    assert applicant.autofill_map("usr_b").get("first_name") != "Aaa"


def test_unauthenticated_requests_are_refused_when_fail_closed(monkeypatch):
    _two_users(monkeypatch)
    monkeypatch.setenv("AUTH_FAIL_OPEN", "false")
    monkeypatch.setenv("APPLY_API_TOKEN", "")
    config.get_settings.cache_clear()
    c = _client()
    for path in ("/apply/data", "/apply/applications", "/chat/history"):
        assert c.get(path).status_code == 401, f"{path} answered without a session"


def test_a_revoked_session_stops_working(monkeypatch):
    a_tok, _, _, _ = _two_users(monkeypatch)
    monkeypatch.setenv("AUTH_FAIL_OPEN", "false")
    config.get_settings.cache_clear()
    c = _client()
    assert c.get("/apply/applications", headers=_hdr(a_tok)).status_code == 200
    auth.revoke_session(a_tok)
    assert c.get("/apply/applications", headers=_hdr(a_tok)).status_code == 401
