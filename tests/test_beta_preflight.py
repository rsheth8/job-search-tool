"""The invite-beta preflight verdict.

The point of the script is that "verify it, don't assume it" should not itself
be a thing you do by eye. So what counts as a blocker — the settings that decide
whether one tester can read another's pipeline — is pinned here rather than left
to whoever reads the output.
"""
from __future__ import annotations

import pytest

from scripts.beta_preflight import FAIL, OK, WARN, evaluate


def _health(**over) -> dict:
    """A deployment that is safe to invite people to."""
    base = {
        "status": "ok",
        "db_ok": True,
        "db": "/data/job_search.db",
        "reminder_delivery": "app",
        "auth": {
            "fail_open": False,
            "dev_login": False,
            "allowlist": True,
            "sentry": True,
            "email_signup": True,
            "methods": ["apple", "email"],
        },
        "beta": {"invite_ready": True, "llm_ready": True},
        "llm": {"problem": None, "model": "claude-haiku-4-5"},
        "dependencies": {"missing": []},
    }
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return base


def _marks(rep) -> dict[str, str]:
    return {name: mark for mark, name, _ in rep.rows}


def test_a_ready_deployment_has_no_blockers():
    rep = evaluate(_health())
    assert rep.blocking_failures == 0
    assert all(m != FAIL for m, _, _ in rep.rows)


# ---------------------------------------------------------------------------
# Blockers — each of these means one tester could reach another's data
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("patch,name", [
    ({"auth": {"fail_open": True}}, "AUTH_FAIL_OPEN is off"),
    ({"auth": {"dev_login": True}}, "Dev login is off"),
    ({"auth": {"allowlist": False}}, "Invite allowlist is set"),
    ({"beta": {"invite_ready": False}}, "beta.invite_ready"),
    ({"db_ok": False}, "Database writable"),
    ({"reminder_delivery": "log"}, "Reminders deliver in-app"),
])
def test_each_isolation_setting_blocks(patch, name):
    rep = evaluate(_health(**patch))
    assert rep.blocking_failures >= 1
    assert _marks(rep)[name] == FAIL


def test_open_email_signup_without_an_allowlist_is_its_own_blocker():
    """Both doors share the allowlist, so an empty one plus email sign-up means
    anyone who finds the URL can create an account — no Apple ID required."""
    rep = evaluate(_health(auth={"allowlist": False, "email_signup": True}))
    assert _marks(rep)["Email sign-up is open"] == FAIL


def test_email_signup_with_an_allowlist_is_fine():
    rep = evaluate(_health(auth={"allowlist": True, "email_signup": True}))
    assert "Email sign-up is open" not in _marks(rep)
    assert rep.blocking_failures == 0


def test_apple_only_deployment_is_not_flagged_for_email():
    rep = evaluate(_health(auth={"email_signup": False, "methods": ["apple"]}))
    assert "Email sign-up is open" not in _marks(rep)


# ---------------------------------------------------------------------------
# Advisories — degraded, not dangerous. These must NOT block.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("patch,name", [
    ({"llm": {"problem": "ANTHROPIC_API_KEY is not set"}}, "Anthropic key configured"),
    ({"beta": {"llm_ready": False}}, "beta.llm_ready"),
    ({"dependencies": {"missing": ["tectonic"]}}, "Python dependencies present"),
    ({"auth": {"sentry": False}}, "Sentry DSN set"),
])
def test_degraded_features_warn_but_do_not_block(patch, name):
    rep = evaluate(_health(**patch))
    assert _marks(rep)[name] == WARN
    assert rep.blocking_failures == 0


_RESUME_OK = {"enabled": True, "dir": "/data/resumes",
              "bases": ["aiml.tex", "swe.tex"],
              "expected": ["aiml.tex", "swe.tex"]}


def test_no_base_resumes_uploaded_warns():
    """`bases or expected` would swallow the empty list — the one case that
    matters, since an empty volume is what "did the upload land?" is asking."""
    rep = evaluate(_health(resume={**_RESUME_OK, "bases": []}))
    assert _marks(rep)["Base résumés on the volume"] == WARN
    assert rep.blocking_failures == 0


def test_a_partial_resume_upload_warns():
    rep = evaluate(_health(resume={**_RESUME_OK, "bases": ["swe.tex"]}))
    assert _marks(rep)["Base résumés on the volume"] == WARN


def test_present_base_resumes_pass():
    rep = evaluate(_health(resume=_RESUME_OK))
    assert _marks(rep)["Base résumés on the volume"] == OK


def test_resume_check_skipped_when_tailoring_is_disabled():
    rep = evaluate(_health(resume={**_RESUME_OK, "enabled": False, "bases": []}))
    assert "Base résumés on the volume" not in _marks(rep)


def test_push_inactive_warns_and_names_what_is_missing():
    rep = evaluate(_health(push={"enabled": True, "active": False,
                                 "missing": ["APNS_KEY_PATH"], "sandbox": False}))
    assert _marks(rep)["Push (APNs) active"] == WARN
    assert "APNS_KEY_PATH" in dict(
        (name, detail) for _, name, detail in rep.rows)["Push (APNs) active"]
    assert rep.blocking_failures == 0


def test_push_on_the_sandbox_host_is_called_out():
    """TestFlight ships the production entitlement; a sandbox host means every
    push is silently rejected."""
    rep = evaluate(_health(push={"enabled": True, "active": True,
                                 "missing": [], "sandbox": True}))
    assert _marks(rep)["Push (APNs) active"] == OK
    assert _marks(rep)["APNs host"] == WARN


def test_push_active_on_production_host_is_clean():
    rep = evaluate(_health(push={"enabled": True, "active": True,
                                 "missing": [], "sandbox": False}))
    assert _marks(rep)["Push (APNs) active"] == OK
    assert "APNs host" not in _marks(rep)


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

def test_an_empty_payload_blocks_rather_than_crashing():
    """A server that answers 200 with nothing useful must not read as ready."""
    rep = evaluate({})
    assert rep.blocking_failures >= 1


def test_every_row_is_renderable():
    rep = evaluate(_health())
    rep.render()  # would raise on an empty rows list or a bad format
    assert rep.rows
