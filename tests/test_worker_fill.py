"""The submit worker's form filling, driven against a *real* headless browser.

This is the piece the docs long called untestable: `worker/run.py` drives a live
browser, so none of it was covered. It is testable — just not against live ATS
sites. Here we serve hand-written fixtures that reproduce the shapes real forms
take (iframes, reveal-then-render, ARIA comboboxes, late SPA paints, EEO sections)
from a local HTTP server, and drive them with the same Chromium the worker uses.

No network, no credentials, no live site is touched. The module skips cleanly where
Playwright/Chromium isn't installed, so a browserless CI still passes.

The two invariants these tests exist to defend:
  1. `fill_form` NEVER submits — a human approves first.
  2. EEO / demographic fields are NEVER filled, on any control type.
"""
from __future__ import annotations

import contextlib
import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("playwright", reason="browser tests need the playwright package")

from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from worker import run as worker_run  # noqa: E402

FORMS_DIR = Path(__file__).parent / "fixtures" / "forms"

# A filled-in applicant. Values mirror what /worker/claim actually sends: the
# identity has already been flattened by applicant.autofill_map, so the Yes/No
# questions arrive as the strings "Yes"/"No".
IDENTITY = {
    "first_name": "Rahil", "last_name": "Sheth", "full_name": "Rahil Sheth",
    "email": "rahil@example.com", "phone": "555-0100",
    "location": "Chicago, IL", "city": "Chicago", "state": "IL",
    "country": "United States",
    "linkedin": "https://linkedin.com/in/rahil",
    "github": "https://github.com/rahil",
    "school": "State University", "current_company": "Acme Corp",
    "work_authorized": "Yes", "needs_sponsorship": "No",
}

QUESTIONS = [{
    "question": "Why do you want to work at Acme?",
    "answer": "Acme's platform work lines up with what I have been building: "
              "I spent the last two years on high-throughput backend services.",
}]

