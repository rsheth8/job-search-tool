"""Eligibility gate: profile-driven rule tier + optional (injectable) LLM tier."""
from __future__ import annotations

import sqlite3

from app import eligibility
from app.jobsources import JobPosting


def _profile(seniority="", roles="software engineer", resume="", keywords="") -> sqlite3.Row:
    cols = {"roles": roles, "keywords": keywords, "locations": "", "seniority": seniority,
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

def test_nontechnical_field_roles_dropped_for_technical_candidate():
    # Wrong field for a technical candidate, no technical signal in the title.
    for t in ("Account Executive", "Business Development Representative",
              "Sales Development Representative", "Recruiter",
              "Administrative Coordinator", "Marketing Coordinator",
              "Customer Success Specialist"):
        assert not eligibility.is_eligible(_p(t), ENTRY), t


def test_technical_and_adjacent_roles_survive_field_filter():
    # A technical signal in the title keeps technical-adjacent roles.
    for t in ("Sales Engineer", "Solutions Engineer", "Data Analyst",
              "Business Systems Analyst II", "Marketing Analyst",
              "Financial Analyst", "Software Engineer", "ML Engineer"):
        assert eligibility.is_eligible(_p(t), ENTRY), t


def test_field_filter_respects_config_flag(monkeypatch):
    monkeypatch.setenv("ELIGIBILITY_FIELD_FILTER", "false")
    from app.config import get_settings
    get_settings.cache_clear()
    assert eligibility.is_eligible(_p("Account Executive"), ENTRY)  # not dropped when off


def test_empty_profile_looks_technical():
    assert eligibility.profile_looks_technical(None)
    assert eligibility.profile_looks_technical(_profile(roles="", keywords=""))


def test_marketing_profile_keeps_nontechnical_roles():
    marketing = _profile(roles="marketing coordinator", keywords="brand")
    assert not eligibility.profile_looks_technical(marketing)
    for t in ("Account Executive", "Marketing Coordinator", "Recruiter"):
        assert eligibility.is_eligible(_p(t), marketing), t


def test_swe_profile_still_drops_nontechnical_roles():
    swe = _profile(roles="software engineer", keywords="python")
    assert eligibility.profile_looks_technical(swe)
    assert not eligibility.is_eligible(_p("Account Executive"), swe)
    assert not eligibility.is_eligible(_p("Marketing Coordinator"), swe)


def test_nurse_profile_keeps_nursing_roles():
    nurse = _profile(roles="registered nurse")
    assert not eligibility.profile_looks_technical(nurse)
    assert eligibility.is_eligible(
        _p("Registered Nurse", desc="Must be a registered nurse."), nurse
    )


def test_big_experience_requirement_ineligible():
    assert not eligibility.is_eligible(_p(desc="We need 8+ years of experience."), ENTRY)


def test_years_requirement_recognizes_yoe_abbreviation():
    assert not eligibility.is_eligible(_p("Software Engineer (8+ YOE)"), ENTRY)
    assert not eligibility.is_eligible(_p("Backend Engineer", "Requires 7 YOE."), ENTRY)


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
