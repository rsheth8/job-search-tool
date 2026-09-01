"""The iOS app's autofill engine, run against real forms in a real browser.

`ios/JobPilot/Autofill.swift` carries its filler as a JavaScript string injected into
WKWebView. That's ordinary JavaScript, so we can extract it and run it against the
same fixtures the submit worker uses — which means the iPhone's autofill gets real
browser coverage without Xcode, a simulator, or a device in the loop.

This exists because the mobile engine was the least-tested and most-drifted copy of
the field-matching brain: it shipped a narrower EEO list than the backend, so the
phone would fill demographic questions the worker refuses. These tests pin the
behaviour that matters — it fills what it should, fills optional demographics
only when identity has them, and **never fills hard-blocked EEO fields** —
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

from tests.browserutil import skip_unless_ci_chromium  # noqa: E402
from app import fieldmatch  # noqa: E402

FORMS_DIR = Path(__file__).parent / "fixtures" / "forms"
AUTOFILL_SWIFT = Path(__file__).resolve().parents[1] / "ios" / "JobPilot" / "Autofill.swift"

IDENTITY = {
    "first_name": "Rahil", "last_name": "Sheth", "full_name": "Rahil Sheth",
    "email": "rahil@example.com", "phone": "555-0100",
    "location": "Chicago, IL", "country": "United States",
    "linkedin": "https://linkedin.com/in/rahil", "github": "https://github.com/rahil",
    "school": "State University", "current_company": "Acme Corp",
    "work_authorized": "Yes", "needs_sponsorship": "No",
    # values for optional EEO — filled when set; hard-blocked fields stay empty
    "gender": "Male", "race": "Asian",
    "veteran_status": "I am not a protected veteran",
    "disability_status": "No, I do not have a disability",
}

ANSWERS = [{"question": "Why do you want to work at Acme?",
            "answer": "Acme's platform work lines up with what I've been building."}]


def _extract_lib() -> str:
    "The JS inside Swift's raw-string delimiters in Autofill.swift."
    source = AUTOFILL_SWIFT.read_text()
    m = re.search(r'static let lib = #"""\n(.*?)\n\s*"""#', source, re.S)
    assert m, "could not find the `lib` raw string in Autofill.swift"
    return m.group(1)


def test_lib_notes_file_and_date_skips():
    """iOS cannot set <input type=file> or date pickers. Those must be skips
    the Still-you banner can turn into Attach résumé / pick a date."""
    lib = _extract_lib()
    assert 'noteSkip(label || "Resume", "file")' in lib
    assert 'noteSkip(label || "Start date", "date")' in lib


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
    return _extract_lib()


def run_autofill(browser, server, lib, fixture, *, serve_rules=True, identity=None):
    """Load a fixture, inject the profile exactly as WebView.swift does, run the
    engine, and hand back the page plus its reported result."""
    page = browser.new_page()
    payload = {
        "identity": identity or IDENTITY,
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


def test_clicks_the_matching_combobox_option(browser, server, lib):
    """A div role=combobox only commits when an option is clicked, not typed."""
    page = run_autofill(browser, server, lib, "custom_dropdown.html")
    try:
        assert page.get_attribute("#country-combo", "data-selected") == "United States"
        assert page.get_attribute("#auth-combo", "data-selected") == "Yes"
    finally:
        page.close()


def test_selects_the_closest_typeahead_option(browser, server, lib):
    """School/location typeaheads and a degree <select>: identity text is not an
    allowed value, so we search and click the nearest option. The hidden committed
    fields (what the form actually saves) must match — not the typed identity."""
    ident = {
        **IDENTITY,
        "school": "University of Minnesota Twin Cities",
        "location": "Chicago, IL",
        "degree": "B.S. Computer Science (May 2026); M.S. Data Science (in progress)",
    }
    page = run_autofill(browser, server, lib, "typeahead_select.html", identity=ident)
    try:
        assert page.input_value("#school_id") == "University of Minnesota-Twin Cities"
        assert page.input_value("#location_id") == "Chicago, Illinois, United States"
        assert page.input_value("#degree") == "Master's Degree"
        assert page.input_value("#school") == "University of Minnesota-Twin Cities"
        assert page.input_value("#location") == "Chicago, Illinois, United States"
    finally:
        page.close()


def test_js_pick_best_matches_python_select_value(browser, lib):
    """The engine's closest-option picker is the JS port of fieldmatch.select_value."""
    page = browser.new_page()
    try:
        page.evaluate(lib)
        cases = [
            (["United States", "Canada"], "USA"),
            (["University of Minnesota Crookston", "University of Minnesota-Twin Cities"],
             "University of Minnesota Twin Cities"),
            (["Chicago, Illinois, United States", "Vernon Hills, Illinois, United States"],
             "Chicago, IL"),
            (["High School", "Bachelor's Degree", "Master's Degree"], "B.S."),
            (["High School", "Bachelor's Degree", "Master's Degree"],
             "B.S. Computer Science; M.S. Data Science (in progress)"),
            (["Yes", "No"], "no"),
            (["Authorized to work", "Not authorized"], "Authorized"),
            (["Select…", "United States"], "USA"),
        ]
        for options, value in cases:
            js = page.evaluate("([o, v]) => window.__applyPickBest(o, v)", [options, value])
            py = fieldmatch.select_value(options, value)
            assert js == py, (options, value, js, py)
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


