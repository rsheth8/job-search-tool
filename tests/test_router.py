from app.intents import Intent
from app.router import HeuristicRouter, normalize_status

r = HeuristicRouter()


def test_apply_with_company_and_role():
    p = r.parse("applied spotify swe ii")
    assert p.intent == Intent.APPLY
    assert p.company == "Spotify"
    assert p.role and "swe" in p.role.lower()
    assert p.is_high()


def test_apply_natural_language():
    p = r.parse("i just applied to figma backend engineer")
    assert p.intent == Intent.APPLY
    assert p.company == "Figma"
    assert "backend" in (p.role or "").lower()


def test_apply_bare_is_low_confidence():
    p = r.parse("applied")
    assert p.intent == Intent.APPLY
    assert p.company is None
    assert p.is_low()


def test_update_with_status():
    p = r.parse("spotify update oa received")
    assert p.intent == Intent.UPDATE
    assert p.company == "Spotify"
    assert p.status == "OA received"


def test_update_without_status_is_medium():
    p = r.parse("spotify update")
    assert p.intent == Intent.UPDATE
    assert p.company == "Spotify"
    assert p.status is None
    assert p.is_medium()


def test_note():
    p = r.parse("note spotify recruiter seemed positive")
    assert p.intent == Intent.NOTE
    assert p.company == "Spotify"
    assert "recruiter" in (p.message or "")


def test_remind():
    p = r.parse("remind spotify in 3 days")
    assert p.intent == Intent.REMIND
    assert p.company == "Spotify"
    assert "3 days" in (p.time_reference or "")


def test_remind_with_filler_words():
    # "me", "about" must not leak into the company name.
    p = r.parse("remind me about spotify in 3 days")
    assert p.intent == Intent.REMIND
    assert p.company == "Spotify"


def test_query():
    p = r.parse("what should I follow up on")
    assert p.intent == Intent.QUERY


def test_unknown():
    p = r.parse("asdf qwerty zzz")
    assert p.intent == Intent.UNKNOWN


def test_normalize_status():
    assert normalize_status("oa received") == "OA received"
    assert normalize_status("got rejected") == "Rejected"
    assert normalize_status("interview next week") == "Interview"
    assert normalize_status(None) is None
