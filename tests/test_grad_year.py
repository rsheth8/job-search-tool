"""When does the degree end?

The parser used to take the first month-labelled year in the education section.
On the commonest header shape there is -- an enrolment range on the school line
and the graduation date beside the degree -- that is the year the person
*started*:

    University of Minnesota, Minneapolis, MN    Aug 2023 - Present
    B.S. Computer Science(May 2026)

which returned 2023. It went onto applications as a graduation year, and
_roles_and_seniority reads the same field to decide whether someone is a new
grad, so it also mis-sorted the jobs they were shown.
"""
from __future__ import annotations

import pytest

from app import profile_import


def _year(doc: str) -> str:
    return profile_import._grad_year_from_text(
        profile_import._education_block(doc), doc
    )


ENROLMENT_AND_DEGREE = """Rahil Sheth

EDUCATION & LEADERSHIP

University of Minnesota, Minneapolis, MN            Aug 2023 - Present
M.S. Data Science(In Progress) — B.S. Computer Science(May 2026)
"""


def test_the_enrolment_year_is_not_the_graduation_year():
    """The reported bug, in the shape it was reported in."""
    assert _year(ENROLMENT_AND_DEGREE) == "2026"


def test_it_reads_off_a_finished_range():
    assert _year("EDUCATION\nUniv X   Sep 2015 - May 2019\nB.S. CS\n") == "2019"


@pytest.mark.parametrize("dash", ["-", "–", "—", "to"])
def test_every_dash_a_resume_uses_is_understood(dash):
    """en dash, em dash and the word "to" all show up in real resumes."""
    doc = f"EDUCATION\nUniv X   Aug 2023 {dash} Present\nB.S.(May 2026)\n"
    assert _year(doc) == "2026"


# --- an explicit promise outranks counting ------------------------------

def test_expected_beats_a_bigger_stray_year():
    doc = """EDUCATION
Univ X   Aug 2019 - Present
M.S. Data Science (expected December 2027)
Dean's List (2019-2028)
"""
    assert _year(doc) == "2027"


def test_class_of_is_believed():
    assert _year("EDUCATION\nUniv X  Class of 2028\nAug 2024 - Present\n") == "2028"


def test_graduating_is_believed():
    assert _year("EDUCATION\nUniv X\nB.S. CS, graduating May 2027\n") == "2027"


# --- refusing to guess ---------------------------------------------------

def test_an_unfinished_degree_with_no_end_date_returns_nothing():
    """The start year is not an answer. An empty field the user fills in beats a
    wrong one autofilled onto a real application."""
    assert _year("EDUCATION\nUniv X   Aug 2023 - Present\nPh.D. Statistics\n") == ""


def test_a_year_is_only_dropped_where_it_opens_the_open_range():
    """Exclusion is positional, so the same year stated properly elsewhere still
    counts -- otherwise one "2023 - Present" would erase every 2023."""
    doc = """EDUCATION
Univ X   Aug 2020 - Present
B.S. Computer Science, May 2023
"""
    assert _year(doc) == "2023"


def test_no_years_at_all_is_empty_not_an_error():
    assert _year("EDUCATION\nUniv X\nB.S. Computer Science\n") == ""


# --- the downstream field this feeds ------------------------------------

def test_seniority_follows_the_corrected_year():
    """_roles_and_seniority reads grad_year; the old value flipped this."""
    got = profile_import._heuristic_parse(ENROLMENT_AND_DEGREE)
    assert got["identity"]["grad_year"] == "2026"
