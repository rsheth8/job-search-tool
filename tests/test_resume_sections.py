"""Section headings, PDF text extraction, and what happens when the LLM is off.

Three bugs sat behind one symptom -- a resume importing "React" as a city and
"AI" as a state:

* the heading regexes demanded a bare keyword alone on its line, so the very
  common "EDUCATION & LEADERSHIP" matched nothing and the whole section was
  invisible. Every field under it then fell back to a whole-document scan;
* pypdf's default extraction discards column geometry, welding a right-aligned
  date onto the line's last word and splitting letter-spaced headings
  ("EDUCA TION"), which is what made the section unmatchable in the first place;
* when the paid overlay was skipped the response looked identical to a good
  parse, so nobody could tell a regex-only result from a checked one.
"""
from __future__ import annotations

import sys
import types

from app import config, profile_import


# The header shape that broke it: a compound heading, and a right-aligned date
# in the same line as the city and state.
COMPOUND = """Rahil Sheth
(224) 374-9073 — rahil@example.com

EDUCATION & LEADERSHIP

University of Minnesota, Minneapolis, MN            Aug 2023 - Present
B.S. Computer Science(May 2026) — Undergrad GPA: 3.5/4.0

PROFESSIONAL EXPERIENCE

Acme Corp - Software Engineer Intern                 June - Aug 2025
– Shipped twenty production tickets across an eight week cycle

KEY PROJECTS

Routewise — a transit planner used by four hundred people each week

SKILLS & AWARDS
Python, FastAPI, PostgreSQL
"""


# --- compound headings ----------------------------------------------------

def test_a_compound_heading_still_opens_its_section():
    """"EDUCATION & LEADERSHIP" used to match nothing, so the block was empty."""
    block = profile_import._education_block(COMPOUND)
    assert block, "education section came back empty"
    assert "University of Minnesota" in block


def test_the_education_block_stops_at_the_next_heading():
    """It must not run on into experience, or the school scan picks up a title."""
    block = profile_import._education_block(COMPOUND)
    assert "Acme Corp" not in block


def test_fields_under_a_compound_heading_are_read_from_it():
    got = profile_import._heuristic_parse(COMPOUND)["identity"]
    assert got.get("city") == "Minneapolis"
    assert got.get("state") == "MN"
    # Previously "University of Minnesota Mar" -- the unbounded whole-document
    # scan ran past the section and welded on the next capitalised token.
    assert got.get("school") == "University of Minnesota"
    assert got.get("gpa") == "3.5"


def test_a_compound_heading_also_works_as_a_terminator():
    """"SKILLS & AWARDS" has to close the projects section, not just open one."""
    items = profile_import._projects_from_text(COMPOUND)
    assert items, "projects section came back empty"
    assert not any("PostgreSQL" in i["text"] for i in items)


def test_a_bare_heading_is_unaffected():
    """The pattern got looser; it must not have got narrower anywhere."""
    plain = COMPOUND.replace("EDUCATION & LEADERSHIP", "EDUCATION")
    assert "University of Minnesota" in profile_import._education_block(plain)


def test_a_trailing_colon_is_a_heading_too():
    colon = COMPOUND.replace("EDUCATION & LEADERSHIP", "Education:")
    assert "University of Minnesota" in profile_import._education_block(colon)


def test_prose_that_merely_starts_with_the_keyword_is_not_a_heading():
    """The reason the tail words must be capitalised: this line is not a heading,
    and reading it as one would hand the parser somebody's bullet list."""
    prose = """Rahil Sheth

SUMMARY
Education outreach for local schools
Experience shipping payment systems at scale

PROFESSIONAL EXPERIENCE
Acme Corp - Engineer
"""
    assert profile_import._education_block(prose) == ""


def test_a_heading_is_not_matched_mid_line():
    """A bullet mentioning education must not open the section."""
    mid = """Rahil Sheth

PROFESSIONAL EXPERIENCE
– Ran education programs for two hundred students each term
"""
    assert profile_import._education_block(mid) == ""


# --- PDF extraction mode --------------------------------------------------

