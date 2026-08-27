"""The desktop extension's bulk autofill, run against real forms in Chromium.

`extension/content.js` is an MV3 content script. These tests inject the shipping
file (with `window.__APPLY_TEST` so it skips chrome.storage init), set identity
and rules the same way production would after fetch, and call `bulkAutofill`.

The extension is a thinner engine than iOS: it fills text, native <select>, and
Yes/No *radios*. It does not draft essays in bulk, and it does not click Ashby
Yes/No <button> pairs. Those gaps are pinned here so they can't be confused with
a regression in the fields it *does* own.

Skips cleanly where Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("playwright", reason="browser tests need the playwright package")

from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from tests.browserutil import skip_unless_ci_chromium  # noqa: E402
from app import fieldmatch  # noqa: E402

FORMS_DIR = Path(__file__).parent / "fixtures" / "forms"
CONTENT_JS = Path(__file__).resolve().parents[1] / "extension" / "content.js"

IDENTITY = {
    "first_name": "Rahil", "last_name": "Sheth", "full_name": "Rahil Sheth",
    "email": "rahil@example.com", "phone": "555-0100",
    "location": "Chicago, IL", "country": "United States",
    "linkedin": "https://linkedin.com/in/rahil", "github": "https://github.com/rahil",
    "school": "State University", "current_company": "Acme Corp",
    "work_authorized": "Yes", "needs_sponsorship": "No",
    "gender": "Male", "race": "Asian",
    "veteran_status": "I am not a protected veteran",
    "disability_status": "No, I do not have a disability",
}


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):  # noqa: N802
        body = b"<html><body>SUBMITTED</body></html>"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


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
    src = CONTENT_JS.read_text()
    assert "window.__applyBulkAutofill" in src, (
        "content.js lost its test seam — bulkAutofill is no longer injectable")
    return src


def run_autofill(browser, server, lib, fixture, *, identity=None, rules="served"):
    """Load a fixture, inject shipping content.js, bulk-fill, return the page."""
    page = browser.new_page()
    page.add_init_script("window.__APPLY_TEST = true;")
    page.goto(f"{server}/{fixture}")
    page.evaluate(lib)
    payload = None if rules == "bundled" else (
        {"rules": [["email", "([unclosed"]], "never_fill": "gender"}
        if rules == "malformed" else fieldmatch.rules_payload()
    )
    page.evaluate(
        """([identity, payload]) => {
            window.__applyUseIdentity(identity);
            window.__applyUseRules(payload);
            window.__applyBulkAutofill();
        }""",
        [identity if identity is not None else IDENTITY, payload],
    )
    return page


def test_fills_identity_and_native_select(browser, server, lib):
    page = run_autofill(browser, server, lib, "greenhouse_basic.html")
    try:
        assert page.input_value("#first_name") == "Rahil"
        assert page.input_value("#last_name") == "Sheth"
        assert page.input_value("#email") == "rahil@example.com"
        assert page.input_value("#phone") == "555-0100"
        assert page.input_value("#location") == "Chicago, IL"
        assert page.input_value("#linkedin") == "https://linkedin.com/in/rahil"
        assert page.input_value("#country") == "United States"
        assert page.input_value("#favorite") == ""
    finally:
        page.close()


def test_answers_the_yes_no_radio_group(browser, server, lib):
    page = run_autofill(browser, server, lib, "custom_dropdown.html")
    try:
        assert page.is_checked("input[name='sponsorship'][value='No']")
        assert not page.is_checked("input[name='sponsorship'][value='Yes']")
    finally:
        page.close()


def test_bulk_fill_does_not_draft_essays(browser, server, lib):
    """Bulk fill counts textareas as needing the human; it does not POST /apply/answer."""
    page = run_autofill(browser, server, lib, "custom_dropdown.html")
    try:
        assert page.input_value("#why") == ""
    finally:
        page.close()


def test_never_submits_the_form(browser, server, lib):
    for fixture in ("greenhouse_basic.html", "custom_dropdown.html", "eeo_present.html"):
        page = run_autofill(browser, server, lib, fixture)
        try:
            assert "SUBMITTED" not in page.content()
        finally:
            page.close()


def test_fills_optional_eeo_when_identity_has_values(browser, server, lib):
    page = run_autofill(browser, server, lib, "eeo_present.html")
    try:
        assert page.input_value("#first_name") == "Rahil"
        assert page.input_value("#gender") == "Male"
        assert page.input_value("#race") == "Asian"
        assert page.input_value("#veteran") == "I am not a protected veteran"
        assert page.input_value("#disability") == "No, I do not have a disability"
        assert page.input_value("#hispanic") == ""
        assert page.input_value("#orientation") == ""
        assert not page.is_checked("input[name='gender_identity'][value='Yes']")
        assert not page.is_checked("input[name='gender_identity'][value='No']")
    finally:
        page.close()


def test_optional_eeo_fills_on_bundled_fallback_too(browser, server, lib):
    page = run_autofill(browser, server, lib, "eeo_present.html", rules="bundled")
    try:
        assert page.input_value("#gender") == "Male"
        assert page.input_value("#hispanic") == ""
        assert page.evaluate("window.__applyRulesSrc()") == "bundled"
    finally:
        page.close()


def test_skips_optional_eeo_when_identity_has_no_value(browser, server, lib):
    bare = {k: v for k, v in IDENTITY.items()
            if k not in ("gender", "race", "veteran_status", "disability_status")}
    page = run_autofill(browser, server, lib, "eeo_present.html", identity=bare)
    try:
        assert page.input_value("#email") == "rahil@example.com"
        for field in ("#gender", "#race", "#veteran", "#disability",
                      "#hispanic", "#orientation"):
            assert page.input_value(field) == "", f"{field} was auto-filled"
    finally:
        page.close()


def test_falls_back_cleanly_on_a_malformed_rules_payload(browser, server, lib):
    page = run_autofill(browser, server, lib, "eeo_present.html", rules="malformed")
    try:
        assert page.input_value("#email") == "rahil@example.com"
        assert page.input_value("#gender") == "Male"
        assert page.input_value("#orientation") == ""
        assert page.evaluate("window.__applyRulesSrc()") == "bundled"
    finally:
        page.close()


def test_field_label_prefers_visible_text_over_name_and_id(browser, server, lib):
    """Anchored rules (gender/sex) die if name/id are stapled onto a real label."""
    page = run_autofill(browser, server, lib, "eeo_present.html")
    try:
        gender = page.evaluate("window.__applyFieldLabel(document.getElementById('gender'))")
        assert gender == "gender", gender
        email = page.evaluate("window.__applyFieldLabel(document.getElementById('email'))")
        assert email == "email", email
        # Unlabelled controls still match via name/id last-resort.
        page.evaluate("""() => {
            const el = document.createElement('input');
            el.name = 'email'; el.id = 'no-label';
            document.body.appendChild(el);
        }""")
        fallback = page.evaluate(
            "window.__applyFieldLabel(document.getElementById('no-label'))")
        assert fallback == "email no-label", fallback
    finally:
        page.close()
