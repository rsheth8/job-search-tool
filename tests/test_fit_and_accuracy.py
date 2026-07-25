"""Job accuracy (are these real, current jobs?) and fit transparency (why this one?).

Two halves:

* **Accuracy** — closed/filled reqs, commission-only "opportunities", broader
  staleness parsing, placeholder employers, and the same job arriving from three
  feeds at once.
* **Transparency** — a recommendation that can state its reasons in words, and
  admit its concerns.

The honesty rule under test: a reason must be checkable against the posting. We
never claim a skill match the description doesn't contain.
"""
from __future__ import annotations

import pytest

from app import fit
from app.jobsources import JobPosting, ghost, quality


def _p(**kw) -> JobPosting:
    base = dict(source="aggregator", external_id="1", title="Software Engineer",
                url="https://x/apply", company="Acme", location="Remote",
                description="Build backend services in Python.")
    base.update(kw)
    return JobPosting(**base)


# --- closed / filled reqs ---------------------------------------------------

@pytest.mark.parametrize("text", [
    "This position has been filled.",
    "We are no longer accepting applications for this role.",
    "Applications are closed.",
    "This job is no longer available.",
    "This posting has expired.",
])
def test_closed_postings_are_detected(text):
    assert ghost.is_closed(_p(description=text))
    assert ghost.is_ghost(_p(description=text))


def test_a_closed_req_is_dropped_even_on_a_trusted_board():
    """First-party boards are trusted about *who's* hiring, but a closed req there
    still burns a real application — the one case worth overriding trust for."""
    posting = _p(source="greenhouse", description="This position has been filled.")
    assert quality.is_first_party(posting)
    assert ghost.is_ghost(posting)


def test_an_open_first_party_posting_is_still_trusted():
    assert not ghost.is_ghost(_p(source="greenhouse",
                                 description="Join us to build backend services."))


def test_ordinary_wording_is_not_read_as_closed():
    assert not ghost.is_closed(_p(description="This role is open and we're hiring."))
    assert not ghost.is_closed(_p(description="Applications are reviewed weekly."))


# --- not-really-a-job -------------------------------------------------------

@pytest.mark.parametrize("text", [
    "This is a 100% commission role.",
    "Commission-only position with unlimited upside.",
    "Unpaid internship for the right candidate.",
    "You must purchase a starter kit to begin.",
])
def test_pay_to_play_listings_are_flagged(text):
    assert ("commission-only / pay-to-play"
            in [r for r, _ in ghost.ghost_signals(_p(description=text))])


# --- staleness --------------------------------------------------------------

@pytest.mark.parametrize("posted,stale", [
    ("10 days ago", False),
    ("50 days ago", True),
    ("3 weeks ago", False),
    ("8 weeks ago", True),
    ("2 months ago", True),
    ("1 month ago", False),
    ("30+ days ago", True),
])
def test_staleness_reads_days_weeks_and_months(posted, stale):
    signals = [r for r, _ in ghost.ghost_signals(_p(posted_at=posted))]
    assert ("stale posting" in signals) is stale


# --- placeholder employers --------------------------------------------------

@pytest.mark.parametrize("company", [
    "Our Client", "Confidential Company", "Leading Company", "Fortune 500",
    "Recruiting Agency", "TBD",
])
def test_placeholder_employers_are_dropped(company):
    assert not quality.is_reputable(_p(company=company))


@pytest.mark.parametrize("title", [
    "Make money from home", "URGENT HIRING - Software Engineer",
    "Hiring Immediately!", "$$$ Sales Rep", "Data Entry - Daily Pay",
])
def test_spam_titles_are_dropped(title):
    assert not quality.is_reputable(_p(title=title))


def test_a_real_posting_survives_the_filters():
    posting = _p(company="Stripe", title="Senior Backend Engineer")
    assert quality.is_reputable(posting)
    assert not ghost.is_ghost(posting)


# --- dedupe -----------------------------------------------------------------

def test_the_same_job_from_several_feeds_collapses():
    postings = [
        _p(source="aggregator", external_id="a", title="Senior Software Engineer (Remote)"),
        _p(source="rss", external_id="b", title="Software Engineer II", company="Acme, Inc."),
        _p(source="greenhouse", external_id="c", title="Software Engineer"),
    ]
    kept, dropped = quality.dedupe(postings)
    assert dropped == 2 and len(kept) == 1
    # the copy you can apply to directly wins, whatever order it arrived in
    assert kept[0].source == "greenhouse"


def test_dedupe_keeps_two_openings_on_the_same_board():
    """Two reqs on one board with the same title are two real openings (different
    teams, same ad). Merging them would hide a job — the one thing dedupe must
    never do."""
    postings = [_p(source="greenhouse", external_id="1", title="Software Engineer"),
                _p(source="greenhouse", external_id="2", title="Software Engineer")]
    kept, dropped = quality.dedupe(postings)
    assert dropped == 0 and len(kept) == 2


