"""More than one degree.

A single flat set of education fields cannot say "bachelor's finished, master's
under way", which is an ordinary thing to be: it is most of the people this app
is for. ``education`` is a list, and the flat keys autofill already paints onto
forms are derived from it, so nothing migrates and no caller has to change.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import applicant

THIS_YEAR = datetime.now(timezone.utc).year

BACHELORS = {"school": "University of Minnesota", "degree": "B.S.",
             "discipline": "Computer Science", "gpa": "3.5",
             "grad_year": str(THIS_YEAR - 1), "status": "completed"}
MASTERS = {"school": "University of Minnesota", "degree": "M.S.",
           "discipline": "Data Science", "start_year": str(THIS_YEAR - 1),
           "status": "in_progress"}


@pytest.fixture
def user():
    applicant.set_identity("edu_u", {"education": [], "school": "", "degree": "",
                                     "discipline": "", "gpa": "", "grad_year": ""})
    return "edu_u"


# --- the list drives the flat fields -------------------------------------

def test_the_degree_in_progress_is_the_one_forms_get(user):
    """A form asking "school" means the one you are at now."""
    applicant.set_identity(user, {"education": [BACHELORS, MASTERS]})
    got = applicant.get_identity(user)
    assert got["degree"] == "M.S."
    assert got["discipline"] == "Data Science"


def test_both_degrees_survive_in_order(user):
    applicant.set_identity(user, {"education": [BACHELORS, MASTERS]})
    assert [e["degree"] for e in applicant.get_identity(user)["education"]] \
        == ["M.S.", "B.S."]


def test_a_single_free_text_box_gets_both(user):
    """Plenty of forms have one education field and nothing else."""
    applicant.set_identity(user, {"education": [BACHELORS, MASTERS]})
    summary = applicant.get_identity(user)["degrees"]
    assert "M.S. Data Science" in summary and "B.S. Computer Science" in summary
    assert "in progress" in summary


def test_a_finished_degrees_gpa_still_answers_the_gpa_question(user):
    """The degree in progress has no GPA yet. The one already earned does, and
    that is a true fact about the person whichever block asks for it."""
    applicant.set_identity(user, {"education": [BACHELORS, MASTERS]})
    assert applicant.get_identity(user)["gpa"] == "3.5"


def test_school_and_degree_are_never_mixed_across_entries(user):
    """The half that must not be clever: an education block describes one
    degree, and pairing a master's title with a bachelor's school is how a
    wrong fact lands on a real application."""
    applicant.set_identity(user, {"education": [
        {"school": "State College", "degree": "B.A.", "grad_year": str(THIS_YEAR - 4),
         "status": "completed"},
        {"school": "Tech Institute", "degree": "M.S.", "status": "in_progress"},
    ]})
    got = applicant.get_identity(user)
    assert got["degree"] == "M.S." and got["school"] == "Tech Institute"


# --- in progress or not --------------------------------------------------

def test_an_explicit_status_is_believed_over_the_dates():
    assert applicant.is_in_progress({"status": "in_progress", "grad_year": "2000"})
    assert not applicant.is_in_progress({"status": "completed",
                                         "grad_year": str(THIS_YEAR + 5)})


def test_a_future_graduation_year_means_still_studying():
    assert applicant.is_in_progress({"grad_year": str(THIS_YEAR + 2)})


def test_a_start_year_with_no_end_means_still_studying():
    assert applicant.is_in_progress({"start_year": str(THIS_YEAR - 1)})


def test_a_degree_ending_this_year_is_called_finished():
    """Genuinely ambiguous without a month. Finished is the conservative read:
    claiming to still be enrolled is the more embarrassing way to be wrong."""
    assert not applicant.is_in_progress({"grad_year": str(THIS_YEAR)})


# --- nothing changes for a profile that never uses it --------------------

def test_a_single_degree_profile_is_untouched(user):
    applicant.set_identity(user, {"school": "Rice", "degree": "B.S.",
                                  "grad_year": "2019"})
    got = applicant.get_identity(user)
    assert got["school"] == "Rice" and got["degree"] == "B.S."


def test_a_single_degree_profile_still_reads_as_a_list(user):
    """So the app codes against one shape whether or not this user has two."""
    applicant.set_identity(user, {"school": "Rice", "degree": "B.S.",
                                  "grad_year": "2019"})
    assert applicant.get_identity(user)["education"] == [
        {"school": "Rice", "degree": "B.S.", "grad_year": "2019"}
    ]


def test_a_client_that_predates_the_list_still_edits_the_right_degree(user):
    """The old app writes flat fields. Routed into the entry they describe --
    otherwise the write would be silently derived over on the next read."""
    applicant.set_identity(user, {"education": [BACHELORS, MASTERS]})
    applicant.set_identity(user, {"school": "Carnegie Mellon"})
    got = applicant.get_identity(user)
    assert got["school"] == "Carnegie Mellon"
    assert got["education"][0]["school"] == "Carnegie Mellon"
    assert got["education"][1]["school"] == "University of Minnesota", "wrong entry"


# --- what goes onto a form ------------------------------------------------

def test_the_list_is_never_painted_onto_a_form(user):
    applicant.set_identity(user, {"education": [BACHELORS, MASTERS]})
    assert "education" not in applicant.autofill_map(user)


def test_the_summary_is_available_to_autofill(user):
    applicant.set_identity(user, {"education": [BACHELORS, MASTERS]})
    assert "M.S." in applicant.autofill_map(user)["degrees"]


# --- input hygiene --------------------------------------------------------

def test_junk_entries_are_dropped():
    assert applicant.clean_education(["a string", {}, None, 7]) == []


def test_an_unknown_status_word_is_dropped_not_believed():
    assert "status" not in applicant.clean_education([{"degree": "B.S.",
                                                       "status": "maybe?"}])[0]


def test_duplicate_degrees_collapse():
    got = applicant.clean_education([BACHELORS, dict(BACHELORS)])
    assert len(got) == 1


def test_the_list_is_capped():
    got = applicant.clean_education(
        [{"degree": "B.S.", "school": f"School {i}"} for i in range(20)]
    )
    assert len(got) == applicant.MAX_EDUCATION


def test_clearing_the_list_restores_the_flat_fields(user):
    applicant.set_identity(user, {"education": [BACHELORS, MASTERS]})
    applicant.set_identity(user, {"education": []})
    got = applicant.get_identity(user)
    assert got["education"] == [] and not got.get("degrees")


# --- reading two degrees off a resume ------------------------------------

RESUME = """Rahil Sheth
Minneapolis, MN | rahil@example.com