def test_clicks_ashby_style_yes_no_buttons(browser, server, lib):
    """Common Room / Ashby render work-auth as two big Yes/No buttons, not radios.
    US auth follows identity; Canada must NOT inherit a US Yes; sponsorship = No."""
    page = run_autofill(browser, server, lib, "ashby_yesno_buttons.html")
    try:
        us = page.locator('[data-field="us_auth"]')
        ca = page.locator('[data-field="ca_auth"]')
        sp = page.locator('[data-field="sponsor"]')
        assert us.locator('button[aria-pressed="true"]').inner_text() == "Yes"
        assert ca.locator('button[aria-pressed="true"]').inner_text() == "No"
        assert sp.locator('button[aria-pressed="true"]').inner_text() == "No"
        assert page.input_value("#linkedin") == "https://linkedin.com/in/rahil"
    finally:
        page.close()


def test_never_submits_the_form(browser, server, lib):
    """Autofill fills; the human submits. Same promise as the worker."""
    for fixture in ("greenhouse_basic.html", "custom_dropdown.html", "eeo_present.html",
                    "ashby_yesno_buttons.html", "typeahead_select.html"):
        page = run_autofill(browser, server, lib, fixture)
        try:
            assert "SUBMITTED" not in page.content()
        finally:
            page.close()


# --- the invariant that was actually broken ---------------------------------

def test_fills_optional_eeo_when_identity_has_values(browser, server, lib):
    page = run_autofill(browser, server, lib, "eeo_present.html")
    try:
        assert page.input_value("#first_name") == "Rahil"
        assert page.input_value("#email") == "rahil@example.com"
        assert page.input_value("#gender") == "Male"
        assert page.input_value("#race") == "Asian"
        assert page.input_value("#veteran") == "I am not a protected veteran"
        assert page.input_value("#disability") == "No, I do not have a disability"
        # hard-blocked stay empty
        assert page.input_value("#hispanic") == ""
        assert page.input_value("#orientation") == ""
        assert not page.is_checked("input[name='gender_identity'][value='Yes']")
        assert not page.is_checked("input[name='gender_identity'][value='No']")
    finally:
        page.close()


def test_fills_hispanic_latino_when_identity_has_a_value(browser, server, lib):
    ident = {**IDENTITY, "hispanic_latino": "No"}
    page = run_autofill(browser, server, lib, "eeo_present.html", identity=ident)
    try:
        assert page.input_value("#hispanic") == "No"
        assert page.input_value("#orientation") == ""
    finally:
        page.close()


def test_optional_eeo_fills_on_bundled_fallback_too(browser, server, lib):
    """Offline fallback must match served rules for opt-in demographics."""
    page = run_autofill(browser, server, lib, "eeo_present.html", serve_rules=False)
    try:
        assert page.input_value("#first_name") == "Rahil"
        assert page.input_value("#gender") == "Male"
        assert page.input_value("#race") == "Asian"
        assert page.input_value("#hispanic") == ""
        assert page.input_value("#orientation") == ""
    finally:
        page.close()


