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


@pytest.mark.parametrize("label", [
    "Gender", "Race / Ethnicity", "Are you a protected veteran?",
    "Disability status", "Sexual orientation",
])
def test_never_fills_demographic_fields(label):
    assert fieldmatch.match_key(label) is None


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
    "Resume", "Resume/CV", "Résumé", "Upload your CV", "Curriculum Vitae",
])
def test_is_resume_field_yes(label):
    assert fieldmatch.is_resume_field(label)


@pytest.mark.parametrize("label", [
    "Cover letter", "Cover Letter (optional)", "Portfolio", "Other attachment", "",
])
def test_is_resume_field_no(label):
    assert not fieldmatch.is_resume_field(label)
