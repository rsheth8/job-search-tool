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


def test_prefilter_keeps_software_engineering_intern():
    """Intern titles use 'engineering', not 'engineer' — still a SWE match."""
    prof = _profile(roles="software engineer", keywords="python")
    intern = _p("Software Engineering Intern")
    coop = _p("Software Engineering Co-op")
    sales = _p("Sales Intern")
    kept = matcher.prefilter([intern, coop, sales], prof)
    assert intern in kept
    assert coop in kept
    assert sales not in kept
    # Bare "swe" must expand the same way.
    swe_prof = _profile(roles="swe", keywords="swe")
    assert intern in matcher.prefilter([intern], swe_prof)


def test_short_terms_match_whole_words_not_substrings():
    # "ai" must match an "AI Engineer" role but NOT words like email/training.
    prof = _profile(roles="ai engineer", keywords="ml")
    junk = _p("Digital Marketing Manager", desc="email campaigns and training")
    real = _p("AI Engineer", desc="build ML systems")
    kept = matcher.prefilter([junk, real], prof)
    assert real in kept and junk not in kept


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


def test_llm_score_chunks_and_merges_global_indices(monkeypatch):
    # _llm_score must split a big batch into chunks and merge results back onto
    # the GLOBAL posting indices (the bug: a single oversized call truncated JSON).
    monkeypatch.setattr(matcher, "_SCORE_CHUNK", 2)
    seen = []

    def fake_chunk(chunk, profile_block):
        seen.append(len(chunk))
        # Echo a score keyed by the role title so we can verify index mapping.
        return {i: round(0.1 * (int(p.title) + 1), 3) for i, p in enumerate(chunk)}

    monkeypatch.setattr(matcher, "_llm_score_chunk", fake_chunk)
    postings = [_p(str(n)) for n in range(5)]  # titles "0".."4"
    out = matcher._llm_score(postings, "profile")
    assert seen == [2, 2, 1]                    # chunked, not one giant call
    assert set(out.keys()) == {0, 1, 2, 3, 4}   # global indices preserved
    assert out[3] == round(0.1 * 4, 3)          # 4th posting scored correctly


def test_score_allow_llm_false_uses_heuristic(monkeypatch):
    # Even with the LLM "available", allow_llm=False must not call it.
    monkeypatch.setattr("app.config.Settings.use_llm_router", property(lambda self: True))

    def boom(*a, **k):
        raise AssertionError("LLM scorer must not run when allow_llm=False")

    monkeypatch.setattr(matcher, "_llm_score", boom)
    prof = _profile(roles="software engineer")
    out = matcher.score([_p("Software Engineer")], prof, allow_llm=False)
    assert len(out) == 1 and 0.0 <= out[0][1] <= 1.0  # heuristic ran, no crash


# ---------------------------------------------------------------------------
# Titles-only profiles — the skills step is skippable, so this is normal
# ---------------------------------------------------------------------------

def _score(prof, posting):
    return matcher._heuristic_score(
        posting, matcher._terms(prof), matcher._locations(prof),
        roles=matcher._roles(prof),
    )


def test_an_intern_posting_matches_an_intern_profile():
    """Regression: the prefilter expanded intern variants and scoring did not.

    "Software Engineering Intern" survived the gate on a "software intern"
    profile and then scored 0.15 — the same as an unrelated marketing role —
    because neither raw role string appears in that title verbatim.
    """
    prof = _profile(roles="software engineer, software intern", locations="chicago, remote")
    intern = _p("Software Engineering Intern, Summer 2027", location="Chicago")
    marketing = _p("Marketing Coordinator", location="Remote")
    assert _score(prof, intern) > 0.8
    assert _score(prof, intern) > _score(prof, marketing) + 0.5


def test_engineering_intern_matches_an_engineer_profile_too():
    prof = _profile(roles="software engineer")
    assert _score(prof, _p("Software Engineering Intern")) > 0.5


def test_a_titles_only_feed_does_not_collapse_into_one_score():
    """A real run produced three distinct values across the whole feed, and the
    top of it was a flat tie — nothing to rank on."""
    prof = _profile(roles="software engineer, software intern", locations="chicago, remote")
    feed = [
        _p("Software Engineer", location="Remote"),
        _p("Software Engineer, Product", location="Remote"),
        _p("Software Engineer, ML Developer Experience", location="Remote"),
        _p("Software Engineer II, Ads Ranking Platform, Monetization", location="Chicago"),
        _p("Software Engineering Intern, Summer 2027", location="Chicago"),
    ]
    scores = {_score(prof, p) for p in feed}
    assert len(scores) >= 4, f"feed collapsed into {sorted(scores)}"


def test_a_plain_title_outranks_a_heavily_qualified_one():
    """For a profile that only said "software engineer", the posting that is
    exactly that is the closest answer; extra clauses are specialisation the
    person never asked for."""
    prof = _profile(roles="software engineer")
    plain = _score(prof, _p("Software Engineer"))
    qualified = _score(prof, _p("Software Engineer II, Ads Ranking Platform, Monetization"))
    assert plain > qualified


def test_roles_are_alternatives_not_a_checklist():
    """Wanting "swe" OR "backend" must not make a perfect SWE posting a half
    match — that conjunction is what flattened every titles-only feed."""
    prof = _profile(roles="swe, backend")
    one_role = _score(prof, _p("SWE", desc="swe role"))
    assert one_role > 0.8


def test_an_unrelated_role_still_scores_low_without_skills():
    prof = _profile(roles="software engineer", locations="chicago")
    assert _score(prof, _p("Marketing Coordinator", location="Chicago")) < 0.3


def test_skills_still_gate_a_title_match_when_the_profile_has_them():
    """The titles-only path must not leak into profiles that do list skills:
    "right title, wrong stack" stays below the 0.6 alert bar."""
    prof = _profile(roles="software engineer", keywords="kubernetes, terraform")
    wrong_stack = _score(prof, _p("Software Engineer", desc="COBOL maintenance"))
    assert wrong_stack < 0.6
    right_stack = _score(prof, _p("Software Engineer", desc="kubernetes terraform"))
    assert right_stack > wrong_stack


def test_role_variants_cover_both_directions():
    assert "software engineering intern" in matcher._role_variants("software engineer")
    assert "software engineering intern" in matcher._role_variants("software intern")
    assert "software engineer" in matcher._role_variants("software engineer")
