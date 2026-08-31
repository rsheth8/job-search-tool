"""Email + password accounts: the second door into the invite-only beta.

Sign in with Apple depends on the Apple ID signed into the device. These tests
pin the alternative — that it mints the same kind of session, honours the same
invite allowlist, and doesn't hand out an account to anyone who guesses.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import auth, config
from app.main import app


def _client() -> TestClient:
    return TestClient(app)


def _signup(c: TestClient, email="tester@example.com", password="hunter2-beta", **kw):
    return c.post("/auth/signup", json={"email": email, "password": password, **kw})


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def test_password_hash_is_not_the_password():
    stored = auth.hash_password("correct horse battery")
    assert "correct horse battery" not in stored
    assert stored.startswith("scrypt$")
    assert auth.verify_password("correct horse battery", stored)
    assert not auth.verify_password("wrong", stored)


def test_hash_is_salted_so_equal_passwords_differ():
    a = auth.hash_password("same-password")
    b = auth.hash_password("same-password")
    assert a != b
    assert auth.verify_password("same-password", a)
    assert auth.verify_password("same-password", b)


def test_verify_never_raises_on_a_junk_row():
    for junk in (None, "", "notascheme", "scrypt$bad", "scrypt$x$y$z$zz$zz", "a$b$c$d$e$f"):
        assert auth.verify_password("anything", junk) is False


# ---------------------------------------------------------------------------
# Sign-up
# ---------------------------------------------------------------------------

def test_signup_returns_a_working_session():
    c = _client()
    res = _signup(c)
    assert res.status_code == 200
    data = res.json()
    assert data["user"]["email"] == "tester@example.com"
    assert data["user"]["id"].startswith("usr_")

    me = c.get("/auth/me", headers={"Authorization": f"Bearer {data['token']}"})
    assert me.status_code == 200
    assert me.json()["user"]["id"] == data["user"]["id"]


def test_signup_normalizes_the_email():
    c = _client()
    res = _signup(c, email="  Tester@Example.COM  ")
    assert res.status_code == 200
    assert res.json()["user"]["email"] == "tester@example.com"
    # And the normalized form is what signs in.
    assert c.post(
        "/auth/login",
        json={"email": "TESTER@example.com", "password": "hunter2-beta"},
    ).status_code == 200


def test_signup_defaults_the_display_name_to_the_local_part():
    c = _client()
    assert _signup(c, email="ada@example.com").json()["user"]["display_name"] == "ada"


def test_signup_keeps_an_explicit_display_name():
    c = _client()
    res = _signup(c, display_name="Ada Lovelace")
    assert res.json()["user"]["display_name"] == "Ada Lovelace"


def test_duplicate_signup_is_refused():
    c = _client()
    assert _signup(c).status_code == 200
    dupe = _signup(c)
    assert dupe.status_code == 409
    assert "already exists" in dupe.json()["detail"]


def test_short_password_is_refused_before_an_account_exists():
    c = _client()
    res = _signup(c, password="short")
    assert res.status_code == 400
    assert "at least" in res.json()["detail"]
    # Nothing was created, so the address is still free.
    assert _signup(c).status_code == 200


def test_absurdly_long_password_is_refused():
    """scrypt on unbounded input is free CPU for anyone who can POST."""
    c = _client()
    res = _signup(c, password="x" * (auth.MAX_PASSWORD_LENGTH + 1))
    assert res.status_code == 400
    assert "at most" in res.json()["detail"]


def test_malformed_emails_are_refused():
    c = _client()
    for bad in ("", "   ", "nope", "no@domain", "a b@example.com", "@example.com"):
        assert _signup(c, email=bad).status_code == 400, bad


def test_signup_can_be_disabled(monkeypatch):
    monkeypatch.setenv("AUTH_ALLOW_EMAIL_SIGNUP", "false")
    config.get_settings.cache_clear()
    assert _signup(_client()).status_code == 403


# ---------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------

def test_login_roundtrip():
    c = _client()
    _signup(c)
    res = c.post("/auth/login", json={"email": "tester@example.com", "password": "hunter2-beta"})
    assert res.status_code == 200
    token = res.json()["token"]
    assert c.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_login_with_the_wrong_password_is_401():
    c = _client()
    _signup(c)
    res = c.post("/auth/login", json={"email": "tester@example.com", "password": "nope-nope-nope"})
    assert res.status_code == 401


def test_login_for_an_unknown_email_is_401_not_404():
    """A missing account and a wrong password must look identical."""
    c = _client()
    res = c.post("/auth/login", json={"email": "ghost@example.com", "password": "whatever123"})
    assert res.status_code == 401
    assert res.json()["detail"] == "Wrong email or password."


def test_login_and_signup_give_separate_sessions():
    c = _client()
    first = _signup(c).json()["token"]
    second = c.post(
        "/auth/login", json={"email": "tester@example.com", "password": "hunter2-beta"}
    ).json()["token"]
    assert first != second
    # Both are live — signing up on a second device must not evict the first.
    for t in (first, second):
        assert c.get("/auth/me", headers={"Authorization": f"Bearer {t}"}).status_code == 200


def test_logout_revokes_only_that_session():
    c = _client()
    a = _signup(c).json()["token"]
    b = c.post(
        "/auth/login", json={"email": "tester@example.com", "password": "hunter2-beta"}
    ).json()["token"]
    assert c.post("/auth/logout", headers={"Authorization": f"Bearer {a}"}).status_code == 200
    assert c.get("/auth/me", headers={"Authorization": f"Bearer {a}"}).status_code == 401
    assert c.get("/auth/me", headers={"Authorization": f"Bearer {b}"}).status_code == 200


# ---------------------------------------------------------------------------
# The invite allowlist gates both doors
# ---------------------------------------------------------------------------

def test_allowlist_blocks_an_uninvited_signup(monkeypatch):
    monkeypatch.setenv("AUTH_ALLOWED_EMAILS", "invited@example.com")
    config.get_settings.cache_clear()
    res = _signup(_client(), email="stranger@example.com")
    assert res.status_code == 403
    assert "invite-only" in res.json()["detail"]


def test_allowlist_admits_an_invited_signup(monkeypatch):
    monkeypatch.setenv("AUTH_ALLOWED_EMAILS", "Invited@Example.com")
    config.get_settings.cache_clear()
    assert _signup(_client(), email="invited@example.com").status_code == 200


def test_allowlist_shrinking_locks_out_an_existing_account(monkeypatch):
    """Revoking an invite must stop the next sign-in, not just the sign-up."""
    c = _client()
    assert _signup(c, email="invited@example.com").status_code == 200
    monkeypatch.setenv("AUTH_ALLOWED_EMAILS", "someone-else@example.com")
    config.get_settings.cache_clear()
    res = c.post("/auth/login", json={"email": "invited@example.com", "password": "hunter2-beta"})
    assert res.status_code == 403


# ---------------------------------------------------------------------------
# Throttling
# ---------------------------------------------------------------------------

def test_repeated_failures_lock_the_address(monkeypatch):
    monkeypatch.setenv("AUTH_MAX_LOGIN_ATTEMPTS", "3")
    config.get_settings.cache_clear()
    auth.reset_login_throttle()
    c = _client()
    _signup(c)
    for _ in range(3):
        assert c.post(
            "/auth/login", json={"email": "tester@example.com", "password": "wrong-one"}
        ).status_code == 401
    # Even the *right* password is refused while the cool-off holds.
    res = c.post("/auth/login", json={"email": "tester@example.com", "password": "hunter2-beta"})
    assert res.status_code == 429
    assert "Try again" in res.json()["detail"]


def test_a_successful_login_clears_the_failure_count(monkeypatch):
    monkeypatch.setenv("AUTH_MAX_LOGIN_ATTEMPTS", "3")
    config.get_settings.cache_clear()
    auth.reset_login_throttle()
    c = _client()
    _signup(c)
    for _ in range(2):
        c.post("/auth/login", json={"email": "tester@example.com", "password": "wrong-one"})
    assert c.post(
        "/auth/login", json={"email": "tester@example.com", "password": "hunter2-beta"}
    ).status_code == 200
    # Counter reset: two more failures must not trip the limit.
    for _ in range(2):
        assert c.post(
            "/auth/login", json={"email": "tester@example.com", "password": "wrong-one"}
        ).status_code == 401


def test_throttle_is_per_address(monkeypatch):
    monkeypatch.setenv("AUTH_MAX_LOGIN_ATTEMPTS", "2")
    config.get_settings.cache_clear()
    auth.reset_login_throttle()
    c = _client()
    _signup(c, email="a@example.com")
    _signup(c, email="b@example.com")
    for _ in range(2):
        c.post("/auth/login", json={"email": "a@example.com", "password": "wrong-one"})
    assert c.post(
        "/auth/login", json={"email": "a@example.com", "password": "hunter2-beta"}
    ).status_code == 429
    assert c.post(
        "/auth/login", json={"email": "b@example.com", "password": "hunter2-beta"}
    ).status_code == 200


# ---------------------------------------------------------------------------
# Isolation from Apple accounts
# ---------------------------------------------------------------------------

def test_an_apple_row_is_not_signinable_by_email():
    """An Apple user sharing the address is a different account."""
    apple = auth.upsert_apple_user(
        apple_sub="apple-sub-1", email="shared@example.com", display_name="Apple User"
    )
    assert auth.get_user_by_email("shared@example.com") is None
    res = _client().post(
        "/auth/login", json={"email": "shared@example.com", "password": "hunter2-beta"}
    )
    assert res.status_code == 401
    assert auth.get_user(apple["id"])["password_hash"] is None


def test_email_signup_alongside_an_apple_row_makes_a_separate_user():
    apple = auth.upsert_apple_user(apple_sub="apple-sub-2", email="shared@example.com")
    res = _signup(_client(), email="shared@example.com")
    assert res.status_code == 200
    assert res.json()["user"]["id"] != apple["id"]


def test_email_users_are_isolated_from_each_other():
    c = _client()
    a = _signup(c, email="a@example.com").json()
    b = _signup(c, email="b@example.com").json()
    assert a["user"]["id"] != b["user"]["id"]

    ha = {"Authorization": f"Bearer {a['token']}"}
    hb = {"Authorization": f"Bearer {b['token']}"}
    assert c.post("/chat", json={"text": "applied to stripe swe"}, headers=ha).status_code == 200

    # B's transcript must not contain A's turn.
    body = c.get("/chat/history", headers=hb).json()
    text = str(body)
    assert "stripe" not in text.lower()


# ---------------------------------------------------------------------------
# Password rotation
# ---------------------------------------------------------------------------

def test_change_password_then_sign_in_with_the_new_one():
    c = _client()
    token = _signup(c).json()["token"]
    res = c.post(
        "/auth/password",
        json={"current_password": "hunter2-beta", "new_password": "a-longer-secret"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    assert c.post(
        "/auth/login", json={"email": "tester@example.com", "password": "hunter2-beta"}
    ).status_code == 401
    assert c.post(
        "/auth/login", json={"email": "tester@example.com", "password": "a-longer-secret"}
    ).status_code == 200


def test_change_password_needs_the_current_one():
    c = _client()
    token = _signup(c).json()["token"]
    res = c.post(
        "/auth/password",
        json={"current_password": "not-it-at-all", "new_password": "a-longer-secret"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 401


def test_change_password_enforces_the_length_floor():
    c = _client()
    token = _signup(c).json()["token"]
    res = c.post(
        "/auth/password",
        json={"current_password": "hunter2-beta", "new_password": "tiny"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 400


def test_change_password_requires_a_session():
    assert _client().post(
        "/auth/password",
        json={"current_password": "a", "new_password": "bbbbbbbbbb"},
    ).status_code == 401


def test_apple_account_has_no_password_to_change(monkeypatch):
    monkeypatch.setenv("AUTH_ALLOW_DEV_LOGIN", "true")
    config.get_settings.cache_clear()
    c = _client()
    token = c.post("/auth/dev", json={"display_name": "Dev"}).json()["token"]
    res = c.post(
        "/auth/password",
        json={"current_password": "x", "new_password": "bbbbbbbbbb"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 400
    assert "no password" in res.json()["detail"]


# ---------------------------------------------------------------------------
# Health reporting
# ---------------------------------------------------------------------------

def test_health_lists_the_available_sign_in_methods():
    body = _client().get("/health").json()
    assert body["auth"]["email_signup"] is True
    assert "apple" in body["auth"]["methods"]
    assert "email" in body["auth"]["methods"]


def test_health_drops_email_when_signup_is_disabled(monkeypatch):
    monkeypatch.setenv("AUTH_ALLOW_EMAIL_SIGNUP", "false")
    config.get_settings.cache_clear()
    body = _client().get("/health").json()
    assert body["auth"]["email_signup"] is False
    assert "email" not in body["auth"]["methods"]