class _Page:
    """A page that reports which extraction mode it was asked for."""

    def __init__(self, *, layout=None, plain="", raises=False):
        self.layout, self.plain, self.raises = layout, plain, raises
        self.seen: list[str] = []

    def extract_text(self, **kw):
        mode = kw.get("extraction_mode", "plain")
        self.seen.append(mode)
        if mode == "layout":
            if self.raises:
                raise ValueError("this generator confuses pypdf")
            return self.layout
        return self.plain


def test_layout_extraction_is_preferred():
    page = _Page(layout="Minneapolis, MN      Aug 2023", plain="Minneapolis, MNAug 2023")
    assert profile_import._page_text(page) == "Minneapolis, MN      Aug 2023"
    assert page.seen == ["layout"], "plain mode should not have been needed"


def test_plain_extraction_is_the_fallback_when_layout_raises():
    page = _Page(raises=True, plain="Minneapolis, MNAug 2023")
    assert profile_import._page_text(page) == "Minneapolis, MNAug 2023"
    assert page.seen == ["layout", "plain"]


def test_plain_extraction_is_the_fallback_when_layout_is_empty():
    page = _Page(layout="   ", plain="real text")
    assert profile_import._page_text(page) == "real text"


def test_a_page_neither_mode_can_read_is_empty_not_an_error():
    assert profile_import._page_text(_Page(raises=True, plain="")) == ""


def test_column_gaps_do_not_survive_into_stored_bullets():
    """Layout mode separates columns with runs of spaces. They are structure for
    the regexes and noise in a stored fact, so they get collapsed on the way in."""
    items = profile_import._experience_from_text(COMPOUND)
    assert items, "experience section came back empty"
    assert not any("  " in i["text"] for i in items)


# --- the overlay being skipped is visible ---------------------------------

def _keyed(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    config.get_settings.cache_clear()


def test_a_spent_budget_warns_instead_of_degrading_quietly(monkeypatch):
    """A regex-only parse used to be indistinguishable from a checked one."""
    _keyed(monkeypatch)
    from app import llm_budget
    monkeypatch.setattr(llm_budget, "consume", lambda *a, **k: False)
    got = profile_import.parse_document(COMPOUND)
    assert "limit" in got.get("warning", "").lower()
    # And it still parses -- the warning is a caveat, not a failure.
    assert got["identity"].get("city") == "Minneapolis"


def test_no_key_configured_is_not_worth_warning_about(monkeypatch):
    """Heuristics are the whole design without a key, not a degraded mode."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config.get_settings.cache_clear()
    assert not profile_import.parse_document(COMPOUND).get("warning")


def test_a_dead_api_warns_too(monkeypatch):
    _keyed(monkeypatch)
    from app import llm_budget
    monkeypatch.setattr(llm_budget, "consume", lambda *a, **k: True)

    class _Messages:
        def create(self, **kw):
            raise RuntimeError("connection reset")

    mod = types.ModuleType("anthropic")
    mod.Anthropic = lambda api_key=None: types.SimpleNamespace(messages=_Messages())
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    got = profile_import.parse_document(COMPOUND)
    assert got.get("warning")
    assert got["identity"].get("city") == "Minneapolis"


def test_llm_parse_always_returns_a_reason(monkeypatch):
    """The contract the caller relies on to decide whether to warn."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config.get_settings.cache_clear()
    result, reason = profile_import._llm_parse(COMPOUND)
    assert result is None and reason == ""


def test_a_bounded_section_keeps_a_later_mention_out_of_the_school_name():
    """The concrete shape of the original bug. With the section invisible, the
    school scan ran over the whole document and picked up a second, worse
    mention -- the real resume produced "University of Minnesota Mar"."""
    doc = """Rahil Sheth

EDUCATION & LEADERSHIP

University of Minnesota, Minneapolis, MN            Aug 2023 - Present
B.S. Computer Science

PROFESSIONAL EXPERIENCE

Acme Corp - Engineer
– Volunteer tutor at University of Minnesota Mar 2024 through Dec 2024
"""
    assert profile_import._heuristic_parse(doc)["identity"]["school"] \
        == "University of Minnesota"
