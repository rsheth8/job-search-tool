"""Matcher: free pre-filter + scoring (heuristic path is what CI exercises)."""
from __future__ import annotations

from app import matcher, profile
from app.jobsources import JobPosting


def _p(title, location="", desc=""):
    return JobPosting(
        source="greenhouse", external_id=title, title=title, url="https://x",
        company="Acme", location=location, description=desc,
    )


def _profile(**kw):
    profile.set_profile("u", **kw)
    return profile.get_profile("u")


def test_prefilter_keeps_only_term_matches():
    prof = _profile(roles="software engineer, backend", keywords="python")
    posts = [_p("Backend Software Engineer"), _p("Sales Director"), _p("Python Developer")]
    kept = {p.title for p in matcher.prefilter(posts, prof)}
    assert kept == {"Backend Software Engineer", "Python Developer"}


def test_prefilter_passes_all_when_no_terms():
    posts = [_p("Anything"), _p("Whatever")]
    assert len(matcher.prefilter(posts, None)) == 2


def test_prefilter_expands_abbreviations_and_drops_filler():
    # Regression: a phrasey profile must still surface real roles. "swe" should
    # match "Software Engineer", and filler words ("new grad roles") must not gate.
    prof = _profile(roles="new grad swe roles, remote or nyc",
                    keywords="new grad swe roles, remote or nyc")
    posts = [_p("Senior Software Engineer", desc="backend"),
             _p("Marketing Manager", desc="brand")]
    kept = {p.title for p in matcher.prefilter(posts, prof)}
    assert "Senior Software Engineer" in kept
    assert "Marketing Manager" not in kept


def test_match_terms_vs_score_terms():
    prof = _profile(roles="swe", keywords="swe")
    # Match terms expand the abbreviation; scoring terms stay literal.
    assert "software engineer" in matcher._match_terms(prof)
    assert matcher._terms(prof) == {"swe"}


def test_generic_words_only_match_inside_phrases():
    # Literal "software engineer" in a profile must NOT reintroduce the bare
    # "software"/"engineer" tokens that matched software-company sales roles.
    prof = _profile(roles="software engineer, machine learning engineer",
                    keywords="python")
    terms = matcher._match_terms(prof)
    assert "software engineer" in terms and "machine learning engineer" in terms
    assert "software" not in terms and "engineer" not in terms
    assert "python" in terms
    # A software-company sales role still must not pass.
    sales = _p("Enterprise Account Executive", desc="sell our software")
    assert sales not in matcher.prefilter([sales], prof)
    # A real SWE role still passes (via the phrase).
    swe = _p("Software Engineer, Backend")
    assert swe in matcher.prefilter([swe], prof)


def test_swe_does_not_expand_to_bare_software():
    # Regression: bare "software" matched every software-company posting (incl.
    # their sales roles), flooding the scoring cap. Only the precise phrase.
    prof = _profile(roles="swe", keywords="swe")
    assert "software" not in matcher._match_terms(prof)
    # A software-company SALES role must NOT pass the prefilter.
    sales = _p("Commercial Account Executive", desc="Sell our software platform")
    assert sales not in matcher.prefilter([sales], prof)


def test_heuristic_scores_match_higher_than_nonmatch():
    prof = _profile(roles="software engineer", keywords="python, distributed systems")
    scored = dict(
        (p.title, s)
        for p, s in matcher.score(
            [_p("Software Engineer", desc="python distributed systems"),
             _p("Software Engineer", desc="java")],
            prof,
        )
    )
    assert scored["Software Engineer"]  # both present (same title key collapses)
    # Full term match should beat partial.
    full = matcher._heuristic_score(
        _p("Software Engineer", desc="python distributed systems"),
        {"software engineer", "python", "distributed systems"}, [],
    )
    partial = matcher._heuristic_score(
        _p("Software Engineer", desc="java"),
        {"software engineer", "python", "distributed systems"}, [],
    )
    assert full > partial


def test_location_bonus_and_penalty():
    # Partial term match (1 of 2) so base=0.5 and location adjustments show.
    terms = {"swe", "backend"}
    here = matcher._heuristic_score(_p("SWE", location="New York"), terms, ["new york"])
    remote = matcher._heuristic_score(_p("SWE", location="Remote"), terms, ["new york"])
    elsewhere = matcher._heuristic_score(_p("SWE", location="Tokyo"), terms, ["new york"])
    assert here > remote > elsewhere


def test_empty_profile_scores_neutral():
    [(_, s)] = matcher.score([_p("Anything")], None)
    assert s == 0.5  # below the default 0.6 threshold -> no spam


def test_score_uses_injected_llm_and_falls_back_on_error():
    prof = _profile(roles="swe")
    posts = [_p("A"), _p("B")]

    def fake_llm(postings, profile_block):
        return {0: 0.91, 1: 0.42}

    out = dict((p.title, s) for p, s in matcher.score(posts, prof, llm=fake_llm))
    assert out == {"A": 0.91, "B": 0.42}

    def boom(postings, profile_block):
        raise RuntimeError("api down")

    # Falls back to heuristic (no exception bubbles up).
    fb = matcher.score(posts, prof, llm=boom)
    assert len(fb) == 2 and all(isinstance(s, float) for _, s in fb)


def test_score_empty():
    assert matcher.score([], None) == []
