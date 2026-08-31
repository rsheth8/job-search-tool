"""Push notifications: the device registry, the gate, and the two triggers.

Push is the difference between a tool you check and one that tells you. It's also
an external service that can be down, misconfigured, or handed a dead token — so
the rule everywhere here is that a notification failure can never cost the thing
that triggered it. These tests pin that harder than they pin delivery.

Nothing here talks to Apple: `configured()` is false without the APNs settings, so
the whole module no-ops, and the one test that exercises delivery stubs the HTTP
call. The signing libraries (cryptography, h2) aren't even required to run this.
"""
from __future__ import annotations

import pytest

from app import config, push


@pytest.fixture(autouse=True)
def _reset():
    push.reset_for_tests()
    yield
    push.reset_for_tests()


def _configure(monkeypatch, tmp_path, **overrides):
    """Turn push on with plausible settings (no real key material)."""
    key = tmp_path / "apns.p8"
    key.write_text("-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----\n")
    env = {"PUSH_ENABLED": "true", "APNS_KEY_ID": "ABC123",
           "APNS_TEAM_ID": "TEAM456", "APNS_BUNDLE_ID": "com.rahil.jobpilot",
           "APNS_KEY_PATH": str(key)}
    env.update(overrides)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    config.get_settings.cache_clear()


# --- the registry -----------------------------------------------------------

def test_register_and_list():
    assert push.register_device("u1", "tok-a") is True
    assert push.register_device("u1", "tok-b") is True
    assert set(push.devices("u1")) == {"tok-a", "tok-b"}
    assert push.devices("u2") == []


def test_registering_the_same_token_twice_does_not_duplicate():
    """iOS hands the app the same token on every launch."""
    push.register_device("u1", "tok-a")
    push.register_device("u1", "tok-a")
    assert push.devices("u1") == ["tok-a"]


def test_register_rejects_junk():
    assert push.register_device("u1", "") is False
    assert push.register_device("u1", "   ") is False
    assert push.register_device("", "tok") is False
    assert push.devices("u1") == []


def test_forget_device():
    push.register_device("u1", "tok-a")
    assert push.forget_device("u1", "tok-a") is True
    assert push.forget_device("u1", "tok-a") is False
    assert push.devices("u1") == []


# --- the gate ---------------------------------------------------------------

def test_not_configured_by_default():
    assert push.configured() is False


def test_send_is_a_no_op_when_unconfigured():
    push.register_device("u1", "tok-a")
    assert push.send("u1", "Title", "Body") == 0