def test_skips_optional_eeo_when_identity_has_no_value(browser, server, lib):
    page = browser.new_page()
    try:
        bare = {k: v for k, v in IDENTITY.items()
                if k not in ("gender", "race", "veteran_status", "disability_status")}
        payload = {"identity": bare, "answers": ANSWERS,
                   "rules": fieldmatch.rules_payload()}
        page.add_init_script(f"window.__APPLY = {json.dumps(payload)};")
        page.goto(f"{server}/eeo_present.html")
        page.evaluate(lib)
        page.evaluate("window.__applyAutofill()")
        assert page.input_value("#email") == "rahil@example.com"
        for field in ("#gender", "#race", "#veteran", "#disability",
                      "#hispanic", "#orientation"):
            assert page.input_value(field) == "", f"{field} was auto-filled"
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
        skips = {s["label"]: s["reason"] for s in sent.get("skips") or []}
        assert skips.get("favorite color") == "unmatched"
        file_skips = [s for s in sent.get("skips") or [] if s.get("reason") == "file"]
        assert any("resume" in s["label"].lower() for s in file_skips)
        assert any("cover" in s["label"].lower() for s in file_skips)
    finally:
        page.close()


def test_date_inputs_are_reported_as_still_you(browser, lib):
    """Native date pickers cannot be filled from JS; they must show up as remaining work."""
    page = browser.new_page()
    try:
        payload = {"identity": IDENTITY, "answers": ANSWERS,
                   "rules": fieldmatch.rules_payload()}
        page.set_content("""<!doctype html>
            <label for="start">Available start date</label>
            <input type="date" id="start" name="start_date">
            <label for="email">Email</label>
            <input type="email" id="email" name="email">
        """)
        # Injected after set_content, not via add_init_script: an init script
        # does not run for a document installed this way, and __APPLY came out
        # undefined. The fill then reported "bundled" rules and skipped email
        # as empty — the test would have passed its date assertion while
        # exercising none of the profile it thought it had loaded.
        page.evaluate("(p) => { window.__APPLY = p; }", payload)
        page.evaluate(lib)
        page.evaluate("""() => {
            window.__sent = null;
            window.webkit = { messageHandlers: { applyfill: {
                postMessage: (m) => { window.__sent = m; } } } };
        }""")
        page.evaluate("window.__applyAutofill()")
        sent = page.evaluate("window.__sent")
        skips = {s["label"].lower(): s["reason"] for s in sent.get("skips") or []}
        assert any("start" in k and v == "date" for k, v in skips.items()), skips
        assert page.input_value("#email") == "rahil@example.com"
    finally:
        page.close()


def test_pick_best_matches_python_select_value(browser, lib):
    """JS pickBest must stay on the same option as fieldmatch.select_value."""
    page = browser.new_page()
    try:
        page.evaluate(lib)
        cases = [
            (["Male", "Female", "Non-binary"], "Man", "Male"),
            (["Hispanic or Latino", "Not Hispanic or Latino"], "No",
             "Not Hispanic or Latino"),
            (["I am authorized to work in the US",
              "I am not authorized to work in the US"], "Yes",
             "I am authorized to work in the US"),
            (["$80,000 - $100,000", "$100,000 - $130,000"], "120000",
             "$100,000 - $130,000"),
            (["0-2", "3+", "5+"], "5", "5+"),
            (["Fully remote", "Hybrid", "On-site"], "Remote", "Fully remote"),
            (["United States+1", "Canada+1"], "United States", "United States+1"),
            (["3.7 - 4.0", "3.1 - 3.6", "3.0 or under"], "3.5", "3.1 - 3.6"),
        ]
        for options, value, expected in cases:
            py = fieldmatch.select_value(options, value)
            js = page.evaluate("([o, v]) => window.__applyPickBest(o, v)",
                               [options, value])
            assert py == expected, (options, value, py)
            assert js == py, (options, value, js, py)
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
        # malformed never_fill was "gender", but fallback uses real rules — optional
        # gender fills when identity has it; hard-blocked stay empty
        assert page.input_value("#gender") == "Male"
        assert page.input_value("#orientation") == ""
    finally:
        page.close()


# --- the bundled copy must not drift again ----------------------------------

