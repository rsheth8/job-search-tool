"""Reading a location off a résumé without inventing one.

From a real résumé that set City="React" and State="AI" on live applications.
The line responsible:

    Event Director – Social Coding Club (led 5+ workshops on React, AI/ML, ...)

`([A-Z]{2})` accepts any two capitals, so "React, AI" is a perfectly good
"City, ST" as far as the pattern is concerned. Two things made it worse:

* The *real* location — "University of Minnesota, Minneapolis, MNAug 2023" —
  was missed entirely, because PDF extraction runs the right-aligned date into
  the location and `\\b` finds no boundary between "MN" and "Aug". So the only
  true location in the document was invisible while three false ones were not.
* Every other guard in this module protects the `locations` search field.
  Identity city/state came straight off the regex, ungated — and identity is
  what gets typed into an employer's form.
"""
from __future__ import annotations

import pytest

from app.profile_import import _find_city_state, _parse_location, parse_document


# Verbatim from the résumé that produced the bug, including the "MNAug" run-on
# that PDF text extraction actually emits.
REAL_RESUME = """Rahil Sheth
(224) 374-9073 — rahilsheth05@gmail.com — linkedin.com/in/rsheth8
EDUCA TION & LEADERSHIP
University of Minnesota, Minneapolis, MNAug 2023 – Present
M.S. Data Science(In Progress) —B.S. Computer Science(May 2026)
Leadership:Co-President – UMN Cricket Club (200+ members),
Event Director – Social Coding Club (led 5+ workshops on React, AI/ML, System Design)
PROFESSIONAL EXPERIENCE
Blue Cross Blue Shield (HCSC)–Software Developer Intern June – Aug 2025
– Shipped 20+ production tickets via React Native and Java Spring Boot, with zero rollbacks
– Containerised the service with Docker, AWS ECS, and Terraform
"""


def test_the_real_resume_yields_the_real_city():
    assert _find_city_state(REAL_RESUME) == ("Minneapolis", "MN")


@pytest.mark.parametrize("phrase", [
    "workshops on React, AI/ML, System Design",
    "Java Spring Boot, with zero rollbacks",
    "Docker, AWS ECS, and Terraform",
    "built with Postgres, MY own schema",
])
def test_a_skill_pair_is_not_a_location(phrase):
    """Each of these matched the old pattern. None is a place."""
    assert _find_city_state(phrase) is None


def test_a_state_run_into_the_next_word_is_still_found():
    """PDF extraction produces this constantly — a right-aligned date colliding
    with the location beside it."""
    assert _find_city_state("Minneapolis, MNAug 2023 – Present") == ("Minneapolis", "MN")
    assert _find_city_state("Chicago, ILMay 2024") == ("Chicago", "IL")


def test_a_lowercase_continuation_is_still_rejected():
    """The boundary was loosened to accept "MNAug"; it must not accept a state
    code that is really the start of a longer lowercase word."""
    assert _find_city_state("Minneapolis, MNesota") is None


def test_the_first_valid_match_wins_not_the_first_match():
    """A skill pair appearing before the location must not shadow it."""
    text = "skills: React, AI and Node\nlives in Austin, TX"
    assert _find_city_state(text) == ("Austin", "TX")


@pytest.mark.parametrize("city,state", [
    ("Minneapolis", "MN"), ("Chicago", "IL"), ("New York", "NY"),
    ("San Francisco", "CA"), ("Washington", "DC"), ("Toronto", "ON"),
])
def test_real_places_still_parse(city, state):
    assert _find_city_state(f"{city}, {state}") == (city, state)


def test_state_is_normalised_to_upper():
    assert _find_city_state("Austin, TX")[1] == "TX"


def test_parse_location_uses_the_same_guard():
    """`_parse_location` handles the profile's free-text location field; it must
    not accept what the résumé path rejects."""
    assert _parse_location("Minneapolis, MN") == {"city": "Minneapolis", "state": "MN"}
    # "AI" is not a state, so this falls through to the comma split, which
    # correctly refuses to call a two-letter non-state a state.
    assert _parse_location("React, AI").get("state") is None


def test_end_to_end_extraction_does_not_put_a_skill_in_the_city_field():
    """The whole point: identity is what gets typed into an employer's form."""
    identity = (parse_document(REAL_RESUME) or {}).get("identity") or {}
    assert identity.get("city") == "Minneapolis"
    assert identity.get("state") == "MN"