def test_configured_needs_every_setting(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    assert push.configured() is True
    # drop any one of them and it goes dark again
    for missing in ("APNS_KEY_ID", "APNS_TEAM_ID", "APNS_BUNDLE_ID", "APNS_KEY_PATH"):
        _configure(monkeypatch, tmp_path, **{missing: ""})
        assert push.configured() is False, f"{missing} should be required"


def test_enabled_flag_alone_is_not_enough(monkeypatch):
    monkeypatch.setenv("PUSH_ENABLED", "true")
    config.get_settings.cache_clear()
    assert push.configured() is False


def test_send_with_no_registered_devices(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    assert push.send("nobody", "Title", "Body") == 0


# --- failure never propagates ----------------------------------------------

def test_a_bad_signing_key_does_not_raise(monkeypatch, tmp_path):
    """The .p8 here is nonsense, so signing throws. The caller must not see it."""
    _configure(monkeypatch, tmp_path)
    push.register_device("u1", "tok-a")
    assert push.send("u1", "Title", "Body") == 0      # logged, not raised


def test_a_missing_key_file_does_not_raise(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, APNS_KEY_PATH=str(tmp_path / "nope.p8"))
    push.register_device("u1", "tok-a")
    assert push.send("u1", "Title", "Body") == 0


def test_delivery_failure_for_one_device_does_not_stop_the_others(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    push.register_device("u1", "good-1")
    push.register_device("u1", "bad")
    push.register_device("u1", "good-2")
    monkeypatch.setattr(push, "_auth_header", lambda: "bearer stub")

    def fake_post(token, payload, auth):
        if token == "bad":
            raise RuntimeError("connection reset")
        return True

    monkeypatch.setattr(push, "_post", fake_post)
    assert push.send("u1", "Title", "Body") == 2


def test_a_dead_token_is_dropped(monkeypatch, tmp_path):
    """A reinstalled app leaves a token APNs rejects forever; stop retrying it."""
    _configure(monkeypatch, tmp_path)
    push.register_device("u1", "dead")
    push.register_device("u1", "live")
    monkeypatch.setattr(push, "_auth_header", lambda: "bearer stub")

    class FakeResponse:
        def __init__(self, code, reason=""):
            self.status_code = code
            self._reason = reason

        def json(self):
            return {"reason": self._reason}

    class FakeClient:
        def __init__(self, **_kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def post(self, url, **_kw):
            return (FakeResponse(410, "Unregistered") if url.endswith("dead")
                    else FakeResponse(200))

    import httpx
    monkeypatch.setattr(httpx, "Client", FakeClient)

    assert push.send("u1", "Title", "Body") == 1
    assert push.devices("u1") == ["live"]      # the dead one is gone


def test_the_auth_token_is_cached(monkeypatch, tmp_path):
    """APNs rejects a JWT regenerated too often, so this must not be per-send."""
    _configure(monkeypatch, tmp_path)
    push.register_device("u1", "tok")
    calls = []
    monkeypatch.setattr(push, "_post", lambda *a, **k: True)

    import jwt
    real_encode = jwt.encode
    monkeypatch.setattr(jwt, "encode",
                        lambda *a, **k: calls.append(1) or "stub-token")
    try:
        push.send("u1", "a", "b")
        push.send("u1", "c", "d")
    finally:
        monkeypatch.setattr(jwt, "encode", real_encode)
    assert len(calls) == 1, "the APNs JWT should be signed once, then reused"


# --- push triggers ----------------------------------------------------------

def test_notify_new_matches_is_silent_for_zero(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    push.register_device("u1", "tok")
    monkeypatch.setattr(push, "send", lambda *a, **k: 1)
    assert push.notify_new_matches("u1", 0) == 0


# --- the endpoints ----------------------------------------------------------

def test_device_registration_endpoints():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    body = client.post("/apply/device",
                       json={"user": "u1", "token": "tok-a"}).json()
    assert body["ok"] is True
    assert body["configured"] is False       # tells the app pushes won't arrive yet
    assert push.devices("u1") == ["tok-a"]

    assert client.post("/apply/device/remove",
                       json={"user": "u1", "token": "tok-a"}).json()["ok"] is True
    assert push.devices("u1") == []


def test_device_registration_stores_timezone():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    client.post("/apply/device", json={
        "user": "u1", "token": "tok-tz", "timezone": "America/New_York",
    })
    from app import voice
    assert voice.timezone_for("u1") == "America/New_York"


# --- the key can arrive as a secret, not only as a file ----------------------
#
# APNS_KEY_PATH means getting a .p8 onto the Fly volume over SSH. APNS_KEY_PEM
# is one `fly secrets set`, which is both easier and a better home for a signing
# key. Both must work, and a deployment already using the path must not move.

_PEM = "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----"


def test_pem_alone_configures_push(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, APNS_KEY_PATH="", APNS_KEY_PEM=_PEM)
    assert push.configured() is True
    assert config.get_settings().apns_key_source == "pem"


def test_neither_key_form_leaves_push_dark(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, APNS_KEY_PATH="", APNS_KEY_PEM="")
    assert push.configured() is False
    assert config.get_settings().apns_key_source == ""


def test_a_path_still_wins_so_nothing_deployed_moves(monkeypatch, tmp_path):
    """An existing deployment sets only the path; adding the new setting later
    must not quietly start signing with something else."""
    _configure(monkeypatch, tmp_path, APNS_KEY_PEM="ignored-pem")
    s = config.get_settings()
    assert s.apns_key_source == "path"
    assert "not-a-real-key" in push._signing_key(s)
    assert "ignored-pem" not in push._signing_key(s)


def test_pem_is_read_verbatim(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, APNS_KEY_PATH="", APNS_KEY_PEM=_PEM)
    assert push._signing_key(config.get_settings()) == _PEM + "\n"


def test_an_escaped_pem_is_unfolded(monkeypatch, tmp_path):
    """A shell that escapes the newlines produces a PEM that looks right in
    `fly secrets` and fails deep inside PyJWT. Unfold it rather than making
    someone debug that."""
    _configure(monkeypatch, tmp_path, APNS_KEY_PATH="",
               APNS_KEY_PEM=_PEM.replace("\n", "\\n"))
    assert push._signing_key(config.get_settings()) == _PEM + "\n"


def test_a_junk_pem_does_not_raise(monkeypatch, tmp_path):
    """Same contract as a junk file: signing throws, the caller never sees it."""
    _configure(monkeypatch, tmp_path, APNS_KEY_PATH="", APNS_KEY_PEM="not a pem")
    push.register_device("u1", "tok-a")
    assert push.send("u1", "Title", "Body") == 0