def test_field_label_prefers_visible_text_over_name_and_id(browser, server, lib):
    """Same contract as the extension: a <label for> of "Gender" must stay "gender",
    not "gender gender gender" from name/id — or the anchored rule never fires."""
    page = run_autofill(browser, server, lib, "eeo_present.html")
    try:
        gender = page.evaluate("window.__applyFieldLabel(document.getElementById('gender'))")
        assert gender == "gender", gender
        email = page.evaluate("window.__applyFieldLabel(document.getElementById('email'))")
        assert email == "email", email
    finally:
        page.close()


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


def _load_engine(browser, server, lib, fixture, *, wait_until="domcontentloaded"):
    """Inject the iOS engine without running Fill — for probe / hop / pause tests."""
    page = browser.new_page()
    payload = {
        "identity": IDENTITY,
        "answers": ANSWERS,
        "rules": fieldmatch.rules_payload(),
    }
    page.add_init_script(f"window.__APPLY = {json.dumps(payload)};")
    page.add_init_script("""
        window.__sent = [];
        window.webkit = { messageHandlers: { applyfill: {
            postMessage: (m) => { window.__sent.push(m); } } } };
    """)
    page.goto(f"{server}/{fixture}", wait_until=wait_until)
    page.evaluate(lib)
    return page


def test_fill_or_pause_fills_generic_html(browser, server, lib):
    """Fill is not gated on Greenhouse — a plain company form is enough."""
    page = _load_engine(browser, server, lib, "custom_html_apply.html")
    try:
        probe = page.evaluate("window.__applyFormProbe()")
        assert probe["kind"] == "application"
        filled = page.evaluate("window.__applyFillOrPause()")
        assert filled > 0
        assert page.input_value("#email") == "rahil@example.com"
        assert page.input_value("#first_name") == "Rahil"
        assert "SUBMITTED" not in page.content()
    finally:
        page.close()


def test_fill_or_pause_does_not_type_into_a_login_wall(browser, server, lib):
    page = _load_engine(browser, server, lib, "login_wall.html")
    try:
        probe = page.evaluate("window.__applyFormProbe()")
        assert probe["kind"] == "login"
        filled = page.evaluate("window.__applyFillOrPause()")
        assert filled == 0
        assert page.input_value("#email") == ""
        sent = page.evaluate("window.__sent")
        assert sent and sent[-1]["status"] == "needsHuman"
        assert sent[-1]["probe"]["kind"] == "login"
    finally:
        page.close()


def test_fill_or_pause_does_not_type_through_a_captcha(browser, server, lib):
    page = _load_engine(browser, server, lib, "captcha_wall.html")
    try:
        probe = page.evaluate("window.__applyFormProbe()")
        assert probe["kind"] == "captcha"
        filled = page.evaluate("window.__applyFillOrPause()")
        assert filled == 0
        assert page.input_value("#email") == ""
        sent = page.evaluate("window.__sent")
        assert sent and sent[-1]["status"] == "needsHuman"
        assert sent[-1]["probe"]["kind"] == "captcha"
    finally:
        page.close()


def test_find_apply_embed_hops_to_workable_not_captcha(browser, server, lib):
    page = browser.new_page()
    try:
        page.route("**/*workable.com/**", lambda route: route.abort())
        payload = {
            "identity": IDENTITY,
            "answers": ANSWERS,
            "rules": fieldmatch.rules_payload(),
        }
        page.add_init_script(f"window.__APPLY = {json.dumps(payload)};")
        page.goto(f"{server}/apply_embed.html", wait_until="domcontentloaded")
        page.evaluate(lib)
        url = page.evaluate("window.__applyFindApplyEmbed()")
        assert url and "apply.workable.com" in url
        assert "recaptcha" not in url
    finally:
        page.close()


def test_watch_mode_clears_when_login_wall_drops(browser, server, lib):
    page = _load_engine(browser, server, lib, "login_wall.html")
    try:
        page.evaluate("() => { void window.__applyDrive({ mode: 'watch' }); }")
        page.wait_for_function(
            "() => (window.__sent || []).some(m => m.status === 'needsHuman')",
            timeout=3000,
        )
        page.evaluate("""() => {
          document.body.innerHTML = `
            <form>
              <label for="email">Email</label>
              <input type="email" id="email">
              <label for="first_name">First name</label>
              <input type="text" id="first_name">
              <label for="last_name">Last name</label>
              <input type="text" id="last_name">
              <button type="submit">Submit application</button>
            </form>`;
        }""")
        page.wait_for_function(
            "() => (window.__sent || []).some(m => m.status === 'watchingClear')",
            timeout=4000,
        )
        kinds = [m.get("probe", {}).get("kind") for m in page.evaluate("window.__sent")
                 if m.get("status") == "watchingClear"]
        assert kinds and kinds[-1] == "application"
    finally:
        page.close()


