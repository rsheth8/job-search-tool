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
    "Citizenship status", "Are you Hispanic or Latino?",
    "Do you identify as transgender?", "Gender identity",
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
    assert fieldmatch.select_value(countries, "USA") is None        # no overlap
    assert fieldmatch.select_value(countries, "united kingdom") == "United Kingdom"
    # Yes/No
    assert fieldmatch.select_value(["Yes", "No"], "Yes") == "Yes"
    assert fieldmatch.select_value(["Yes", "No"], "no") == "No"
    # contains either direction
    assert fieldmatch.select_value(["Authorized to work", "Not authorized"],
                                   "Authorized") == "Authorized to work"
    assert fieldmatch.select_value(["Yes", "No"], "maybe") is None


def test_is_essay_label():
    assert fieldmatch.is_essay_label("Why do you want to work here?")
    assert fieldmatch.is_essay_label("Describe a project you're proud of")
    assert not fieldmatch.is_essay_label("Email")          # a fact, not an essay
    assert not fieldmatch.is_essay_label("First name")


@pytest.mark.parametrize("label", [
    "Sexual orientation", "Do you identify as transgender?",
    "Are you Hispanic or Latino?", "Religion", "Date of birth",
])
def test_is_eeo_hard_blocked(label):
    assert fieldmatch.is_eeo(label)


def test_is_eeo_leaves_ordinary_and_optional_demographics_alone():
    assert not fieldmatch.is_eeo("First name")
    assert not fieldmatch.is_eeo("Why do you want to work here?")
    assert not fieldmatch.is_eeo("Gender")
    assert not fieldmatch.is_eeo("")


def test_long_eeo_question_is_not_an_essay():
    """A demographic question that is long and question-shaped clears the essay
    bar on wording alone — and would then get a *drafted answer* written into it.
    The EEO guard has to come first."""
    label = "Are you Hispanic or Latino? hispanic hispanic"
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
