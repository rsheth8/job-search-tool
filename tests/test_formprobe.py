"""DOM form-probe scoring (no browser)."""
from __future__ import annotations

from app import formprobe


def test_application_signals():
    r = formprobe.probe_signals(
        labels=["Email", "First name", "Last name", "Phone"],
        button_texts=["Submit application"],
    )
    assert r["kind"] == "application"
    assert r["fillable_count"] >= 3
    assert r["submit_visible"] is True


def test_captcha_wins():
    r = formprobe.probe_signals(
        labels=["Email", "First name"],
        button_texts=["Submit"],
        captcha_hit=True,
    )
    assert r["kind"] == "captcha"
    assert r["blocker_reason"]


def test_login_wall():
    r = formprobe.probe_signals(
        labels=["Email"],
        button_texts=["Sign in"],
        has_password=True,
    )
    assert r["kind"] == "login"


def test_jd_with_apply_reveal():
    r = formprobe.probe_signals(
        labels=[],
        button_texts=["Apply for this job"],
    )
    assert r["kind"] == "unknown"
    assert r["reveal_label"]


def test_advance_vs_submit():
    assert formprobe.is_advance_text("Next")
    assert formprobe.is_advance_text("Continue")
    assert not formprobe.is_advance_text("Submit application")
    assert formprobe.is_submit_text("Submit application")
    assert not formprobe.is_submit_text("Next")


def test_known_ats_boost():
    weak = formprobe.probe_signals(labels=["Email"], button_texts=[])
    strong = formprobe.probe_signals(labels=["Email"], button_texts=[], known_ats=True)
    assert strong["score"] > weak["score"]


def test_payload_versioned():
    p = formprobe.payload()
    assert "version" in p
    assert "advance" in p


def test_generic_html_form_is_an_application():
    r = formprobe.probe_signals(
        labels=["First name", "Last name", "Email", "Phone"],
        button_texts=["Submit application"],
    )
    assert r["kind"] == "application"
    assert r["blocker_reason"] is None