# --- one fill, one report, one honest number --------------------------------
#
# The engine is injected with `forMainFrameOnly: false`, so it runs in every
# frame on the page, and every frame posts into the SAME native handler. These
# pin the three ways that used to go wrong.

def _run_tree(browser, server, lib, fixture, *, wait=2500):
    """Load a fixture, inject the engine into every frame, and funnel all frames'
    native posts into one array — exactly how WKWebView's shared `applyfill`
    handler sees them."""
    page = browser.new_page()
    payload = {"identity": IDENTITY, "answers": ANSWERS,
               "rules": fieldmatch.rules_payload()}
    page.add_init_script(f"window.__APPLY = {json.dumps(payload)};")
    page.add_init_script("""
      try { if (!window.top.__sent) window.top.__sent = []; } catch (e) {}
      window.webkit = { messageHandlers: { applyfill: {
        postMessage: (m) => { try { window.top.__sent.push(m); } catch (e) {} } } } };
    """)
    page.goto(f"{server}/{fixture}")
    page.wait_for_timeout(700)
    for frame in page.frames:
        try:
            frame.evaluate(lib)
        except PlaywrightError:
            pass                      # a frame that won't take script can't fill
    page.evaluate("window.__applyAutofillAll()")
    page.wait_for_timeout(wait)
    return page, page.evaluate("window.__sent") or []


def test_a_noise_frame_cannot_overwrite_the_real_fill_report(browser, server, lib):
    """about:blank and a widget frame fill nothing. Their zero must not land on
    top of the real result — that turned a filled form into "No fields matched"
    and, worse, blanked the skips that grow the phrasing table."""
    page, sent = _run_tree(browser, server, lib, "noise_frames.html")
    try:
        assert page.input_value("#first_name") == "Rahil"      # it really filled
        assert len(sent) == 1, f"expected exactly one report, got {sent}"
        assert sent[0]["filled"] == 4
        # the skip survives, so filllearn still sees the unmatched label
        labels = {s["label"] for s in sent[0].get("skips") or []}
        assert "favorite color" in labels
    finally:
        page.close()


def test_the_report_counts_fields_filled_inside_an_embedded_form(browser, server, lib):
    """The whole point of the iframe hop: a careers page whose top frame has no
    fields at all must still report what the embed filled."""
    page, sent = _run_tree(browser, server, lib, "ashby_iframe.html")
    try:
        assert len(sent) == 1, f"expected exactly one report, got {sent}"
        assert sent[0]["filled"] > 0, "embedded form filled, but the app was told 0"
        assert sent[0]["url"].endswith("ashby_iframe.html")   # the page, not a frame
    finally:
        page.close()


def test_a_second_fill_never_overwrites_an_answer_already_there(browser, server, lib):
    """Autofill fills blanks. A value the person typed — or one an earlier pass
    committed — survives tapping the button again."""
    page = run_autofill(browser, server, lib, "greenhouse_basic.html")
    try:
        assert page.input_value("#phone") == "555-0100"
        page.fill("#phone", "+1 (312) 555-9999")       # the human corrects it
        page.evaluate("window.__applyAutofill()")
        page.wait_for_timeout(300)
        assert page.input_value("#phone") == "+1 (312) 555-9999"
    finally:
        page.close()


