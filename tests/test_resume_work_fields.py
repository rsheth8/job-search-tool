"""What a resume says about work, read as fields.

The experience section was mined for free-text bullets and nothing else, so
"current company", "current title" and "years of experience" came back empty
from a resume that named all three -- and the quiz had nothing to prefill the
Work step with.
"""
from __future__ import annotations

from app import profile_import as pi

STUDENT = """
RAHIL SHETH
Chicago, IL | rahil@example.com

EDUCATION
Purdue University
M.S. Computer Science, Expected May 2027

EXPERIENCE
Stripe — Software Engineer Intern
May 2024 - August 2024, San Francisco, CA
Built a payments reconciliation service in Go.

Datadog — Software Engineering Intern
May 2023 - August 2023, New York, NY
Shipped a Kubernetes autoscaler integration.

SKILLS
Python, Go, React
"""


def _identity(text: str) -> dict:
    return pi._heuristic_parse(text)["identity"]


def test_the_most_recent_job_becomes_the_current_one():
    ident = _identity(STUDENT)
    assert ident["current_company"] == "Stripe"
    assert ident["current_title"] == "Software Engineer Intern"


def test_most_recent_is_by_date_not_by_order_on_the_page():
    """Plenty of resumes list oldest first."""
    text = STUDENT.replace(
        "Stripe — Software Engineer Intern\nMay 2024 - August 2024, San Francisco, CA\n"
        "Built a payments reconciliation service in Go.\n\n"
        "Datadog — Software Engineering Intern\nMay 2023 - August 2023, New York, NY\n"
        "Shipped a Kubernetes autoscaler integration.",
        "Datadog — Software Engineering Intern\nMay 2023 - August 2023, New York, NY\n"
        "Shipped a Kubernetes autoscaler integration.\n\n"
        "Stripe — Software Engineer Intern\nMay 2024 - August 2024, San Francisco, CA\n"
        "Built a payments reconciliation service in Go.",
    )
    assert _identity(text)["current_company"] == "Stripe"


def test_the_city_is_not_mistaken_for_the_employer():
    """"May 2024 - August 2024, San Francisco, CA" is a very common second line.

    The header splits on commas, so stripping the location has to happen first
    -- otherwise "San Francisco" arrives as its own part and looks exactly like
    a company name.
    """
    ident = _identity(STUDENT)
    assert "Francisco" not in ident.get("current_company", "")


def test_experience_is_counted_from_the_dates():
    """Four months plus four months is eight, which is zero whole years."""
    assert _identity(STUDENT)["years_experience"] == "0"


def test_a_stated_number_of_years_wins_over_the_arithmetic():
    text = STUDENT.replace("SKILLS", "SUMMARY\n7 years of experience in backend.\n\nSKILLS")
    assert _identity(text)["years_experience"] == "7"


def test_years_are_floored_not_rounded_up():
    """This lands on real applications. Understating is correctable; a false
    claim is not."""
    text = """
EXPERIENCE
Acme — Data Analyst
January 2022 - June 2023, Boston, MA
Analysed things.
EDUCATION
"""
    # 18 months.
    assert _identity(text)["years_experience"] == "1"


def test_overlapping_jobs_are_not_counted_twice():
    text = """
EXPERIENCE
Acme — Software Engineer
January 2022 - December 2023, Boston, MA
Built things.

Globex — Contract Developer
January 2022 - December 2023, Remote
Also built things.
EDUCATION
"""
    assert _identity(text)["years_experience"] == "2"


def test_a_current_job_runs_to_today():
    text = """
EMPLOYMENT
Shopify — Staff Engineer
March 2015 - Present
Owned checkout.
EDUCATION
"""
    assert int(_identity(text)["years_experience"]) >= 10


def test_a_year_span_inside_a_sentence_is_not_a_job():
    """The bug this guard exists for.

    "Analyzed 2019 - 2021 revenue trends across regions" was read as a job at a
    company called "Analyzed revenue trends across regions", and its two
    invented years went into the experience total.
    """
    text = """
EXPERIENCE
Acme — Data Analyst
March 2022 - March 2024, Boston, MA
Analyzed 2019 - 2021 revenue trends across regions.
EDUCATION
"""
    ident = _identity(text)
    assert ident["current_company"] == "Acme"
    assert ident["years_experience"] == "2"


def test_a_resume_with_no_work_says_nothing_rather_than_zero():
    text = """
Sam Lee
EDUCATION
B.A. History, Oberlin College, May 2024
SKILLS
Writing
"""
    ident = _identity(text)
    assert "current_company" not in ident
    assert "years_experience" not in ident


def test_title_first_layouts_still_split_correctly():
    text = """
WORK EXPERIENCE
Senior Software Engineer, Acme Corp            January 2019 - March 2021
Led the billing rewrite.
EDUCATION
"""
    ident = _identity(text)
    assert ident["current_company"] == "Acme Corp"
    assert ident["current_title"] == "Senior Software Engineer"


def test_pipe_separated_headers_with_a_city():
    text = """
PROFESSIONAL EXPERIENCE
Northwestern Mutual | Financial Analyst | Milwaukee, WI | Jun 2021 - Aug 2023
Modeled retirement portfolios.
SKILLS
Excel
"""
    ident = _identity(text)
    assert ident["current_company"] == "Northwestern Mutual"
    assert ident["current_title"] == "Financial Analyst"


def test_the_import_only_fills_what_is_empty(tmp_path, monkeypatch):
    """Same rule as every other imported field: never overwrite the person."""
    from app import applicant, db

    db.init_db()
    uid = "usr_work_fields"
    applicant.set_identity(uid, {"current_company": "My Own Answer"})
    pi.apply_extracted(uid, pi._heuristic_parse(STUDENT), source="resume")
    saved = applicant.get_identity(uid)
    assert saved["current_company"] == "My Own Answer"
    assert saved["current_title"] == "Software Engineer Intern"
