"""The questions applications actually ask, beyond name and email.

Each field here was a box the autofill left empty, and most of them were empty
for a reason worth stating:

* "Address line 2" matched the ``address`` rule, so the apartment box got the
  street and the street got nothing;
* "Phone type" contains the word *phone*, so a Mobile/Home/Work select was
  handed a phone number;
* "What is your work authorization status?" contains *work authorization*, so
  the F-1 OPT / H-1B dropdown got a Yes;
* "Referred by" was matched by ``related_to_employee``, a boolean, so a name
  field got the word "No".

First match wins in ``FIELD_RULES``, which is why every one of those is an
ordering bug rather than a missing rule, and why these tests check the losers
as well as the winners.

Two questions are deliberately still unanswered: date of birth and the
criminal-conviction question. Both stay in the never-fill list.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from app import applicant, fieldmatch

ROOT = pathlib.Path(__file__).resolve().parents[1]

NEW_TEXT = (
    "middle_name", "phone_type", "address2", "work_auth_type",
    "security_clearance", "languages", "certifications", "employment_type",
    "referral_name",
)


# --- the fields exist and round-trip --------------------------------------

def test_the_new_fields_are_known_identity_keys():
    for key in NEW_TEXT:
        assert key in applicant.TEXT_FIELDS, key
    assert "drivers_license" in applicant.BOOL_FIELDS


def test_they_round_trip_through_the_identity_blob():
    applicant.set_identity("u1", {
        "middle_name": "Quinn", "phone_type": "Mobile",
        "address2": "Apt 4B", "work_auth_type": "F-1 OPT",
        "security_clearance": "Secret", "languages": "English, Gujarati",
        "certifications": "CompTIA Security+", "employment_type": "Internship",
        "referral_name": "Dana Reed", "drivers_license": True,
    })
    got = applicant.get_identity("u1")
    assert got["address2"] == "Apt 4B"
    assert got["work_auth_type"] == "F-1 OPT"
    assert got["drivers_license"] is True


def test_the_autofill_map_carries_them_to_the_form():
    applicant.set_identity("u1", {"address2": "Suite 900", "drivers_license": True})
    painted = applicant.autofill_map("u1")
    assert painted["address2"] == "Suite 900"
    assert painted["drivers_license"] == "Yes", "bools render as Yes/No for selects"


def test_an_unset_field_is_simply_absent():
    """No value must mean no suggestion, not an empty string painted on."""
    applicant.set_identity("u1", {"first_name": "Ada"})
    assert "security_clearance" not in applicant.autofill_map("u1")


# --- label routing, including what each rule must NOT take ----------------

@pytest.mark.parametrize("label,expected", [
    ("Middle name", "middle_name"),
    ("Middle initial", "middle_name"),
    ("Name (Middle)", "middle_name"),
    ("First name", "first_name"),
    ("Phone type", "phone_type"),
    ("Phone", "phone"),
    ("Mobile phone number", "phone"),
    ("Address line 2", "address2"),
    ("Apt / Suite", "address2"),
    ("Street address", "address"),
    ("Address line 1", "address"),
    ("What is your work authorization status?", "work_auth_type"),
    ("Visa type", "work_auth_type"),
    ("Which of the following describes your work authorization?", "work_auth_type"),
    ("Are you authorized to work in the United States?", "work_authorized"),
    ("Will you now or in the future require sponsorship?", "needs_sponsorship"),
    ("Do you hold an active security clearance?", "security_clearance"),
    ("Clearance level", "security_clearance"),
    ("Do you have a valid driver's license?", "drivers_license"),
    ("Driver’s licence", "drivers_license"),
    ("Licenses & Certifications", "certifications"),
    ("Certifications", "certifications"),
    ("What languages do you speak?", "languages"),
    ("Language proficiency", "languages"),
    ("Employment type", "employment_type"),
    ("What type of position are you seeking?", "employment_type"),
    ("Who referred you?", "referral_name"),
    ("Referred by", "referral_name"),
    ("Are you related to anyone who works here?", "related_to_employee"),
    ("How did you hear about us?", "how_heard"),
    ("Referral source", "how_heard"),
])
def test_label_routes_to_the_right_field(label, expected):
    assert fieldmatch.match_key(label) == expected


def test_the_programming_languages_question_is_not_the_spoken_one():
    """A skills question wearing the word "languages". Filling it with
    "English, Spanish" would be worse than leaving it blank."""
    assert fieldmatch.match_key("Programming languages") is None
    assert fieldmatch.match_key("Languages and frameworks") is None


def test_the_questions_we_still_refuse_to_answer():
    for label in ("Date of birth", "Citizenship status", "Sexual orientation",
                  "National origin", "Gender identity"):
        assert fieldmatch.match_key(label) is None, label
        assert fieldmatch.is_eeo(label), label


def test_a_conviction_question_is_not_answered_as_a_background_check():
    """``background_check`` matches "criminal background"; that's consent to be
    screened. Nothing here answers whether the person has a record."""
    assert "convict" not in fieldmatch.rules_payload()["never_fill"], (
        "if this changes, re-read the comment below")
    # There is no identity key for it, which is the actual guarantee.
    assert not [k for k in applicant.FIELDS if "convict" in k or "felony" in k]


# --- the client mirrors of the schema -------------------------------------

DRAFT = ROOT / "ios/JobPilot/IdentityDraft.swift"


def _block(source: str, start: str, end: str) -> str:
    i = source.index(start) + len(start)
    return source[i:source.index(end, i)]


def _draft_keys():
    src = DRAFT.read_text()
    payload = set(re.findall(r'"([a-z0-9_]+)":', _block(
        src, "let all: [String: Any] = [", "\n        ]")))
    loaded = set(re.findall(r'id\["([a-z0-9_]+)"\]', _block(
        src, "mutating func load", "/// Only the given keys")))
    full = set(re.findall(r'"([a-z0-9_]+)"', _block(
        src, "payload(keys: Set([", "]), omitEmpty:")))
    return payload, loaded, full


def test_the_ios_draft_agrees_with_itself():
    """Four hand-maintained lists describe one schema: the properties, `load`,
    the payload dict and `fullPayload`'s key set. Miss one and a field saves
    but never loads back, silently."""
    payload, loaded, full = _draft_keys()
    assert payload == loaded, (
        f"in payload but never loaded: {sorted(payload - loaded)}; "
        f"loaded but never sent: {sorted(loaded - payload)}")
    assert payload == full, (
        f"the You-tab editor doesn't send: {sorted(payload - full)}")


def test_every_ios_key_is_a_field_the_server_knows():
    payload, _, _ = _draft_keys()
    unknown = sorted(payload - set(applicant.FIELDS))
    assert unknown == [], f"iOS sends keys the server drops: {unknown}"


def test_the_quiz_actually_saves_each_new_field():
    """A field with a UI but no key in its step's save set is a box that
    forgets what you typed the moment you tap Next."""
    src = (ROOT / "ios/JobPilot/SetupView.swift").read_text()
    saved = set(re.findall(r'"([a-z0-9_]+)",?\s*(?=[,\]\n])',
                           "\n".join(re.findall(r"saveIdentity\(\[(.*?)\]\)",
                                                src, re.S))))
    for key in NEW_TEXT + ("drivers_license",):
        assert key in saved, f"{key} has no step that saves it"
