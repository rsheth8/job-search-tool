"""Shared field-matching brain (Phase 2): label -> identity key, select-option
matching, essay detection, and the never-fill EEO guard."""
from __future__ import annotations

import pytest

from app import fieldmatch


@pytest.mark.parametrize("label,key", [
    ("Email Address", "email"),                  # email beats address
    ("Current location", "location"),            # the bug we fixed, now in Python
    ("Street address", "address"),
    ("First name", "first_name"),
    ("Preferred first name", "preferred_name"),  # preferred beats first
    ("LinkedIn Profile", "linkedin"),
    ("Are you authorized to work in the US?", "work_authorized"),
    ("Will you require visa sponsorship?", "needs_sponsorship"),
    ("Are you willing to relocate?", "willing_to_relocate"),
    ("Desired salary", "salary_expectation"),
    ("Graduation year", "grad_year"),
    ("School / University", "school"),
])
def test_match_key(label, key):
    assert fieldmatch.match_key(label) == key


@pytest.mark.parametrize("label,key", [
    ("Gender", "gender"),
    ("Race / Ethnicity", "race"),
    ("Are you a protected veteran?", "veteran_status"),
    ("Disability status", "disability_status"),
    ("Are you Hispanic/Latino?", "hispanic_latino"),
])
def test_optional_eeo_maps_when_identity_can_fill(label, key):
    """Gender/race/veteran/disability are opt-in via FIELD_RULES (fill only when set)."""
    assert fieldmatch.match_key(label) == key
    assert not fieldmatch.is_eeo(label)


@pytest.mark.parametrize("label", [
    "Sexual orientation",
    # hard-blocked: a missed one answers a protected-class question for the user
    "National origin", "Voluntary Self-Identification", "EEO information",
    "Equal Employment Opportunity", "Marital status", "Religion",
    "Do you identify as LGBTQ+?", "Date of birth", "DOB",
    "Citizenship status",
    "Do you identify as transgender?", "Gender identity", "Birthday",
])
def test_never_fills_hard_blocked_demographic_fields(label):
    assert fieldmatch.match_key(label) is None
    assert fieldmatch.is_eeo(label)


@pytest.mark.parametrize("label,key", [
    # label variants real ATS forms use that the original rules missed
    ("Name (First)", "first_name"),
    ("Name (Last)", "last_name"),
    ("Contact number", "phone"),
    ("Where do you live?", "location"),
    ("Currently based", "location"),
    ("Homepage", "portfolio"),
    ("Where did you study?", "school"),
    ("Year of graduation", "grad_year"),
    ("How many years of Python?", "years_experience"),
    ("Notice period", "start_date"),
    ("When could you start?", "start_date"),
    ("When do you graduate?", "grad_year"),
    ("End date month", "grad_month"),
    ("End date year", "grad_year_num"),
    ("Do you prefer a winter or summer internship?", "intern_season"),
    ("What is your gender?", "gender"),
    ("Please select your race", "race"),
    ("Referral source", "how_heard"),
    ("Preferred work location", "work_arrangement"),
    ("Are you 18 years of age or older?", "over_18"),
    ("Will you now or in the future require sponsorship?", "needs_sponsorship"),
    ("Most recent employer", "current_company"),
    ("Area of study", "discipline"),
    ("Country/Region", "country"),
    ("Where are you currently living?", "location"),
    ("Cell phone", "phone"),
    ("When will you graduate?", "grad_year"),
    ("How did you hear about this opportunity?", "how_heard"),
    ("Do you know anyone who works here?", "related_to_employee"),
    ("Cumulative GPA", "gpa"),
])
def test_broadened_label_coverage(label, key):
    assert fieldmatch.match_key(label) == key


def test_current_salary_is_still_not_treated_as_an_expectation():
    """We store a desired salary, never a current one — don't let a broadened
    rule volunteer the wrong number."""
    assert fieldmatch.match_key("Current salary") is None