def test_dedupe_drops_the_aggregator_copy_but_keeps_both_real_reqs():
    postings = [_p(source="greenhouse", external_id="1", title="Software Engineer"),
                _p(source="greenhouse", external_id="2", title="Software Engineer"),
                _p(source="aggregator", external_id="x", title="Software Engineer")]
    kept, dropped = quality.dedupe(postings)
    assert dropped == 1
    assert [p.source for p in kept] == ["greenhouse", "greenhouse"]


def test_dedupe_keeps_genuinely_different_roles():
    postings = [_p(external_id="a", title="Software Engineer"),
                _p(external_id="b", title="Product Manager"),
                _p(external_id="c", title="Software Engineer", company="Beta")]
    kept, dropped = quality.dedupe(postings)
    assert dropped == 0 and len(kept) == 3


def test_dedupe_never_merges_postings_missing_a_company_or_title():
    postings = [_p(external_id="a", company=""), _p(external_id="b", company="")]
    kept, dropped = quality.dedupe(postings)
    assert dropped == 0 and len(kept) == 2


def test_dedup_key_ignores_seniority_and_legal_suffixes():
    assert (quality.dedup_key(_p(title="Senior Software Engineer III", company="Acme Inc."))
            == quality.dedup_key(_p(title="Software Engineer", company="Acme")))


# --- why this fits you ------------------------------------------------------

class _Profile(dict):
    """Stands in for a profile row (which supports [] but not .get())."""


def _profile(**kw):
    base = {"roles": "backend engineer", "keywords": "python, go",
            "locations": "chicago", "seniority": "", "resume_summary": ""}
    base.update(kw)
    return _Profile(base)


def test_explain_states_checkable_reasons():
    detail = fit.explain(
        _p(title="Backend Engineer", description="You'll write Python and Go."),
        _profile(), score=0.87)
    reasons = " · ".join(detail["reasons"])
    assert "backend engineer" in reasons        # the role matched the title
    assert "python" in reasons and "go" in reasons
    assert "remote" in reasons
    assert detail["line"].startswith("87% ·")


def test_explain_never_claims_a_skill_the_posting_lacks():
    detail = fit.explain(
        _p(title="Backend Engineer", description="You'll write Java all day."),
        _profile(keywords="python, rust"), score=0.5)
    assert "python" not in " ".join(detail["reasons"]).lower()
    assert "rust" not in " ".join(detail["reasons"]).lower()


def test_explain_does_not_match_a_skill_inside_another_word():
    """"go" must not fire on "going", or every posting looks like a Go job."""
    detail = fit.explain(
        _p(title="Backend Engineer", description="We're going to grow the team."),
        _profile(keywords="go"), score=0.5)
    assert "go" not in " ".join(detail["reasons"])


def test_explain_surfaces_concerns():
    detail = fit.explain(
        _p(title="Marketing Manager", location="Austin, TX",
           description="Own our campaigns."),
        _profile(), score=0.4)
    assert any("target roles" in c for c in detail["concerns"])
    assert any("austin" in c.lower() for c in detail["concerns"])
    assert "⚠️" in detail["line"]


def test_explain_credits_applying_direct():
    detail = fit.explain(_p(source="greenhouse"), _profile(), score=0.8)
    assert "apply direct" in detail["reasons"]
    assert "apply direct" not in fit.explain(_p(source="aggregator"),
                                             _profile(), score=0.8)["reasons"]


def test_explain_works_with_no_profile_at_all():
    detail = fit.explain(_p(), None, score=0.7)
    assert detail["line"].startswith("70%")
    assert detail["concerns"] == []


def test_explain_folds_in_a_cached_llm_read_without_fetching_one():
    detail = fit.explain(_p(), _profile(), score=0.8,
                         summary={"stretch": "needs 8+ years, you have 3"})
    assert any("8+ years" in c for c in detail["concerns"])


def test_explain_reads_a_stored_row_too():
    """Postings arrive as dataclasses from a source and as rows from the store."""
    from app import jobstore

    row = jobstore.save_posting("u1", _p(title="Backend Engineer"),
                                relevance_score=0.91, status="queued")
    assert fit.explain(row, _profile())["line"].startswith("91%")


# --- it reaches the user ----------------------------------------------------

def test_the_review_card_says_why():
    from app import job_alerts

    card = job_alerts.build_review_card(
        _p(title="Backend Engineer", description="Python and Go, all day."),
        0.88, 12, position=1, total=3, profile=_profile())
    assert "✅" in card and "backend engineer" in card
    assert "python" in card


def test_the_review_card_still_renders_without_a_profile():
    from app import job_alerts

    card = job_alerts.build_review_card(_p(), 0.8, 12, position=1, total=3)
    assert "Software Engineer" in card and "✅" not in card