EDUCATION & LEADERSHIP
University of Minnesota, Minneapolis, MN            Aug 2023 - Present
M.S. Data Science(In Progress)  B.S. Computer Science(May 2026)  GPA: 3.5/4.0

PROFESSIONAL EXPERIENCE
Acme Corp - Engineer
"""


def test_a_resume_with_two_degrees_on_one_line_yields_two_entries():
    """They share a line more often than not, so the split is on the degree
    tokens themselves rather than on newlines."""
    from app import profile_import

    got = profile_import._heuristic_parse(RESUME)["identity"]["education"]
    assert [e["degree"] for e in got] == ["M.S.", "B.S."]
    assert got[0]["discipline"] == "Data Science"
    assert got[1]["discipline"] == "Computer Science"


def test_the_in_progress_marker_is_read():
    from app import profile_import

    got = profile_import._heuristic_parse(RESUME)["identity"]["education"]
    assert got[0]["status"] == "in_progress"
    assert got[1]["status"] == "completed"


def test_one_degree_does_not_produce_a_stored_list():
    """It would add a structure that says nothing the flat fields do not."""
    from app import profile_import

    one = RESUME.replace("M.S. Data Science(In Progress)  ", "")
    assert "education" not in profile_import._heuristic_parse(one)["identity"]


def test_importing_a_resume_saves_both_degrees(user):
    from app import profile_import

    profile_import.import_resume(user, text=RESUME)
    got = applicant.get_identity(user)["education"]
    assert [e["degree"] for e in got] == ["M.S.", "B.S."]


def test_an_import_does_not_overwrite_a_list_you_curated(user):
    """Imports write empty fields only -- that rule holds for the list too."""
    from app import profile_import

    mine = [dict(MASTERS, school="Carnegie Mellon"), BACHELORS]
    applicant.set_identity(user, {"education": mine})
    profile_import.import_resume(user, text=RESUME)
    assert applicant.get_identity(user)["education"][0]["school"] == "Carnegie Mellon"
