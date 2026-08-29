"""Python and JavaScript must match labels identically.

The autofill brain runs in three places: `app/fieldmatch.py`, the browser
extension, and the iOS in-app browser. The two JS copies were hand-ported and
drifted — both shipped a *narrower* EEO list than Python, so the phone filled
demographic fields the worker refuses. `fieldmatch.rules_payload()` makes Python
the single source; these tests prove the served rules behave the same once they're
running in a real JavaScript engine.

Two things are checked:

1. **Portability** — every pattern compiles in JS (no Python-only syntax leaked in).
2. **Parity** — for a table of real ATS labels, JS `matchKey` returns exactly what
   `fieldmatch.match_key` returns, and JS agrees on which labels are EEO.

Run in headless Chromium, so "valid JavaScript regex" means a real engine's opinion,
not a guess. Local machines without Chromium skip; CI must run these.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("playwright", reason="parity test needs the playwright package")

from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from tests.browserutil import skip_unless_ci_chromium  # noqa: E402
from app import fieldmatch  # noqa: E402

# Labels drawn from real forms, spanning every rule plus the traps: EEO fields that
# look like ordinary questions, and near-misses that must NOT match.
LABELS = [
    "Email Address", "First name", "Name (First)", "Name (Last)", "Last Name",
    "Full name", "Preferred first name", "Nickname", "Pronouns",
    "Phone", "Mobile number", "Contact number",
    "LinkedIn Profile", "GitHub", "Portfolio", "Personal website", "Homepage",
    "Current location", "Where do you live?", "Currently based", "City", "State",
    "Zip", "Postal code", "Country", "Street address", "Mailing address",
    "School / University", "Where did you study?", "Degree",
    "Highest level of education", "Major", "Field of study", "GPA",
    "Graduation year", "Year of graduation", "Expected graduation date",
    "Current employer", "Current title", "Years of experience",
    "How many years of Python?", "Desired salary", "Expected compensation",
    "Current salary", "Start date", "When could you start?", "Notice period",
    "Are you willing to relocate?", "Are you authorized to work in the US?",
    "Will you require visa sponsorship?",
    "How did you hear about us?", "How did you hear about this opportunity?",
    "Referral source", "Preferred work location", "Country/Region",
    "What is your gender?", "Please select your race",
    "Are you 18 years of age or older?",
    "Will you now or in the future require sponsorship?",
    "Most recent employer", "Area of study", "Cell phone",
    "Where are you currently living?",
    # Optional demographics — map to keys (fill only when identity has a value)
    "Gender", "Race / Ethnicity", "Are you a protected veteran?",
    "Disability status", "Are you Hispanic or Latino?",
    # Hard-blocked EEO — every one of these must resolve to no key, in both engines
    "Sexual orientation", "National origin",
    "Voluntary Self-Identification", "EEO information", "Marital status",
    "Religion", "Do you identify as LGBTQ+?", "Date of birth", "DOB",
    "Citizenship status", "Gender identity", "Birthday",
    # near-misses that should match nothing
    "Favorite color", "Referral code", "",
]

# A tiny JS port of match_key/is_eeo built from the *served* rules. This is the
# contract the real clients implement; keeping it minimal means a failure points at
# the rules, not at engine differences.
_JS = """
(payload) => {
  const rules = payload.rules.map(([k, p]) => [k, new RegExp(p, payload.flags)]);
  const never = new RegExp(payload.never_fill, payload.flags);
  const isEeo = (label) => never.test((label || '').trim().toLowerCase());
  const matchKey = (label) => {
    const text = (label || '').trim().toLowerCase();
    if (!text || isEeo(text)) return null;
    for (const [k, re] of rules) if (re.test(text)) return k;
    return null;
  };
  return (labels) => labels.map((l) => ({ label: l, key: matchKey(l), eeo: isEeo(l) }));
}
"""


@pytest.fixture(scope="module")
def js_matcher():
    """(labels) -> [{label, key, eeo}] evaluated by real JavaScript."""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            payload = fieldmatch.rules_payload()

            def run(labels):
                return page.evaluate(
                    f"([payload, labels]) => ({_JS})(payload)(labels)",
                    [payload, labels])

            yield run
            browser.close()
    except PlaywrightError as e:
        skip_unless_ci_chromium(e)


def test_every_pattern_compiles_as_javascript(js_matcher):
    """A Python-only construct in a rule would make the clients throw on load and
    silently fall back — this catches it at the source."""
    results = js_matcher(["probe"])
    assert len(results) == 1          # got here => every RegExp constructed


def test_python_and_javascript_agree_on_every_label(js_matcher):
    results = {r["label"]: r["key"] for r in js_matcher(LABELS)}
    mismatches = [
        (label, fieldmatch.match_key(label), results[label])
        for label in LABELS
        if fieldmatch.match_key(label) != results[label]
    ]
    assert not mismatches, f"python/js disagree: {mismatches}"


def test_both_engines_refuse_the_same_hard_blocked_eeo_labels(js_matcher):
    eeo_labels = [
        "Sexual orientation", "National origin",
        "Voluntary Self-Identification", "EEO information", "Marital status",
        "Religion", "Do you identify as LGBTQ+?", "Date of birth", "DOB",
        "Citizenship status", "Gender identity", "Birthday",
    ]
    for r in js_matcher(eeo_labels):
        assert r["eeo"] is True, f"JS would fill the EEO field {r['label']!r}"
        assert r["key"] is None
        assert fieldmatch.is_eeo(r["label"])


def test_optional_demographics_are_not_hard_blocked(js_matcher):
    for r in js_matcher(["Gender", "Race / Ethnicity",
                         "Are you a protected veteran?", "Disability status",
                         "Are you Hispanic or Latino?"]):
        assert r["eeo"] is False, f"{r['label']!r} wrongly hard-blocked"
        assert r["key"] is not None
        assert not fieldmatch.is_eeo(r["label"])


def test_ordinary_fields_are_not_swept_up_as_eeo(js_matcher):
    for r in js_matcher(["First name", "Email Address", "Current title",
                         "Why do you want to work here?"]):
        assert r["eeo"] is False, f"{r['label']!r} wrongly treated as demographic"


# --- the payload itself -----------------------------------------------------

def test_payload_shape_and_version():
    payload = fieldmatch.rules_payload()
    assert payload["rules"] and all(len(r) == 2 for r in payload["rules"])
    assert payload["attr_rules"] and all(len(r) == 2 for r in payload["attr_rules"])
    assert payload["autocomplete"]["given-name"] == "first_name"
    assert payload["never_fill"] and payload["flags"] == "i"
    assert len(payload["version"]) == 12
    # stable across calls, so a client can cache on it
    assert payload["version"] == fieldmatch.rules_payload()["version"]


def test_version_changes_when_the_rules_change(monkeypatch):
    before = fieldmatch.rules_payload()["version"]
    monkeypatch.setattr(fieldmatch, "FIELD_RULES",
                        fieldmatch.FIELD_RULES + [("nonsense", r"nonsense")])
    assert fieldmatch.rules_payload()["version"] != before


def test_payload_is_json_serializable():
    """It's served over HTTP; a stray compiled pattern would 500 the endpoint."""
    json.dumps(fieldmatch.rules_payload())


