"""Unbiased autofill corpus.

Gold is written from the HTML a human sees (selector → value), not from
`match_key()`. Several labels are chosen specifically so they fail the regex
table and only succeed via ATS name/id or autocomplete — the signal Simplify
relies on.
"""
from __future__ import annotations

import functools
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("playwright", reason="browser tests need the playwright package")

from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from tests.browserutil import skip_unless_ci_chromium  # noqa: E402
from tests.test_ios_autofill import _extract_lib  # noqa: E402
from app import fieldmatch  # noqa: E402

FORMS_DIR = Path(__file__).parent / "fixtures" / "forms"

# Complete identity, as `applicant.autofill_map` would send to the WebView.
IDENTITY = {
    "first_name": "Rahil",
    "last_name": "Sheth",
    "full_name": "Rahil Sheth",
    "email": "rahil@example.com",
    "phone": "555-0100",
    "address": "1 Main St",
    "city": "Chicago",
    "state": "IL",
    "zip": "60601",
    "country": "United States",
    "location": "Chicago, IL",
    "linkedin": "https://linkedin.com/in/rahil",
    "github": "https://github.com/rahil",
    "portfolio": "https://rahil.dev",
    "school": "University of Minnesota Twin Cities",
    "degree": "B.S. Computer Science (May 2026); M.S. Data Science (in progress)",
    "discipline": "Data Science",
    "gpa": "3.5",
    "grad_year": "December 2027",
    "grad_month": "December",
    "grad_year_num": "2027",
    "intern_season": "Summer",
    "current_company": "HCSC",
    "current_title": "Software Engineer Intern",
    "how_heard": "LinkedIn",
    "work_authorized": "Yes",
    "needs_sponsorship": "No",
    "gender": "Male",
    "hispanic_latino": "No",
}

# Labels that must stay independent of FIELD_RULES — otherwise this suite
# collapsed into testing the regexes that generated it.
UNKNOWN_LABELS = [
    "Applicant",
    "Reach us at",
    "Daytime",
    "Based out of",
    "Professional profile",
    "Code samples",
    "Anything else (URL)",
    "Programme",
    "Subject",
    "Where you've been",
    "What you did there",
    "What should we call you?",
    "Best inbox",
    "Question 1",
    "Question 9",
]


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


@pytest.fixture(scope="module")
def server():
    handler = functools.partial(_QuietHandler, directory=str(FORMS_DIR))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture(scope="module")
def browser():
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            yield b
            b.close()
    except PlaywrightError as e:
        skip_unless_ci_chromium(e)


@pytest.fixture(scope="module")
def lib():
    return _extract_lib()


def _run(browser, server, lib, fixture, identity):
    page = browser.new_page()
    payload = {
        "identity": identity,
        "answers": [],
        "rules": fieldmatch.rules_payload(),
    }
    page.add_init_script(f"window.__APPLY = {json.dumps(payload)};")
    page.goto(f"{server}/{fixture}")
    page.evaluate(lib)
    page.evaluate("window.__applyAutofill()")
    return page


def _read(page, spec):
    sel = spec["sel"]
    via = spec.get("via", "value")
    loc = page.locator(sel)
    if loc.count() == 0:
        return None
    if via == "checked":
        el = page.query_selector(sel)
        return el.get_attribute("value") if el else None
    return loc.input_value()


def test_corpus_labels_are_not_the_regex_table():
    """If every gold label already matches FIELD_RULES, the corpus is biased."""
    leaked = [lab for lab in UNKNOWN_LABELS if fieldmatch.match_key(lab)]
    assert not leaked, f"gold labels collapsed into FIELD_RULES: {leaked}"


def test_match_field_uses_attrs_when_the_label_is_noise():
    assert fieldmatch.match_field(
        "Applicant", name="job_application[first_name]") == "first_name"
    assert fieldmatch.match_field(
        "Reach us at", name="job_application[email]", input_type="email") == "email"
    assert fieldmatch.match_field("Question 1", autocomplete="given-name") == "first_name"
    assert fieldmatch.match_field(
        "What should we call you?", name="_systemfield_name") == "full_name"
    assert fieldmatch.match_field(
        "Code samples", name="job_application[urls][GitHub]") == "github"
    assert fieldmatch.match_field(
        "What you did there", name="job_application[employments][][title]") == (
        "current_title")
    # Visible EEO still wins over a tempting name.
    assert fieldmatch.match_field(
        "Gender identity", name="job_application[first_name]") is None
    assert fieldmatch.match_field("Birthday", autocomplete="bday") is None