def test_a_choice_field_is_left_empty_rather_than_filled_with_text_that_wont_save(
        browser, server, lib):
    """The quiet one. A declared combobox stores only what was *clicked*, so
    typing into it paints text the person reads back as "done" while the field
    the ATS submits stays empty. Better an empty box and a skip they can see."""
    page = browser.new_page()
    try:
        payload = {"identity": IDENTITY, "answers": ANSWERS,
                   "rules": fieldmatch.rules_payload()}
        page.add_init_script(f"window.__APPLY = {json.dumps(payload)};")
        page.goto(f"{server}/typeahead_select.html")
        page.wait_for_timeout(600)
        page.evaluate(lib)
        page.evaluate("""() => {
            window.__sent = [];
            window.webkit = { messageHandlers: { applyfill: {
                postMessage: (m) => window.__sent.push(m) } } };
        }""")
        page.evaluate("window.__applyAutofill()")
        page.wait_for_timeout(1600)
        # IDENTITY's school is not on this form's list.
        assert page.input_value("#school_id") == "", "fixture assumption changed"
        assert page.input_value("#school") == "", (
            "typed text left in a combobox that never committed — the form looks "
            "filled and submits empty")
        # and the one that IS on the list still commits, both display and value
        assert page.input_value("#location_id") == "Chicago, Illinois, United States"
        reasons = {s["key"]: s["reason"] for s in page.evaluate("window.__sent")[0]["skips"]
                   if s.get("key")}
        assert reasons.get("school") == "no_option"
    finally:
        page.close()


def test_autopilot_fills_both_steps_counts_once_and_stops_before_submit(
        browser, server, lib):
    """`__applyDrive({mode:'run'})` is what the ⚡ button actually calls, and it
    had no coverage. It must fill step 1, click Next, fill step 2, report the
    total once — and stop at the Submit button, never through it."""
    page = browser.new_page()
    try:
        payload = {"identity": IDENTITY, "answers": ANSWERS,
                   "rules": fieldmatch.rules_payload()}
        page.add_init_script(f"window.__APPLY = {json.dumps(payload)};")
        page.goto(f"{server}/two_step_next.html")
        page.wait_for_timeout(300)
        page.evaluate(lib)
        page.evaluate("""() => {
            window.__sent = [];
            window.webkit = { messageHandlers: { applyfill: {
                postMessage: (m) => window.__sent.push(m) } } };
        }""")
        page.evaluate("() => { void window.__applyDrive({ mode: 'run' }); }")
        page.wait_for_function(
            "() => (window.__sent || []).some(m => m.status === 'ready')",
            timeout=12000)
        assert page.input_value("#email") == "rahil@example.com"     # step 1
        assert page.input_value("#phone") == "555-0100"              # step 2
        assert page.input_value("#linkedin") == "https://linkedin.com/in/rahil"
        sent = page.evaluate("window.__sent")
        ready = [m for m in sent if m["status"] == "ready"][-1]
        assert ready["filled"] == 5, f"total should count each field once: {sent}"
        assert [m["status"] for m in sent].count("advancing") == 1
        assert page.get_attribute("body", "data-submitted") is None
    finally:
        page.close()


# --- pages that look blocked but aren't -------------------------------------
#
# Each of these was found by running the shipping engine against a live posting,
# not against a fixture. The fixtures below reproduce the DOM those pages have.

def test_an_invisible_recaptcha_is_not_a_wall(browser, server, lib):
    """Greenhouse puts a score-based reCAPTCHA on every posting it serves. It
    asks the person for nothing, so calling it a blocker meant the app refused to
    fill the most common application form there is."""
    page = _load_engine(browser, server, lib, "invisible_captcha_form.html")
    try:
        probe = page.evaluate("window.__applyFormProbe()")
        assert probe["kind"] == "application", probe
        assert probe["captcha"] is False
        filled = page.evaluate("window.__applyFillOrPause()")
        assert filled >= 4
        assert page.input_value("#email") == "rahil@example.com"
    finally:
        page.close()


def test_a_visible_checkbox_captcha_still_stops_the_fill(browser, server, lib):
    """The other half of the same rule: a challenge someone has to tick is still
    a blocker, and the engine still must not type behind it."""
    page = _load_engine(browser, server, lib, "captcha_wall.html")
    try:
        assert page.evaluate("window.__applyFormProbe()")["kind"] == "captcha"
        assert page.evaluate("window.__applyFillOrPause()") == 0
        assert page.input_value("#email") == ""
    finally:
        page.close()