# A tiny but structurally valid PDF, so set_input_files gets real bytes.
RESUME = {"name": "rahil_sheth_resume.pdf", "mimeType": "application/pdf",
          "buffer": b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<<>>\n%%EOF\n"}


# --- local fixture server + browser ----------------------------------------

class _QuietHandler(SimpleHTTPRequestHandler):
    """Serves the fixture forms and accepts the POST a submit produces, so a real
    submission is observable as a page whose body says SUBMITTED."""

    def log_message(self, *_args):  # keep pytest output clean
        pass

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler's naming
        body = b"<html><body>SUBMITTED</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
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
    except PlaywrightError as e:  # browser binary not installed on this machine
        pytest.skip(f"chromium unavailable: {e}")


@contextlib.contextmanager
def filled(browser, server, name, identity=None, questions=None, resume=None):
    """Run the worker's real fill against a fixture; yields (page, preview) so tests
    can assert on both the reported preview and the resulting DOM."""
    page = browser.new_page()
    try:
        job = {"url": f"{server}/{name}",
               "identity": IDENTITY if identity is None else identity,
               "questions": QUESTIONS if questions is None else questions,
               "resume": resume}
        yield page, worker_run.fill_form(page, job)
    finally:
        page.close()


def labels(entries):
    return [e["label"] for e in entries]


def value_for(preview, needle):
    """The value the worker reported filling into the field whose label contains
    ``needle`` (case-insensitive)."""
    for e in preview["filled"]:
        if needle.lower() in e["label"].lower():
            return e["value"]
    return None


def was_submitted(page):
    return "SUBMITTED" in page.content()


# --- the fills --------------------------------------------------------------

def test_greenhouse_basic_fills_text_and_native_select(browser, server):
    with filled(browser, server, "greenhouse_basic.html") as (page, preview):
        assert page.input_value("#first_name") == "Rahil"
        assert page.input_value("#last_name") == "Sheth"
        assert page.input_value("#email") == "rahil@example.com"
        assert page.input_value("#phone") == "555-0100"
        assert page.input_value("#location") == "Chicago, IL"
        assert page.input_value("#linkedin") == "https://linkedin.com/in/rahil"
        # native <select> matched through the shared brain
        assert page.input_value("#country") == "United States"
        # a field we have no fact for is reported, not guessed at
        assert page.input_value("#favorite") == ""
        assert any("favorite" in s.lower() for s in preview["skipped"])
        assert preview["screenshot_url"].startswith("data:image/jpeg;base64,")


def test_resume_attaches_only_to_the_resume_field(browser, server):
    with filled(browser, server, "greenhouse_basic.html", resume=RESUME) as (page, preview):
        assert page.evaluate("document.getElementById('resume').files.length") == 1
        # the cover-letter upload must stay empty — wrong-slot uploads are worse
        # than no upload at all
        assert page.evaluate("document.getElementById('cover').files.length") == 0
        assert value_for(preview, "resume") == RESUME["name"]


def test_lever_reveal_opens_the_form_then_fills_it(browser, server):
    with filled(browser, server, "lever_apply_reveal.html") as (page, preview):
        assert page.input_value("#name") == "Rahil Sheth"
        assert page.input_value("#email") == "rahil@example.com"
        assert page.input_value("#org") == "Acme Corp"
        assert page.input_value("#urls-github") == "https://github.com/rahil"
        # the essay question is answered from the drafted answers
        assert "Acme's platform work" in page.input_value("#comments")
        assert not was_submitted(page)


def test_form_inside_an_iframe_is_found_and_filled(browser, server):
    with filled(browser, server, "ashby_iframe.html") as (page, preview):
        inner = page.frame_locator("#ashby_embed")
        assert inner.locator("#_systemfield_name").input_value() == "Rahil Sheth"
        assert inner.locator("#_systemfield_email").input_value() == "rahil@example.com"
        assert inner.locator("#school").input_value() == "State University"
        assert preview["filled"], "top frame has no fields; the iframe must be used"


def test_custom_combobox_radio_group_and_essay(browser, server):
    with filled(browser, server, "custom_dropdown.html") as (page, preview):
        # ARIA combobox (a div, not a <select>) — click-open then click-option
        assert page.get_attribute("#auth-combo", "data-selected") == "Yes"
        assert page.get_attribute("#country-combo", "data-selected") == "United States"
        # Yes/No radio group answered as a group decision
        assert page.is_checked("input[name='sponsorship'][value='No']")
        assert not page.is_checked("input[name='sponsorship'][value='Yes']")
        assert "Acme's platform work" in page.input_value("#why")
        assert not was_submitted(page)


def test_late_rendering_spa_form_is_waited_out(browser, server):
    """The form paints 1.2s after load with nothing to click meanwhile — a one-shot
    extraction reads zero fields and reports an empty fill."""
    with filled(browser, server, "spa_late_form.html") as (page, preview):
        assert page.input_value("#email") == "rahil@example.com"
        assert page.input_value("#first_name") == "Rahil"
        assert len(preview["filled"]) >= 3


# --- the invariants ---------------------------------------------------------

@pytest.mark.parametrize("fixture", [
    "greenhouse_basic.html", "lever_apply_reveal.html", "ashby_iframe.html",
    "custom_dropdown.html", "eeo_present.html", "spa_late_form.html",
])
def test_fill_form_never_submits(browser, server, fixture):
    """The human-approval gate is the product's core safety promise: filling a form
    must never, on any fixture, cause a submission."""
    with filled(browser, server, fixture, resume=RESUME) as (page, _preview):
        assert not was_submitted(page)


def test_eeo_fields_are_never_filled(browser, server):
    with filled(browser, server, "eeo_present.html") as (page, preview):
        # identity fields above the EEO section still fill normally
        assert page.input_value("#first_name") == "Rahil"
        assert page.input_value("#email") == "rahil@example.com"
        # ...and every demographic control is untouched, whatever its type
        assert page.input_value("#gender") == ""
        assert page.input_value("#race") == ""
        assert page.input_value("#veteran") == ""
        assert page.input_value("#hispanic") == ""
        assert page.input_value("#disability") == ""
        assert page.input_value("#orientation") == ""
        assert not page.is_checked("input[name='gender_identity'][value='Yes']")
        assert not page.is_checked("input[name='gender_identity'][value='No']")
        # and they're surfaced to the human rather than silently dropped
        skipped = " ".join(preview["skipped"]).lower()
        for term in ("gender", "race", "veteran", "disability"):
            assert term in skipped


def test_a_long_eeo_question_is_not_answered_as_an_essay(browser, server):
    """"Are you Hispanic or Latino?" is long and question-shaped, so the essay rule
    would happily write a drafted answer into it. It must not."""
    with filled(browser, server, "eeo_present.html") as (page, _preview):
        assert page.input_value("#hispanic") == ""


# --- submit-button detection ------------------------------------------------

def test_submit_form_clicks_the_real_submit(browser, server):
    page = browser.new_page()
    try:
        page.goto(f"{server}/greenhouse_basic.html")
        worker_run.submit_form(page)
        assert was_submitted(page)
    finally:
        page.close()


def test_submit_form_finds_the_button_inside_an_iframe(browser, server):
    """An embedded form keeps its submit button in the iframe; a top-frame-only
    search silently found nothing and the approved application never went out."""
    page = browser.new_page()
    try:
        page.goto(f"{server}/ashby_iframe.html")
        worker_run.submit_form(page)
        assert "SUBMITTED" in page.frame_locator("#ashby_embed").locator("body").inner_text()
    finally:
        page.close()


def test_submit_form_never_clicks_an_apply_reveal_link(browser, server):
    """On an unrevealed description page the only 'Apply' control is the reveal
    link. Clicking it would look like success while submitting nothing."""
    page = browser.new_page()
    try:
        page.goto(f"{server}/lever_apply_reveal.html")
        with pytest.raises(RuntimeError, match="no submit button"):
            worker_run.submit_form(page)
        assert not was_submitted(page)
        assert page.is_hidden("#application")   # the reveal never fired
    finally:
        page.close()
