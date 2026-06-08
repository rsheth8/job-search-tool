"""Eligibility gate: profile-driven rule tier + optional (injectable) LLM tier."""
from __future__ import annotations

import sqlite3

from app import eligibility
from app.jobsources import JobPosting


def _profile(seniority="", roles="software engineer", resume="") -> sqlite3.Row:
    cols = {"roles": roles, "keywords": "", "locations": "", "seniority": seniority,
            "resume_summary": resume, "min_relevance": None}
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    keys = ", ".join(cols)
    conn.execute(f"CREATE TABLE p ({keys})")
    conn.execute(f"INSERT INTO p ({keys}) VALUES ({', '.join('?' * len(cols))})",
                 tuple(cols.values()))
    return conn.execute("SELECT * FROM p").fetchone()


def _p(title="Software Engineer", desc="Build software.") -> JobPosting:
    return JobPosting(source="greenhouse", external_id="1", title=title,
                      url="x", company="Acme", location="Remote", description=desc)


ENTRY = None  # use config fallback ("entry") when profile is None


# ---------------------------------------------------------------------------
# Rule tier — seniority gap (candidate = entry via config fallback)
# ---------------------------------------------------------------------------

def test_plain_role_is_eligible_for_entry():
    assert eligibility.is_eligible(_p("Software Engineer"), ENTRY)


def test_level_two_role_is_eligible_for_entry():
    # "II" is one level up — an entry candidate can stretch, so it's kept.
    assert eligibility.is_eligible(_p("Software Engineer II"), ENTRY)


def test_senior_role_is_ineligible_for_entry():
    assert not eligibility.is_eligible(_p("Senior Software Engineer"), ENTRY)


def test_manager_and_staff_and_director_ineligible_for_entry():
    for t in ("Engineering Manager", "Staff Data Scientist",
              "Director of Analytics", "Principal Engineer", "Analytics Lead"):
        assert not eligibility.is_eligible(_p(t), ENTRY), t


# ---------------------------------------------------------------------------
# Rule tier — years + credentials
# ---------------------------------------------------------------------------

def test_big_experience_requirement_ineligible():
    assert not eligibility.is_eligible(_p(desc="We need 8+ years of experience."), ENTRY)


def test_small_experience_requirement_ok():
    assert eligibility.is_eligible(_p(desc="1-2 years of experience preferred."), ENTRY)


def test_hard_credentials_ineligible():
    assert not eligibility.is_eligible(_p("Staff Nurse", desc="Must be a registered nurse."), ENTRY)
    assert not eligibility.is_eligible(_p(desc="Active security clearance required."), ENTRY)


def test_required_doctorate_ineligible_but_preferred_ok():
    assert not eligibility.is_eligible(_p(desc="A PhD is required for this role."), ENTRY)
    assert eligibility.is_eligible(_p(desc="A PhD is preferred but not required."), ENTRY)


# ---------------------------------------------------------------------------
# Profile-driven: a senior candidate is NOT gated out of senior roles
# ---------------------------------------------------------------------------

def test_filter_is_customized_to_candidate_level():
    senior = _profile(seniority="Senior software engineer, 8 years")
    role = _p("Senior Software Engineer")
    # The SAME role: dropped for an entry candidate, kept for a senior one.
    assert not eligibility.is_eligible(role, ENTRY)
    assert eligibility.is_eligible(role, senior)


def test_filter_eligible_counts():
    posts = [_p("Software Engineer"), _p("Senior Software Engineer"),
             _p("Engineering Manager")]
    kept, dropped = eligibility.filter_eligible(posts, ENTRY)
    assert len(kept) == 1 and dropped == 2


# ---------------------------------------------------------------------------
# LLM tier (injected judge; never hits the network)
# ---------------------------------------------------------------------------

def test_llm_tier_inactive_is_noop():
    posts = [_p("Software Engineer")]
    kept, dropped = eligibility.filter_eligible_llm(posts, ENTRY)  # no key, no flag
    assert kept == posts and dropped == 0


def test_llm_tier_drops_unqualified_with_injected_judge():
    posts = [_p("Software Engineer", desc="entry friendly"),
             _p("Quant Researcher", desc="needs a PhD in physics")]

    def judge(postings, profile_block):
        return {0: True, 1: False}  # keep the first, drop the second

    kept, dropped = eligibility.filter_eligible_llm(posts, ENTRY, assess=judge)
    assert dropped == 1 and kept[0].title == "Software Engineer"


def test_llm_tier_fails_open_on_error():
    posts = [_p("A"), _p("B")]

    def judge(postings, profile_block):
        raise RuntimeError("model down")

    kept, dropped = eligibility.filter_eligible_llm(posts, ENTRY, assess=judge)
    assert kept == posts and dropped == 0  # never drop on error