def test_match_key_unknown_returns_none():
    assert fieldmatch.match_key("Favorite color") is None
    assert fieldmatch.match_key("") is None


def test_select_value_exact_and_contains():
    countries = ["United States", "Canada", "United Kingdom"]
    assert fieldmatch.select_value(countries, "United States") == "United States"
    assert fieldmatch.select_value(countries, "USA") == "United States"
    assert fieldmatch.select_value(countries, "united kingdom") == "United Kingdom"
    # Yes/No
    assert fieldmatch.select_value(["Yes", "No"], "Yes") == "Yes"
    assert fieldmatch.select_value(["Yes", "No"], "no") == "No"
    # contains either direction
    assert fieldmatch.select_value(["Authorized to work", "Not authorized"],
                                   "Authorized") == "Authorized to work"
    assert fieldmatch.select_value(["Yes", "No"], "maybe") is None


def test_select_value_picks_the_closest_allowed_option():
    """Typeaheads only persist a listed option — pick the nearest, don't invent."""
    schools = [
        "University of Minnesota Crookston",
        "University of Minnesota-Twin Cities",
        "Stanford University",
    ]
    assert fieldmatch.select_value(schools, "University of Minnesota Twin Cities") == (
        "University of Minnesota-Twin Cities")

    locations = [
        "Chicago, Illinois, United States",
        "Vernon Hills, Illinois, United States",
        "Minneapolis, Minnesota, United States",
    ]
    assert fieldmatch.select_value(locations, "Chicago, IL") == (
        "Chicago, Illinois, United States")
    assert fieldmatch.select_value(locations, "Vernon Hills, IL, United States") == (
        "Vernon Hills, Illinois, United States")

    degrees = ["High School", "Associate's Degree", "Bachelor's Degree", "Master's Degree"]
    assert fieldmatch.select_value(degrees, "B.S.") == "Bachelor's Degree"
    assert fieldmatch.select_value(
        degrees,
        "B.S. Computer Science (May 2026); M.S. Data Science (in progress)",
    ) == "Master's Degree"

    states = ["Illinois", "Minnesota", "California"]
    assert fieldmatch.select_value(states, "IL") == "Illinois"
    assert fieldmatch.select_value(["2025", "2026", "2027", "2028"],
                                   "December 2027") == "2027"
    assert fieldmatch.select_value(["Canada", "Mexico"], "United States") is None
    assert fieldmatch.select_value(["Select…", "United States"], "USA") == "United States"
    assert fieldmatch.select_value(
        ["3.7 - 4.0", "3.1 - 3.6", "3.0 or under"], "3.5") == "3.1 - 3.6"
    assert fieldmatch.select_value(
        ["Sept - Dec 2027", "Jan - April 2028", "May - Aug 2028", "Other"],
        "December 2027") == "Sept - Dec 2027"
    assert fieldmatch.select_value(
        ["January", "June", "December"], "December 2027") == "December"
    assert fieldmatch.select_value(
        ["Winter 2027", "Summer 2027"], "Summer") == "Summer 2027"
    assert fieldmatch.select_value(
        ["Afghanistan+93", "United States+1", "Canada+1"],
        "United States") == "United States+1"
    assert fieldmatch.select_value(["Male", "Female", "Non-binary"], "Man") == "Male"
    assert fieldmatch.select_value(
        ["Hispanic or Latino", "Not Hispanic or Latino"], "No") == (
        "Not Hispanic or Latino")
    assert fieldmatch.select_value(
        ["I am authorized to work in the US", "I am not authorized to work in the US"],
        "Yes") == "I am authorized to work in the US"
    assert fieldmatch.select_value(
        ["$80,000 - $100,000", "$100,000 - $130,000"], "120000") == (
        "$100,000 - $130,000")
    assert fieldmatch.select_value(["0-2", "3+", "5+"], "5") == "5+"
    assert fieldmatch.select_value(
        ["Fully remote", "Hybrid", "On-site"], "Remote") == "Fully remote"