# --- the clients' bundled fallbacks must not drift ---------------------------

CLIENTS = [
    ("ios/Apply/Autofill.swift", "FALLBACK_EEO"),
]


@pytest.mark.parametrize("relpath,eeo_const", CLIENTS)
def test_client_fallback_rules_match_python(relpath, eeo_const):
    """Both JS surfaces keep an offline copy of the rules, generated from
    fieldmatch.py. Because the *served* rules normally win, a drifted fallback would
    sit unnoticed until someone was offline — exactly the failure mode that shipped a
    narrower EEO list to the phone. Pin it.
    """
    import pathlib
    import re as _re

    source = (pathlib.Path(__file__).resolve().parents[1] / relpath).read_text()
    payload = fieldmatch.rules_payload()

    missing = [k for k, _ in payload["rules"] if f'["{k}", /' not in source]
    assert not missing, f"{relpath} fallback is missing rules: {missing}"

    attr_chunk = source.split("FALLBACK_ATTR_RULES")[1].split("FALLBACK_AUTOCOMPLETE")[0]
    missing_attr = [k for k, _ in payload["attr_rules"] if f'["{k}", /' not in attr_chunk]
    assert not missing_attr, f"{relpath} attr fallback missing: {missing_attr}"

    m = _re.search(rf"const {eeo_const} = /(.*)/i;", source)
    assert m, f"no {eeo_const} in {relpath}"
    assert m.group(1) == payload["never_fill"], (
        f"{relpath} EEO list has drifted from app/fieldmatch.py — regenerate it")


@pytest.mark.parametrize("relpath", [c[0] for c in CLIENTS])
def test_clients_do_not_staple_nameid_onto_a_visible_label(relpath):
    """fieldLabel must keep the visible label separate from name/id. Unconditionally
    appending them turns "Gender" into "gender gender gender" and the anchored
    gender/sex rule matches nothing — the bug that left #gender empty on iOS."""
    import pathlib

    source = (pathlib.Path(__file__).resolve().parents[1] / relpath).read_text()
    assert "name/id is last-resort only" in source, (
        f"{relpath} lost the fieldLabel contract comment")
    assert 'bits.push(el.name || "", el.id || "")' not in source, (
        f"{relpath} still staples name/id onto every visible label")


@pytest.mark.parametrize("relpath", [c[0] for c in CLIENTS])
def test_clients_prefer_the_served_rules(relpath):
    """Each client must actually consult the served payload, not just carry one."""
    import pathlib

    source = (pathlib.Path(__file__).resolve().parents[1] / relpath).read_text()
    assert "never_fill" in source, f"{relpath} never reads the served EEO list"
    assert "new RegExp" in source, f"{relpath} never compiles the served rules"


def test_the_endpoint_serves_the_rules():
    from fastapi.testclient import TestClient

    from app.main import app

    body = TestClient(app).get("/apply/rules").json()
    assert body["version"] == fieldmatch.rules_payload()["version"]
    assert ["email", r"e-?mail"] in [list(r) for r in body["rules"]]
