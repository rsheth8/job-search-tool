"""Resume parsing, measured against real PDFs instead of strings.

Every resume bug found so far was invisible to a string fixture, because the
damage happened before any regex ran: pypdf's default extraction welded a
right-aligned date onto the line's last word, split a letter-spaced heading
into "EDUCA TION", and flattened a sidebar layout so that both columns shared
each physical line. A test that starts from text cannot see any of it.

So this corpus starts from PDFs. Each ``<name>.pdf`` has a ``<name>.json``
beside it holding the fields a person reading that resume would write down --
gold written from the document, never from what the parser currently returns.
The LaTeX that produced each PDF is in ``src/``, so a fixture can be read in a
diff and rebuilt.

Adding one: drop a PDF in ``tests/fixtures/resumes/``, write the JSON beside
it, and it is picked up automatically. See the README in that directory.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import profile_import

RESUMES = Path(__file__).parent / "fixtures" / "resumes"
CASES = sorted(p for p in RESUMES.glob("*.pdf"))

# A corpus that quietly shrinks to nothing is worse than no corpus: it goes on
# passing while testing less and less.
assert CASES, f"no resume fixtures found in {RESUMES}"


def _gold(pdf: Path) -> dict:
    doc = pdf.with_suffix(".json")
    assert doc.exists(), f"{pdf.name} has no gold file beside it ({doc.name})"
    return json.loads(doc.read_text())


def _parsed(pdf: Path) -> dict:
    """The real path an upload takes: bytes in, heuristics out.

    Deliberately not ``parse_document`` -- the LLM overlay is not configured in
    tests, and the heuristics are what has to hold on their own anyway, since
    they are what runs whenever a key is missing or the daily slice is spent.
    """
    return profile_import._heuristic_parse(
        profile_import._text_from_bytes(pdf.name, pdf.read_bytes())
    )


def _ids(paths):
    return [p.stem for p in paths]


@pytest.mark.parametrize("pdf", CASES, ids=_ids(CASES))
def test_identity_matches_the_resume(pdf: Path):
    gold = _gold(pdf)
    got = _parsed(pdf)["identity"]
    expected = gold["identity"]
    xfail = gold.get("xfail", {})
    wrong = {
        key: (value, got.get(key))
        for key, value in expected.items()
        if got.get(key) != value and key not in xfail
    }
    assert not wrong, "\n".join(
        f"  {k}: expected {exp!r}, got {act!r}" for k, (exp, act) in wrong.items()
    )


@pytest.mark.parametrize("pdf", CASES, ids=_ids(CASES))
def test_fields_the_resume_does_not_state_stay_empty(pdf: Path):
    """A parser that guesses is worse than one that leaves the box alone: these
    values get autofilled onto real applications."""
    got = _parsed(pdf)["identity"]
    for key in _gold(pdf).get("absent", []):
        assert not (got.get(key) or ""), f"{key} was invented: {got.get(key)!r}"


@pytest.mark.parametrize("pdf", CASES, ids=_ids(CASES))
def test_the_search_location_is_the_candidates_city(pdf: Path):
    gold = _gold(pdf)
    if "locations" not in gold:
        pytest.skip("no location gold for this fixture")
    assert _parsed(pdf)["profile"].get("locations") == gold["locations"]


@pytest.mark.parametrize("pdf", CASES, ids=_ids(CASES))
def test_multiple_degrees_are_read_as_multiple_degrees(pdf: Path):
    """A resume showing two degrees has to produce two entries. One flat set of
    education fields silently keeps whichever it saw last."""
    gold = _gold(pdf)
    if "education" not in gold:
        pytest.skip("one degree on this resume")
    got = _parsed(pdf)["identity"].get("education") or []
    assert got == gold["education"]


@pytest.mark.parametrize("pdf", CASES, ids=_ids(CASES))
def test_a_single_degree_resume_stores_no_list(pdf: Path):
    """It would add a structure saying nothing the flat fields do not."""
    if "education" in _gold(pdf):
        pytest.skip("this resume has more than one degree")
    assert "education" not in _parsed(pdf)["identity"]


@pytest.mark.parametrize("pdf", CASES, ids=_ids(CASES))
def test_every_known_gap_is_still_a_gap(pdf: Path):
    """An xfail that starts passing is good news nobody hears about. This turns
    it into a failing test, so the note gets deleted rather than left to rot."""
    gold = _gold(pdf)
    got = _parsed(pdf)["identity"]
    for key, reason in gold.get("xfail", {}).items():
        want = gold["identity"].get(key)
        assert got.get(key) != want or want is None, (
            f"{pdf.name}: {key} now matches gold -- remove it from xfail.\n"
            f"  recorded reason: {reason}"
        )


def test_every_fixture_carries_its_reason():
    """A fixture without a note is a file nobody can maintain: the next person
    cannot tell what it is guarding, so they cannot tell when it is safe to
    change it."""
    missing = [p.name for p in CASES if not _gold(p).get("note", "").strip()]
    assert not missing, f"fixtures with no note: {missing}"


def test_every_fixture_has_its_latex_source():
    """So a fixture can be read in a diff and rebuilt, rather than being an
    opaque binary that nobody dares touch."""
    missing = [p.name for p in CASES if not (RESUMES / "src" / f"{p.stem}.tex").exists()]
    assert not missing, f"fixtures with no LaTeX source: {missing}"