def test_referral_source_is_how_heard_not_related():
    assert fieldmatch.match_key("Referral source") == "how_heard"
    assert fieldmatch.match_key("Do you know anyone who works here?") == (
        "related_to_employee")
    assert fieldmatch.match_key("Preferred work location") == "work_arrangement"
    assert fieldmatch.match_key("Current location") == "location"
    assert fieldmatch.match_key(
        "Are you authorized to work remotely in the US?") == "work_authorized"


def test_is_essay_label():
    assert fieldmatch.is_essay_label("Why do you want to work here?")
    assert fieldmatch.is_essay_label("Describe a project you're proud of")
    assert not fieldmatch.is_essay_label("Email")          # a fact, not an essay
    assert not fieldmatch.is_essay_label("First name")


@pytest.mark.parametrize("label", [
    "Sexual orientation", "Do you identify as transgender?",
    "Religion", "Date of birth",
])
def test_is_eeo_hard_blocked(label):
    assert fieldmatch.is_eeo(label)


def test_is_eeo_leaves_ordinary_and_optional_demographics_alone():
    assert not fieldmatch.is_eeo("First name")
    assert not fieldmatch.is_eeo("Why do you want to work here?")
    assert not fieldmatch.is_eeo("Gender")
    assert not fieldmatch.is_eeo("Are you Hispanic or Latino?")
    assert not fieldmatch.is_eeo("")


def test_long_eeo_question_is_not_an_essay():
    """A demographic question that is long and question-shaped clears the essay
    bar on wording alone — and would then get a *drafted answer* written into it.
    The EEO guard has to come first."""
    label = "Do you identify as LGBTQ+? orientation orientation"
    assert len(label) > 40 and fieldmatch.match_key(label) is None
    assert not fieldmatch.is_essay_label(label)


def test_option_for_resolves_key_and_option():
    identity = {"country": "United States", "needs_sponsorship": "No"}
    assert fieldmatch.option_for("Country", ["Canada", "United States"], identity) == (
        "country", "United States")
    # Yes/No group, same decision path
    assert fieldmatch.option_for("Do you require visa sponsorship?",
                                 ["Yes", "No"], identity) == ("needs_sponsorship", "No")


def test_option_for_reports_why_it_could_not_decide():
    # unknown label -> no key at all
    assert fieldmatch.option_for("Favorite color", ["Red"], {}) == (None, None)
    # optional EEO resolves the key; no matching option -> (key, None)
    assert fieldmatch.option_for("Gender", ["Male", "Female"], {"gender": "x"}) == (
        "gender", None)
    # hard-blocked EEO never resolves, even with options present
    assert fieldmatch.option_for("Sexual orientation", ["Gay", "Straight"],
                                 {"orientation": "x"}) == (None, None)
    # understood label, but nothing to say / nothing that matches -> (key, None)
    assert fieldmatch.option_for("Country", ["Canada"], {}) == ("country", None)
    assert fieldmatch.option_for("Country", ["Canada"],
                                 {"country": "United States"}) == ("country", None)


def test_option_for_accepts_a_precomputed_key():
    """The worker resolves keys from label *and* name/id, so it passes its own."""
    assert fieldmatch.option_for("", ["Yes", "No"], {"work_authorized": "Yes"},
                                 key="work_authorized") == ("work_authorized", "Yes")


@pytest.mark.parametrize("label", [
    "Resume", "Resume/CV", "Résumé", "Upload your CV", "Curriculum Vitae",
])
def test_is_resume_field_yes(label):
    assert fieldmatch.is_resume_field(label)


@pytest.mark.parametrize("label", [
    "Cover letter", "Cover Letter (optional)", "Portfolio", "Other attachment", "",
])
def test_is_resume_field_no(label):
    assert not fieldmatch.is_resume_field(label)
