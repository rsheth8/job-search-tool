"""The iOS app's autofill engine, run against real forms in a real browser.

`ios/Apply/Autofill.swift` carries its filler as a JavaScript string injected into
WKWebView. That's ordinary JavaScript, so we can extract it and run it against the
same fixtures the submit worker uses — which means the iPhone's autofill gets real
browser coverage without Xcode, a simulator, or a device in the loop.

This exists because the mobile engine was the least-tested and most-drifted copy of
the field-matching brain: it shipped a narrower EEO list than the backend, so the
phone would fill demographic questions the worker refuses. These tests pin the
behaviour that matters — it fills what it should, and **never fills EEO fields** —
against the rules the backend actually serves.

Skips cleanly where Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import functools
import json
import re
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("playwright", reason="browser tests need the playwright package")

from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from app import fieldmatch  # noqa: E402

FORMS_DIR = Path(__file__).parent / "fixtures" / "forms"
AUTOFILL_SWIFT = Path(__file__).resolve().parents[1] / "ios" / "Apply" / "Autofill.swift"

IDENTITY = {
    "first_name": "Rahil", "last_name": "Sheth", "full_name": "Rahil Sheth",
    "email": "rahil@example.com", "phone": "555-0100",
    "location": "Chicago, IL", "country": "United States",
    "linkedin": "https://linkedin.com/in/rahil", "github": "https://github.com/rahil",
    "school": "State University", "current_company": "Acme Corp",
    "work_authorized": "Yes", "needs_sponsorship": "No",
    # values for the fields the *old* bundled rules would have filled — present so a
    # regression would actually be visible rather than silently absent
    "gender": "Male", "race": "Asian",
}

ANSWERS = [{"question": "Why do you want to work at Acme?",
            "answer": "Acme's platform work lines up with what I've been building."}]


def _extract_lib() -> str:
    "The JS inside Swift's raw-string delimiters in Autofill.swift."
    source = AUTOFILL_SWIFT.read_text()
    m = re.search(r'static let lib = #"""\n(.*?)\n\s*"""#', source, re.S)
    assert m, "could not find the `lib` raw string in Autofill.swift"
    return m.group(1)


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
        pytest.skip(f"chromium unavailable: {e}")


@pytest.fixture(scope="module")
def lib():
    return _extract_lib()


def run_autofill(browser, server, lib, fixture, *, serve_rules=True):
    """Load a fixture, inject the profile exactly as WebView.swift does, run the
    engine, and hand back the page plus its reported result."""
    page = browser.new_page()
    payload = {
        "identity": IDENTITY,
        "answers": ANSWERS,
        "rules": fieldmatch.rules_payload() if serve_rules else None,
    }
    # Mirrors Autofill.dataScript: the profile lands before any page script runs.
    page.add_init_script(f"window.__APPLY = {json.dumps(payload)};")
    page.goto(f"{server}/{fixture}")
    page.wait_for_timeout(1600)          # let the SPA/reveal fixtures settle
    page.evaluate(lib)                   # defines window.__applyAutofill
    page.evaluate("window.__applyAutofill()")
    return page


# --- it fills what it should ------------------------------------------------

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
        assert page.input_value("#favorite") == ""      # no fact for it — left alone
    finally:
        page.close()


def test_fills_the_essay_from_the_drafted_answers(browser, server, lib):
    page = run_autofill(browser, server, lib, "custom_dropdown.html")
    try:
        assert "Acme's platform work" in page.input_value("#why")
    finally:
        page.close()


def test_answers_the_yes_no_radio_group(browser, server, lib):
    page = run_autofill(browser, server, lib, "custom_dropdown.html")
    try:
        assert page.is_checked("input[name='sponsorship'][value='No']")
        assert not page.is_checked("input[name='sponsorship'][value='Yes']")
    finally:
        page.close()


def test_never_submits_the_form(browser, server, lib):
    """Autofill fills; the human submits. Same promise as the worker."""
    for fixture in ("greenhouse_basic.html", "custom_dropdown.html", "eeo_present.html"):
        page = run_autofill(browser, server, lib, fixture)
        try:
            assert "SUBMITTED" not in page.content()
        finally:
            page.close()


# --- the invariant that was actually broken ---------------------------------

def test_never_fills_eeo_fields_with_served_rules(browser, server, lib):
    page = run_autofill(browser, server, lib, "eeo_present.html")
    try:
        assert page.input_value("#first_name") == "Rahil"     # ordinary fields still fill
        assert page.input_value("#email") == "rahil@example.com"
        for field in ("#gender", "#race", "#veteran", "#hispanic",
                      "#disability", "#orientation"):
            assert page.input_value(field) == "", f"{field} was auto-filled"
        assert not page.is_checked("input[name='gender_identity'][value='Yes']")
        assert not page.is_checked("input[name='gender_identity'][value='No']")
    finally:
        page.close()


def test_never_fills_eeo_fields_on_the_bundled_fallback_either(browser, server, lib):
    """Offline, the engine falls back to the copy baked into Autofill.swift. That
    copy is generated from fieldmatch.py — if someone regenerates it carelessly, or
    hand-edits it, this catches the EEO regression the served path would hide."""
    page = run_autofill(browser, server, lib, "eeo_present.html", serve_rules=False)
    try:
        assert page.input_value("#first_name") == "Rahil"     # fallback still works
        for field in ("#gender", "#race", "#veteran", "#hispanic",
                      "#disability", "#orientation"):
            assert page.input_value(field) == "", f"{field} filled by bundled rules"
    finally:
        page.close()


def test_reports_which_rule_set_ran(browser, server, lib):
    """The fill result carries the rules version, so a phone quietly running stale
    bundled rules is visible instead of invisible."""
    page = browser.new_page()
    try:
        payload = {"identity": IDENTITY, "answers": ANSWERS,
                   "rules": fieldmatch.rules_payload()}
        page.add_init_script(f"window.__APPLY = {json.dumps(payload)};")
        page.goto(f"{server}/greenhouse_basic.html")
        page.evaluate(lib)
        # Capture what the engine posts back to the native side.
        page.evaluate("""() => {
            window.__sent = null;
            window.webkit = { messageHandlers: { applyfill: {
                postMessage: (m) => { window.__sent = m; } } } };
        }""")
        page.evaluate("window.__applyAutofill()")
        sent = page.evaluate("window.__sent")
        assert sent["filled"] > 0
        assert sent["rules"] == fieldmatch.rules_payload()["version"]
    finally:
        page.close()


def test_falls_back_cleanly_on_a_malformed_rules_payload(browser, server, lib):
    """A bad payload must leave the page fillable *and* safe, not broken."""
    page = browser.new_page()
    try:
        payload = {"identity": IDENTITY, "answers": ANSWERS,
                   "rules": {"rules": [["email", "([unclosed"]], "never_fill": "gender"}}
        page.add_init_script(f"window.__APPLY = {json.dumps(payload)};")
        page.goto(f"{server}/eeo_present.html")
        page.evaluate(lib)
        page.evaluate("window.__applyAutofill()")
        assert page.input_value("#email") == "rahil@example.com"   # bundled rules ran
        assert page.input_value("#gender") == ""                   # and stayed safe
    finally:
        page.close()


# --- the bundled copy must not drift again ----------------------------------

def test_the_bundled_fallback_matches_the_python_rules():
    """The comment in Autofill.swift claims the fallback is generated from
    fieldmatch.py. Verify it, so the drift that motivated all of this can't return
    silently — the *served* path would mask it in normal use."""
    lib_js = _extract_lib()
    payload = fieldmatch.rules_payload()

    missing = [key for key, _ in payload["rules"] if f'["{key}", /' not in lib_js]
    assert not missing, f"bundled fallback is missing rules: {missing}"

    m = re.search(r"const FALLBACK_EEO = /(.*)/i;", lib_js)
    assert m, "no FALLBACK_EEO in Autofill.swift"
    assert m.group(1) == payload["never_fill"], (
        "bundled EEO list has drifted from app/fieldmatch.py — regenerate it")
