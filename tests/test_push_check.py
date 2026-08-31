"""Tests for the push diagnostic.

The point of `scripts.push_check` is that push fails *silently* — `send` is
fail-open, so a broken deployment and a quiet one look the same from outside. So
what these pin is the diagnosis: that every way push can be dark is named, that a
real APNs refusal survives all the way to the operator's terminal with its reason
string intact, and that the exit code is honest enough to gate a deploy on.
"""
from __future__ import annotations

import pytest

from app import config, push
from scripts import push_check


@pytest.fixture(autouse=True)
def _reset():
    push.reset_for_tests()
    yield
    push.reset_for_tests()


def _configure(monkeypatch, **overrides):
    env = {"PUSH_ENABLED": "true", "APNS_KEY_ID": "ABC123",
           "APNS_TEAM_ID": "TEAM456", "APNS_BUNDLE_ID": "com.rahil.jobpilot",
           "APNS_KEY_PEM": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----"}
    env.update(overrides)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    config.get_settings.cache_clear()


def _blockers(monkeypatch, **overrides) -> str:
    _configure(monkeypatch, **overrides)
    return " | ".join(push_check.diagnose()["blockers"])


# --- every way push can be dark gets named ----------------------------------

def test_a_full_configuration_has_no_blockers(monkeypatch):
    _configure(monkeypatch)
    info = push_check.diagnose()
    assert info["blockers"] == []
    assert info["configured"] is True


def test_the_switch_being_off_is_a_blocker(monkeypatch):
    assert "PUSH_ENABLED" in _blockers(monkeypatch, PUSH_ENABLED="false")


@pytest.mark.parametrize("name", ["APNS_KEY_ID", "APNS_TEAM_ID", "APNS_BUNDLE_ID"])
def test_each_missing_setting_is_named(monkeypatch, name):
    assert f"{name} is not set" in _blockers(monkeypatch, **{name: ""})


def test_no_key_names_both_ways_of_supplying_one(monkeypatch):
    out = _blockers(monkeypatch, APNS_KEY_PEM="", APNS_KEY_PATH="")
    assert "APNS_KEY_PEM" in out and "APNS_KEY_PATH" in out


def test_a_missing_library_is_a_blocker(monkeypatch):
    """h2 in particular: httpx raises inside the per-device try, which swallows
    it, so the only symptom is zero deliveries."""
    _configure(monkeypatch)
    monkeypatch.setattr(push_check, "_have", lambda mod: mod != "h2")
    assert "h2" in " | ".join(push_check.diagnose()["blockers"])


# --- the environment is reported, because it is the usual culprit ------------

def test_sandbox_selects_the_sandbox_host(monkeypatch):
    _configure(monkeypatch, APNS_USE_SANDBOX="true")
    info = push_check.diagnose()
    assert info["sandbox"] is True
    assert info["host"] == push._HOST_SANDBOX


def test_production_is_the_default_host(monkeypatch):
    _configure(monkeypatch)
    assert push_check.diagnose()["host"] == push._HOST_PROD


# --- devices ----------------------------------------------------------------

def test_devices_are_listed_per_user(monkeypatch):
    _configure(monkeypatch)
    push.register_device("u1", "tok-a")
    push.register_device("u1", "tok-b")
    push.register_device("u2", "tok-c")
    assert set(push_check.diagnose("u1")["tokens"]) == {"tok-a", "tok-b"}
    assert dict(push_check.users_with_devices()) == {"u1": 2, "u2": 1}


def test_no_devices_is_not_a_configuration_blocker(monkeypatch):
    """Nothing is wrong with the deployment — nobody has opened the app."""
    _configure(monkeypatch)
    info = push_check.diagnose("nobody")
    assert info["tokens"] == [] and info["blockers"] == []


def test_a_token_is_masked_not_printed(monkeypatch):
    """These end up in terminal scrollback and CI logs."""
    masked = push_check._mask("a" * 64)
    assert masked != "a" * 64
    assert masked.startswith("aaaaaa") and masked.endswith("aaaa")


# --- the APNs reply survives to the operator --------------------------------

def test_a_refusal_reaches_the_caller_with_its_reason(monkeypatch):
    _configure(monkeypatch)
    push.register_device("u1", "tok-a")
    monkeypatch.setattr(push, "_auth_header", lambda: "bearer x")

    def refuse(token, payload, auth):
        push.logger.warning("push: APNs %s %s", 400, "BadDeviceToken")
        return False

    monkeypatch.setattr(push, "_post", refuse)
    sent, lines = push_check.send_test("u1", "T", "B")
    assert sent == 0
    assert any("BadDeviceToken" in line for line in lines)


def test_a_delivery_is_counted(monkeypatch):
    _configure(monkeypatch)
    push.register_device("u1", "tok-a")
    monkeypatch.setattr(push, "_auth_header", lambda: "bearer x")
    monkeypatch.setattr(push, "_post", lambda *a, **k: True)
    sent, _ = push_check.send_test("u1", "T", "B")
    assert sent == 1


def test_capturing_the_log_leaves_the_logger_as_it_found_it(monkeypatch):
    _configure(monkeypatch)
    before_handlers = list(push.logger.handlers)
    before_level = push.logger.level
    push_check.send_test("nobody", "T", "B")
    assert push.logger.handlers == before_handlers
    assert push.logger.level == before_level


def test_every_hint_names_a_real_apns_reason():
    """A hint keyed on a reason string APNs never sends would never fire."""
    assert set(push._DEAD_TOKEN_REASONS) <= set(push_check._HINTS)


# --- exit codes, so this can gate a deploy ----------------------------------

def _main(monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr("sys.argv", ["push_check", *argv])
    return push_check.main()


def test_list_with_no_devices_fails(monkeypatch, capsys):
    _configure(monkeypatch)
    assert _main(monkeypatch, ["--list"]) == 1
    assert "No devices registered" in capsys.readouterr().out


def test_list_with_devices_succeeds(monkeypatch, capsys):
    _configure(monkeypatch)
    push.register_device("u1", "tok-a")
    assert _main(monkeypatch, ["--list"]) == 0
    assert "u1" in capsys.readouterr().out


def test_check_fails_when_unconfigured(monkeypatch):
    _configure(monkeypatch, PUSH_ENABLED="false")
    push.register_device("u1", "tok-a")
    assert _main(monkeypatch, ["--user", "u1", "--check"]) == 1


def test_check_passes_when_ready(monkeypatch):
    _configure(monkeypatch)
    push.register_device("u1", "tok-a")
    assert _main(monkeypatch, ["--user", "u1", "--check"]) == 0


def test_check_sends_nothing(monkeypatch):
    _configure(monkeypatch)
    push.register_device("u1", "tok-a")
    calls = []
    monkeypatch.setattr(push, "_post", lambda *a, **k: calls.append(a) or True)
    _main(monkeypatch, ["--user", "u1", "--check"])
    assert calls == []


def test_a_blocked_run_refuses_to_send(monkeypatch, capsys):
    _configure(monkeypatch, APNS_KEY_ID="")
    push.register_device("u1", "tok-a")
    calls = []
    monkeypatch.setattr(push, "_post", lambda *a, **k: calls.append(a) or True)
    assert _main(monkeypatch, ["--user", "u1"]) == 1
    assert calls == []
    assert "Not sending" in capsys.readouterr().out


def test_a_successful_send_exits_zero_and_prints_the_hint(monkeypatch, capsys):
    _configure(monkeypatch)
    push.register_device("u1", "tok-a")
    monkeypatch.setattr(push, "_auth_header", lambda: "bearer x")
    monkeypatch.setattr(push, "_post", lambda *a, **k: True)
    assert _main(monkeypatch, ["--user", "u1"]) == 0
    assert "Delivered to 1" in capsys.readouterr().out


def test_a_refused_send_exits_nonzero_with_the_explanation(monkeypatch, capsys):
    _configure(monkeypatch)
    push.register_device("u1", "tok-a")
    monkeypatch.setattr(push, "_auth_header", lambda: "bearer x")

    def refuse(token, payload, auth):
        push.logger.warning("push: APNs %s %s", 400, "BadDeviceToken")
        return False

    monkeypatch.setattr(push, "_post", refuse)
    assert _main(monkeypatch, ["--user", "u1"]) == 1
    out = capsys.readouterr().out
    assert "BadDeviceToken" in out
    assert "APNS_USE_SANDBOX" in out  # the hint, not just the reason