def test_a_sign_in_link_in_the_site_nav_is_not_a_login_wall(browser, server, lib):
    """Nearly every careers page has "Sign in" in its chrome. Counting it made
    the app tell people to log in by hand with an Apply button right there."""
    page = _load_engine(browser, server, lib, "nav_login_link.html")
    try:
        probe = page.evaluate("window.__applyFormProbe()")
        assert probe["kind"] != "login", probe
        assert probe["revealLabel"] == "Apply now"
    finally:
        page.close()


def test_autopilot_fills_an_embedded_form_instead_of_clicking_apply_forever(
        browser, server, lib):
    """The top frame has no fields, so the loop used to click "Apply now" to the
    step limit and report ready over an untouched form. It must fill the embed,
    report what the embed filled, and not spin on reveal."""
    page = browser.new_page()
    try:
        payload = {"identity": IDENTITY, "answers": ANSWERS,
                   "rules": fieldmatch.rules_payload()}
        page.add_init_script(f"window.__APPLY = {json.dumps(payload)};")
        page.add_init_script(lib)          # as WKUserScript does, in every frame
        page.add_init_script("""
          try { if (!window.top.__sent) window.top.__sent = []; } catch (e) {}
          window.webkit = { messageHandlers: { applyfill: {
            postMessage: (m) => { try { window.top.__sent.push(m); } catch (e) {} } } } };
        """)
        page.goto(f"{server}/embedded_apply.html")
        page.wait_for_timeout(600)
        page.evaluate("() => { void window.__applyDrive({ mode: 'run' }); }")
        page.wait_for_function(
            "() => (window.top.__sent || []).some(m => m.status === 'ready')",
            timeout=20000)
        sent = page.evaluate("window.top.__sent")
        ready = [m for m in sent if m["status"] == "ready"][-1]
        assert ready["filled"] > 0, f"filled the embed but reported nothing: {sent}"
        reveals = [m for m in sent if m.get("detail") == "reveal"]
        assert len(reveals) <= 2, f"spun on the reveal button: {len(reveals)} clicks"
        frame = page.frame(name=None, url=lambda u: u.endswith("greenhouse_basic.html"))
        assert frame.input_value("#email") == "rahil@example.com"
    finally:
        page.close()


def test_an_invisible_hcaptcha_beside_a_form_is_not_a_wall(browser, server, lib):
    """Lever runs hCaptcha in invisible mode: the widget is declared, its script
    has drawn it, and what it drew has no height. Calling that a challenge meant
    refusing to fill every field on a Lever application."""
    page = _load_engine(browser, server, lib, "invisible_hcaptcha_form.html")
    try:
        probe = page.evaluate("window.__applyFormProbe()")
        assert probe["kind"] == "application", probe
        assert page.evaluate("window.__applyFillOrPause()") >= 3
        assert page.input_value("#email") == "rahil@example.com"
    finally:
        page.close()


def test_im_interested_counts_as_the_button_that_opens_the_application(
        browser, server, lib):
    """SmartRecruiters' wording for Apply."""
    page = _load_engine(browser, server, lib, "interested_reveal.html")
    try:
        assert page.evaluate("window.__applyFormProbe()")["revealLabel"] == "I'm interested"
    finally:
        page.close()


def test_fields_sharing_a_block_do_not_inherit_each_others_labels(
        browser, server, lib):
    """The worst kind of miss: not a field left blank, but the *wrong value* typed
    into a real application. On Workable, city/postcode/country all resolved to
    "first name" and got the first name typed into all three."""
    page = browser.new_page()
    try:
        identity = dict(IDENTITY, city="Chicago", zip="60601",
                        country="United States")
        payload = {"identity": identity, "answers": ANSWERS,
                   "rules": fieldmatch.rules_payload()}
        page.add_init_script(f"window.__APPLY = {json.dumps(payload)};")
        page.goto(f"{server}/shared_ancestor_labels.html")
        page.evaluate(lib)
        keys = page.evaluate("""() => {
            const out = {};
            for (const el of document.querySelectorAll("input")) {
                out[el.name] = window.__applyMatchKey(
                    window.__applyFieldLabel(el), el.name, el.id,
                    el.getAttribute("autocomplete"), el.type);
            }
            return out;
        }""")
        assert keys == {"firstname": "first_name", "city": "city",
                        "postcode": "zip", "country": "country"}, keys
        page.evaluate("window.__applyAutofill()")
        page.wait_for_timeout(400)
        assert page.input_value("#firstname") == "Rahil"
        assert page.input_value("#city") == "Chicago"
        assert page.input_value("#postcode") == "60601"
        assert page.input_value("#country") == "United States"
    finally:
        page.close()