CASES = [
    ("corpus_greenhouse_attrs.html", IDENTITY, [
        ("#fn", "Rahil"),
        ("#ln", "Sheth"),
        ("#em", "rahil@example.com"),
        ("#ph", "555-0100"),
        ("#loc", "Chicago, IL"),
        ("#li", "https://linkedin.com/in/rahil"),
        ("#gh", "https://github.com/rahil"),
        ("#web", "https://rahil.dev"),
        ("#school", "University of Minnesota Twin Cities"),
        ("#deg", "Master's Degree"),
        ("#disc", "Data Science"),
        ("#co", "HCSC"),
        ("#ti", "Software Engineer Intern"),
        ("#hear", "LinkedIn"),
        ("#snack", ""),
    ]),
    ("corpus_autocomplete.html", IDENTITY, [
        ("#q1", "Rahil"),
        ("#q2", "Sheth"),
        ("#q3", "rahil@example.com"),
        ("#q4", "555-0100"),
        ("#q5", "1 Main St"),
        ("#q6", "Chicago"),
        ("#q7", "IL"),
        ("#q8", "60601"),
        ("#q9", "United States"),
        ("#q10", "HCSC"),
        ("#q11", "Software Engineer Intern"),
        ("#q12", "https://rahil.dev"),
    ]),
    ("corpus_ashby_attrs.html", IDENTITY, [
        ("#_systemfield_name", "Rahil Sheth"),
        ("#_systemfield_email", "rahil@example.com"),
        ("#_systemfield_phone", "555-0100"),
        ("#_systemfield_location", "Chicago, IL"),
    ]),
    ("corpus_traps.html", IDENTITY, [
        ("#first", "Rahil"),
        ("#email", "rahil@example.com"),
        ("#curpay", ""),
        ("#code", ""),
        ("#color", ""),
        ("#salut", ""),
        ("#bday", ""),
        ("#orient", ""),
        ("#gid", ""),
        ("#citizen", ""),
        ("#origin", ""),
    ]),
]


def test_corpus_fills_gold_and_never_wrong_fills(browser, server, lib):
    """Precision 1.0: a wrong fill fails the test. Every gold cell must match."""
    wrong, miss = [], []
    for fixture, ident, fields in CASES:
        page = _run(browser, server, lib, fixture, ident)
        try:
            for sel, expect in fields:
                got = _read(page, {"sel": sel})
                if got is None:
                    miss.append(f"{fixture} {sel} missing")
                    continue
                if (got or "") != expect:
                    if expect == "":
                        wrong.append(f"{fixture} {sel}: filled {got!r}, must stay empty")
                    else:
                        miss.append(f"{fixture} {sel}: got {got!r}, want {expect!r}")
        finally:
            page.close()
    assert not wrong, "wrong fills (precision):\n  " + "\n  ".join(wrong)
    assert not miss, "misses (recall):\n  " + "\n  ".join(miss)


def test_intern_bands_and_compound_work_auth(browser, server, lib):
    ident = {**IDENTITY, "needs_sponsorship": "Yes"}
    page = _run(browser, server, lib, "corpus_intern_bands.html", ident)
    try:
        assert page.input_value("#gpa") == "3.1 - 3.6"
        assert page.input_value("#when") == "Sept - Dec 2027"
        assert page.input_value("#season") == "Summer 2027"
        assert page.input_value("#hisp") == "Not Hispanic or Latino"
        assert page.input_value("#cc") == "United States+1"
        assert page.input_value("#deg") == "Master's Degree"
        # Authorized, but needs a visa → cannot work *without* sponsorship.
        combo = page.query_selector("input[name=combo_auth]:checked")
        assert combo and combo.get_attribute("value") == "No"
        # US identity on a Canada-only auth question.
        canada = page.query_selector("input[name=canada_auth]:checked")
        assert canada and canada.get_attribute("value") == "No"
    finally:
        page.close()