# --- radio groups: the wrong value, not a blank -----------------------------

def test_verbose_yes_no_radios_pick_the_side_the_person_answered(
        browser, server, lib):
    """The worst failure this engine had: a *wrong* answer submitted silently.

    Options were matched by substring, and "yes, i know someone who works
    here".includes("no") is true — so a No answer checked Yes on referral and
    sponsorship questions, on real applications, without a word to the person.
    Radio groups now go through the same option picker as <select>."""
    identity = dict(
        IDENTITY,
        work_authorized="Yes", needs_sponsorship="No",
        related_to_employee="No", previously_applied="No",
        willing_to_relocate="Yes", can_travel="Yes", gender="Woman",
    )
    page = run_autofill(browser, server, lib, "verbose_yesno_radios.html",
                        identity=identity)
    try:
        # authorized without sponsorship, and we need none -> the Yes option
        assert page.is_checked("input[name='sponsor'][value='a']")
        assert not page.is_checked("input[name='sponsor'][value='b']")
        # "Yes, I know someone…" must not win a No answer
        assert page.is_checked("input[name='referral'][value='b']")
        assert not page.is_checked("input[name='referral'][value='a']")
        assert page.is_checked("input[name='prior'][value='b']")
        assert page.is_checked("input[name='relocate'][value='a']")
        # not a Yes/No group at all — the substring matcher never filled it
        assert page.is_checked("input[name='gender'][value='f']")
        # already answered by the person: fill blanks, never overwrite
        assert page.is_checked("input[name='travel'][value='b']")
        assert not page.is_checked("input[name='travel'][value='a']")
    finally:
        page.close()


def test_radio_option_pick_matches_python_select_value(browser, lib):
    """Radio groups and <select> must land on the same option."""
    page = browser.new_page()
    try:
        page.evaluate(lib)
        cases = [
            (["Yes, I do not require sponsorship",
              "No, I will require sponsorship now or in the future"], "Yes"),
            (["Yes, I know someone who works here", "No"], "No"),
            (["Yes, I have applied before", "No, this is my first application"], "No"),
            (["Male", "Female", "Non-binary"], "Woman"),
            (["Yes", "No"], "No"),
        ]
        for options, value in cases:
            js = page.evaluate("([o, v]) => window.__applyRadioPick(o, v)",
                               [options, value])
            py = fieldmatch.select_value(options, value)
            assert js == py, (options, value, js, py)
            assert js is not None, (options, value)
    finally:
        page.close()


# --- drafted answers only go where they answer the question ----------------

def test_a_drafted_answer_is_not_reused_for_a_different_question(browser, lib):
    """One shared filler word used to be enough, so every free-text box on the
    form got a paragraph answering something else — which reads as finished."""
    page = browser.new_page()
    try:
        answers = [
            {"question": "Why do you want to work at Acme?", "answer": "WHY-US"},
            {"question": "Why are you a strong fit for the Software Engineer role?",
             "answer": "WHY-FIT"},
            {"question": "Tell us about a relevant project or accomplishment.",
             "answer": "PROJECT"},
        ]
        page.add_init_script(
            f"window.__APPLY = {json.dumps({'identity': {}, 'answers': answers})};")
        page.goto("about:blank")
        page.evaluate(lib)
        matched = {
            "why do you want to work at acme?": "WHY-US",
            "why do you want to work here?": "WHY-US",
            "what makes you a strong fit for this role?": "WHY-FIT",
            "tell us about a project you are proud of": "PROJECT",
        }
        unmatched = [
            "if you could have dinner with anyone, who would it be?",
            "what is your favorite programming language and why?",
            "please list any accommodations you need for the interview",
            "describe a time you disagreed with a teammate and what you did",
        ]
        for label, expected in matched.items():
            got = page.evaluate("(l) => window.__applyBestAnswer(l)", label)
            assert got == expected, (label, got)
        for label in unmatched:
            got = page.evaluate("(l) => window.__applyBestAnswer(l)", label)
            assert got is None, (label, got)
    finally:
        page.close()
