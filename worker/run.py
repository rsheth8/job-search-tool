"""Headless submit worker (Phase 2).

A separate, on-demand service that fills — and, after you approve the preview,
submits — public application forms (Greenhouse / Lever / Ashby; no login needed).
It reuses the main app's field-matching brain (``app.fieldmatch``) so it makes the
exact same decisions as the in-browser extension.

Flow per job:
  1. claim a pending fill request from the main app  (POST /worker/claim)
  2. open the public form, fill every field it recognizes, screenshot
  3. report the filled preview                        (POST /worker/preview)
  4. poll until you approve (or cancel) on your phone  (GET  /apply/request)
  5. on approval, click submit and report the result   (POST /worker/result)

NOTHING is submitted without your explicit approval — step 4 is a human gate.

Run:  BASE_URL=https://job-search-tool.fly.dev APPLY_API_TOKEN=… python -m worker.run
      add --once to process a single job and exit (handy for testing).

NOTE: the browser-driving here is the one piece that can't be unit-tested — run it
against a real form with you watching the first few times and tune from there.
"""
from __future__ import annotations

import base64
import os
import re
import sys
import time

import httpx

from app import ats, fieldmatch

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("APPLY_API_TOKEN", "")
POLL_SECONDS = int(os.environ.get("WORKER_POLL_SECONDS", "5"))
APPROVE_TIMEOUT = int(os.environ.get("WORKER_APPROVE_TIMEOUT", "900"))  # 15 min
# Run with a visible browser for hands-on tuning: WORKER_HEADLESS=false.
HEADLESS = os.environ.get("WORKER_HEADLESS", "true").lower() not in ("false", "0", "no")
# WORKER_AGENT=true → drive the form with the LLM browser agent (worker/agent.py)
# instead of the hard-coded fieldmatch filler. Needs ANTHROPIC_API_KEY.
USE_AGENT = os.environ.get("WORKER_AGENT", "false").lower() in ("true", "1", "yes")
# How long to wait for a form to render, and how much of that to spend before
# trying an "Apply" reveal click (a description page never sprouts a form on its own).
FORM_WAIT_MS = int(os.environ.get("WORKER_FORM_WAIT_MS", "20000"))
REVEAL_PROBE_MS = int(os.environ.get("WORKER_REVEAL_PROBE_MS", "3000"))

_HEADERS = {"X-Apply-Token": TOKEN} if TOKEN else {}

# Injected into the page to read every fillable field's label + options, tagging
# each with data-jaf-id so we can act on it afterward. Mirrors the extension's
# label extraction.
_EXTRACT_JS = r"""
() => {
  const clean = (s) => (s||'').replace(/\s+/g,' ').trim();
  // The *visible* label only. Kept separate from the name/id hint because the
  // rules anchor on exact wording: a bare "Name" field matches /^name$/ until you
  // staple "_systemfield_name" onto it, and then it matches nothing at all.
  const lbl = (el) => {
    const bits = [];
    if (el.id){ const l=document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if(l) bits.push(l.textContent); }
    const w = el.closest('label'); if (w) bits.push(w.textContent);
    if (el.getAttribute('aria-label')) bits.push(el.getAttribute('aria-label'));
    const by = el.getAttribute('aria-labelledby');
    if (by) by.split(/\s+/).forEach(id=>{const n=document.getElementById(id); if(n) bits.push(n.textContent);});
    if (el.placeholder) bits.push(el.placeholder);
    return clean(bits.join(' '));
  };
  // Fallback signal for unlabelled fields, matched only after the visible label.
  const hint = (el) => clean([el.name||'', el.id||''].join(' '));
  const vis = (el) => !!(el.offsetParent || el.getClientRects().length);
  // A radio's *option* text must be the visible choice alone ("Yes"), never the
  // name/id noise lbl() appends — option matching compares against these.
  const optLabel = (el) => {
    let t = '';
    if (el.id){ const l=document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if(l) t = l.textContent; }
    if (!clean(t)) { const w = el.closest('label'); if (w) t = w.textContent; }
    if (!clean(t)) t = el.getAttribute('aria-label') || '';
    if (!clean(t)) t = el.value || '';
    return clean(t);
  };
  const out = [];
  let i = 0;
  const tagit = (el) => { el.setAttribute('data-jaf-id', i); return i++; };

  // 1. Text inputs, textareas, native <select>s.
  document.querySelectorAll('input, textarea, select').forEach(el => {
    const t = (el.type||'').toLowerCase();
    // radio is handled as a *group* below; the rest are never fillable.
    if (['hidden','submit','button','file','checkbox','image','reset','radio'].includes(t)) return;
    if (!vis(el) || el.disabled || el.readOnly) return;
    const tag = el.tagName.toLowerCase();
    out.push({ id: tagit(el), label: lbl(el), hint: hint(el), tag, type: t,
      kind: tag==='select' ? 'select' : 'text',
      options: tag==='select' ? [...el.options].map(o=>o.text) : [] });
  });

  // 2. Radio groups — one record per group name. A Yes/No question is a *group*
  //    decision, not N independent fields, so we present it that way.
  const groups = new Map();
  document.querySelectorAll('input[type="radio"]').forEach(el => {
    if (!vis(el) || el.disabled) return;
    const name = el.name || ('__anon' + i);
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(el);
  });
  groups.forEach((els, name) => {
    let glabel = '';
    const fs = els[0].closest('fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg) glabel = lg.textContent; }
    if (!glabel) {
      const by = els[0].getAttribute('aria-labelledby');
      if (by) by.split(/\s+/).forEach(id=>{const n=document.getElementById(id); if(n) glabel += ' ' + n.textContent;});
    }
    if (!clean(glabel)) glabel = name;
    const radios = els.map(el => ({ id: tagit(el), text: optLabel(el) }));
    out.push({ id: null, label: clean(glabel), hint: name, tag: 'radiogroup',
      type: 'radio', kind: 'radiogroup', options: radios.map(r=>r.text), radios });
  });

  // 3. Custom (non-native) dropdowns — an ARIA combobox is a div, not a <select>,
  //    so it needs a click-open → click-option dance. Options are read after it
  //    opens (popups are usually rendered lazily / portalled to <body>).
  document.querySelectorAll('[role="combobox"], [aria-haspopup="listbox"]').forEach(el => {
    if (!vis(el) || el.disabled) return;
    if (el.tagName.toLowerCase() === 'select') return;   // native, already captured
    if (el.hasAttribute('data-jaf-id')) return;          // a tagged text input
    out.push({ id: tagit(el), label: lbl(el), hint: hint(el), tag: 'combobox',
      type: '', kind: 'combobox', options: [] });
  });
  return out;
}
"""

# Options of an open ARIA listbox, tagged so we can click the chosen one. Read
# *after* the combobox is clicked open, since popups render lazily.
_OPTIONS_JS = r"""
() => {
  // Clear tags from a previously-opened dropdown first: its options are hidden now,
  // and a stale data-jaf-opt="0" makes the next dropdown's option ambiguous — the
  // click then waits on an invisible element until it times out.
  document.querySelectorAll('[data-jaf-opt]').forEach(el => el.removeAttribute('data-jaf-opt'));
  const out = [];
  let i = 0;
  document.querySelectorAll('[role="option"]').forEach(el => {
    if (!(el.offsetParent || el.getClientRects().length)) return;   // still closed
    el.setAttribute('data-jaf-opt', i);
    out.push({ id: i, text: (el.textContent||'').replace(/\s+/g,' ').trim() });
    i++;
  });
  return out;
}
"""


# File-upload inputs, tagged so we can attach the resume to the right one. These
# are skipped by _EXTRACT_JS (type=file), and are often visually hidden behind a
# styled button, so we read them separately and match on their label.
_FILE_EXTRACT_JS = r"""
() => {
  const lbl = (el) => {
    const bits = [];
    if (el.id){ const l=document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if(l) bits.push(l.textContent); }
    const w = el.closest('label'); if (w) bits.push(w.textContent);
    if (el.getAttribute('aria-label')) bits.push(el.getAttribute('aria-label'));
    bits.push(el.name||'', el.id||'');
    return bits.join(' ').replace(/\s+/g,' ').trim();
  };
  const out = [];
  let i = 0;
  document.querySelectorAll('input[type="file"]').forEach(el => {
    if (el.disabled) return;
    el.setAttribute('data-jaf-file', i);
    out.push({ id: i, label: lbl(el) });
    i++;
  });
  return out;
}
"""


def _api(method: str, path: str, **kw) -> dict:
    r = httpx.request(method, BASE_URL + path, headers=_HEADERS, timeout=30, **kw)
    r.raise_for_status()
    return r.json() if r.content else {}


def _fetch_resume(job: dict) -> dict | None:
    """Download the staged tailored resume PDF for this job, or None. Returns a
    Playwright FilePayload ({name, mimeType, buffer}) ready for set_input_files."""
    if not job.get("has_resume"):
        return None
    try:
        r = httpx.get(
            BASE_URL + "/apply/resume", headers=_HEADERS, timeout=60,
            params={"user": job["user"], "id": job["posting_id"]},
        )
        r.raise_for_status()
        if not r.content:
            return None
    except Exception as e:  # noqa: BLE001 — a missing resume never aborts the fill
        print(f"[worker] resume fetch failed: {e}")
        return None
    name = "resume.pdf"
    disp = r.headers.get("content-disposition", "")
    if 'filename="' in disp:
        name = disp.split('filename="', 1)[1].split('"', 1)[0] or name
    return {"name": name, "mimeType": "application/pdf", "buffer": r.content}


def _attach_resume(frame, resume: dict | None) -> list[str]:
    """Attach the resume to file-upload fields in the form's frame. Targets fields
    whose label reads as a resume/CV; if none match but there's exactly one file
    input, uses that. Returns labels of fields it filled (for the preview)."""
    if not resume:
        return []
    files = frame.evaluate(_FILE_EXTRACT_JS)
    if not files:
        return []
    targets = [f for f in files if fieldmatch.is_resume_field(f["label"])]
    if not targets and len(files) == 1:
        targets = files  # a lone unlabeled upload is almost always the resume
    attached = []
    for f in targets:
        try:
            frame.set_input_files(f'[data-jaf-file="{f["id"]}"]', files=[resume])
            attached.append(f["label"] or "Resume")
        except Exception as e:  # noqa: BLE001
            print(f"[worker] resume attach failed for {f['label']!r}: {e}")
    return attached


def _pick_answer(label: str, questions: list[dict]) -> str | None:
    """Best drafted answer for an essay field: the question whose words overlap the
    field label most, else the first answer."""
    if not questions:
        return None
    words = set(label.lower().split())
    best, score = None, -1
    for q in questions:
        overlap = len(words & set(q["question"].lower().split()))
        if overlap > score:
            best, score = q, overlap
    return (best or questions[0]).get("answer")


# Text on the button/link that opens the real application form when the URL lands
# on a job *description* instead (Lever, some Greenhouse/Ashby description pages).
_APPLY_TRIGGERS = (
    'a.postings-btn',                              # Lever "Apply for this job"
    'a:has-text("Apply for this job")',
    'button:has-text("Apply for this job")',
    'a:has-text("Apply now")', 'button:has-text("Apply now")',
    'a[href*="/apply"]',
    'a:has-text("Apply")', 'button:has-text("Apply")',
)


def _extract_frames(page) -> list:
    """(frame, fields) for every frame that has fillable fields, most fields first.

    Application forms are very often inside an <iframe> (embedded Greenhouse/Lever/
    Ashby on a company careers page), so we look in EVERY frame, not just the top
    one — that's the #1 reason a fill comes back empty."""
    results = []
    for fr in page.frames:
        try:
            fields = fr.evaluate(_EXTRACT_JS)
        except Exception:  # noqa: BLE001 — a cross-origin/detached frame just gets skipped
            continue
        if fields:
            results.append((fr, fields))
    results.sort(key=lambda t: len(t[1]), reverse=True)
    return results


def _wait_for_form(page, timeout_ms: int = 20000, settle_ms: int = 400) -> list:
    """Poll until some frame exposes fillable fields (forms render after JS on SPAs),
    up to timeout_ms. Returns the same shape as _extract_frames (possibly empty).

    Once fields appear we wait ``settle_ms`` and re-read: React forms commonly paint
    in two passes, and grabbing the first paint fills half a form. We keep whichever
    read saw more fields, so a settled render always wins."""
    deadline = time.time() + timeout_ms / 1000
    while True:
        frames = _extract_frames(page)
        if frames:
            page.wait_for_timeout(settle_ms)
            settled = _extract_frames(page)
            if settled and len(settled[0][1]) >= len(frames[0][1]):
                return settled
            return frames
        if time.time() >= deadline:
            return []
        page.wait_for_timeout(500)


# A reveal trigger must never be the form's own submit button — clicking that would
# submit without approval. Text matching either of these is not a reveal.
_NOT_A_REVEAL = re.compile(r"submit|send application|finish|complete application", re.I)


def _reveal_form(page) -> bool:
    """Click an 'Apply' button/link to open the real form when the URL landed on a
    description page. Returns True if it clicked something."""
    for sel in _APPLY_TRIGGERS:
        try:
            for el in page.query_selector_all(sel):
                if not el.is_visible():
                    continue
                text = (el.inner_text() or "").strip()
                if text and _NOT_A_REVEAL.search(text):
                    continue   # that's a submit button, not a reveal — never click it
                el.click()
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(1500)
                print(f"[worker] revealed the form via {text or sel!r}")
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _match_key(label: str, hint: str) -> str | None:
    """The identity key for a field, matching the visible label first and only then
    falling back to its name/id. Two passes because the rules anchor on wording —
    "Name" is a full-name field, "Name _systemfield_name" matched nothing — while
    the hint still rescues fields with no visible label at all."""
    return fieldmatch.match_key(label) or (
        fieldmatch.match_key(f"{label} {hint}".strip()) if hint else None)


def _is_eeo(label: str, hint: str) -> bool:
    """EEO check across both signals: a demographic <select> often carries a bare
    label and gives itself away only through name="gender"."""
    return fieldmatch.is_eeo(label) or (bool(hint) and fieldmatch.is_eeo(hint))


def _log(label: str, key: str | None, action: str, result: str, reason: str = "") -> None:
    """One structured line per field, so a live run is diagnosable after the fact:
    what we saw, what we matched it to, what we did, and why it went that way."""
    print(f"[worker]   {label[:52]!r:56} key={key or '-':<20} {action:<9} {result}"
          + (f" — {reason}" if reason else ""))


def _fill_radiogroup(frame, rec: dict, value) -> tuple[bool, str]:
    """Check the radio in the group whose label best matches ``value``.
    Returns (filled, detail)."""
    opt = fieldmatch.select_value(rec.get("options", []), value)
    if not opt:
        return False, f"no option matches {value!r} in {rec.get('options', [])}"
    for r in rec.get("radios", []):
        if r["text"] == opt:
            frame.check(f'[data-jaf-id="{r["id"]}"]')
            return True, opt
    return False, f"option {opt!r} vanished from the group"


def _fill_combobox(page, frame, rec: dict, value) -> tuple[bool, str]:
    """Fill a custom ARIA combobox: click it open, read the options that appear,
    then click the best match. Native <select>s never come through here."""
    sel = f'[data-jaf-id="{rec["id"]}"]'
    frame.click(sel)
    page.wait_for_timeout(250)
    # The popup may be portalled out of the combobox's own frame, so look in the
    # frame first and fall back to the top document.
    options = frame.evaluate(_OPTIONS_JS) or page.main_frame.evaluate(_OPTIONS_JS)
    if not options:
        return False, "combobox opened but exposed no role=option items"
    opt = fieldmatch.select_value([o["text"] for o in options], value)
    if not opt:
        return False, f"no option matches {value!r} in {[o['text'] for o in options][:8]}"
    for o in options:
        if o["text"] == opt:
            try:
                frame.click(f'[data-jaf-opt="{o["id"]}"]')
            except Exception:  # noqa: BLE001 — portalled popup lives in the top frame
                page.main_frame.click(f'[data-jaf-opt="{o["id"]}"]')
            page.wait_for_timeout(150)
            return True, opt
    return False, f"option {opt!r} vanished from the listbox"


def fill_form(page, job: dict) -> dict:
    """Fill the page from the job's identity + answers. Returns a preview summary."""
    identity = job.get("identity", {})
    questions = job.get("questions", [])
    page.goto(job["url"], wait_until="domcontentloaded", timeout=45000)

    # Probe briefly first: on a job *description* page no amount of waiting produces
    # a form, so spending the whole budget before trying the "Apply" reveal just
    # stalls every Lever-style posting for the full timeout.
    frames = _wait_for_form(page, timeout_ms=REVEAL_PROBE_MS)
    if not frames and _reveal_form(page):   # landed on a description — open the form
        frames = _wait_for_form(page, timeout_ms=FORM_WAIT_MS)
    elif not frames:                        # no reveal to click; it's just slow
        frames = _wait_for_form(page, timeout_ms=FORM_WAIT_MS - REVEAL_PROBE_MS)
    frame, fields = frames[0] if frames else (page.main_frame, [])
    print(f"[worker] {page.url} — {len(fields)} fillable field(s) "
          f"across {len(page.frames)} frame(s)")

    filled, skipped = [], []
    for f in fields:
        label, kind = f["label"], f.get("kind", "text")
        hint = f.get("hint", "")
        sel = f'[data-jaf-id="{f["id"]}"]' if f.get("id") is not None else None
        key = _match_key(label, hint)
        try:
            # Belt-and-suspenders: demographic questions are the human's to answer,
            # on every control type, before any other rule can claim them.
            if _is_eeo(label, hint):
                _log(label, None, "eeo", "skipped", "demographic — never auto-filled")
                skipped.append(label)
                continue

            # --- choice controls: native select, custom combobox, radio group ---
            if kind in ("select", "combobox", "radiogroup"):
                # option_for is the shared decision so all three agree; a combobox's
                # options aren't known until it opens, so it resolves its own.
                if kind == "combobox":
                    value = identity.get(key) if key else None
                    if not key:
                        _log(label, key, kind, "skipped", "no identity key (or EEO)")
                        skipped.append(label)
                        continue
                    if not value:
                        _log(label, key, kind, "skipped", "identity has no value")
                        skipped.append(label)
                        continue
                    ok, detail = _fill_combobox(page, frame, f, value)
                else:
                    key, opt = fieldmatch.option_for(label, f.get("options", []),
                                                     identity, key=key)
                    if not key:
                        _log(label, key, kind, "skipped", "no identity key (or EEO)")
                        skipped.append(label)
                        continue
                    if not opt:
                        _log(label, key, kind, "skipped",
                             f"no option matched identity {identity.get(key)!r}")
                        skipped.append(label)
                        continue
                    if kind == "select":
                        frame.select_option(sel, label=opt)
                        ok, detail = True, opt
                    else:
                        ok, detail = _fill_radiogroup(frame, f, identity[key])
                if ok:
                    _log(label, key, kind, "filled", detail)
                    filled.append({"label": label, "value": detail})
                else:
                    _log(label, key, kind, "skipped", detail)
                    skipped.append(label)

            # --- plain text fields we have a fact for ---
            elif key and identity.get(key):
                value = str(identity[key])
                frame.fill(sel, value)
                _log(label, key, "text", "filled", value[:40])
                filled.append({"label": label, "value": value})

            # --- free-text / essay questions, answered from the drafted answers ---
            elif f["tag"] == "textarea" or fieldmatch.is_essay_label(label):
                ans = _pick_answer(label, questions)
                if ans:
                    frame.fill(sel, ans)
                    _log(label, key, "essay", "filled", f"{len(ans)} chars")
                    filled.append({"label": label, "value": ans[:60] + "…"})
                else:
                    _log(label, key, "essay", "skipped", "no drafted answer matched")
                    skipped.append(label)
            elif label:
                _log(label, key, "text", "skipped", "unrecognized field")
                skipped.append(label)
        except Exception as e:  # noqa: BLE001 — one stubborn field never aborts the fill
            _log(label, key, kind, "error", e.__class__.__name__)
            skipped.append(f"{label} ({e.__class__.__name__})")

    for label in _attach_resume(frame, job.get("resume")):
        filled.append({"label": label, "value": job["resume"]["name"]})

    if not fields:
        skipped.append("⚠️ no form fields found on this page — is the URL the "
                       "application form (not the job description)?")

    return {
        "filled": filled,
        "skipped": skipped[:20],
        "screenshot_url": _screenshot(page),
    }


def _screenshot(page) -> str | None:
    """A full-page JPEG of the filled form as a data: URL, so the phone preview
    shows the actual form (not just a field list). None if capture fails."""
    try:
        png = page.screenshot(full_page=True, type="jpeg", quality=55)
    except Exception as e:  # noqa: BLE001 — a screenshot is nice-to-have, not required
        print(f"[worker] screenshot failed: {e}")
        return None
    return "data:image/jpeg;base64," + base64.b64encode(png).decode()


# Submit-button candidates, most-specific first. An in-form typed submit is the
# safest signal; free-text matches come last and are text-guarded below.
_SUBMIT_SELECTORS = (
    'form button[type="submit"]',
    'form input[type="submit"]',
    'button[type="submit"]',
    'input[type="submit"]',
    'button:has-text("Submit application")',
    'button:has-text("Submit Application")',
    'button:has-text("Send application")',
    'button:has-text("Submit")',
    '[role="button"]:has-text("Submit application")',
)

# Never click these even if a selector matches: "Apply for this job" only *reveals*
# the form (clicking it mid-flow loses the filled data), and the rest aren't submits.
_NOT_SUBMIT = re.compile(
    r"apply for this job|apply now|^\s*apply\s*$|save|cancel|back|previous|"
    r"next|continue|add another|upload", re.I)


def submit_form(page) -> None:
    """Click the form's real submit button.

    Searches **every frame** — an embedded Greenhouse/Ashby form keeps its submit
    button inside the iframe, so a top-frame-only search silently found nothing.
    Only ever called after the user approved the preview.
    """
    for fr in page.frames:
        for sel in _SUBMIT_SELECTORS:
            try:
                candidates = fr.query_selector_all(sel)
            except Exception:  # noqa: BLE001 — detached/cross-origin frame
                break
            for btn in candidates:
                try:
                    if not btn.is_visible() or not btn.is_enabled():
                        continue
                    text = (btn.inner_text() or btn.get_attribute("value") or "").strip()
                    if text and _NOT_SUBMIT.search(text):
                        continue   # a reveal/navigation button, not the submit
                    btn.click()
                    page.wait_for_timeout(3000)
                    print(f"[worker] submitted via {sel} ({text or 'no text'!r})")
                    return
                except Exception:  # noqa: BLE001 — try the next candidate
                    continue
    raise RuntimeError("no submit button found")


def handle_job(browser, job: dict) -> None:
    rid = job["request_id"]
    # Safety net: the server gates this too, but never fill a non-first-party URL —
    # aggregator/login/captcha pages have no form, so hand off to the desktop extension.
    if not ats.is_fillable_form(job.get("url")):
        _api("POST", "/worker/result", json={
            "request_id": rid, "status": "failed",
            "error": "Not a directly fillable form (aggregator / login / captcha) — "
                     "open it on your computer and finish with the browser extension."})
        print(f"[worker] req {rid}: {job.get('url')} isn't a first-party ATS form — handed off")
        return
    job["resume"] = _fetch_resume(job)
    page = browser.new_page()
    try:
        if USE_AGENT:
            from worker import agent
            import anthropic
            preview = agent.run_agent(page, job, anthropic.Anthropic())
            if preview.get("status") == "blocked":
                _api("POST", "/worker/result", json={
                    "request_id": rid, "status": "failed",
                    "error": f"Agent stopped: {preview.get('reason','')} — finish on "
                             "your computer with the browser extension."})
                print(f"[worker] req {rid}: agent blocked — {preview.get('reason','')}")
                return
        else:
            preview = fill_form(page, job)
        _api("POST", "/worker/preview", json={"request_id": rid, "preview": preview})
        print(f"[worker] req {rid}: filled {len(preview['filled'])}, awaiting approval")

        waited = 0
        while waited < APPROVE_TIMEOUT:
            time.sleep(POLL_SECONDS)
            waited += POLL_SECONDS
            req = _api("GET", "/apply/request",
                       params={"user": job["user"], "posting_id": job["posting_id"]}).get("request")
            status = (req or {}).get("status")
            if status == "approved":
                submit_form(page)
                _api("POST", "/worker/result", json={"request_id": rid, "status": "submitted"})
                print(f"[worker] req {rid}: submitted")
                return
            if status in ("failed", "submitted"):  # cancelled or already done
                print(f"[worker] req {rid}: {status}, dropping")
                return
        _api("POST", "/worker/result",
             json={"request_id": rid, "status": "failed", "error": "approval timed out"})
    except Exception as e:  # noqa: BLE001
        _api("POST", "/worker/result",
             json={"request_id": rid, "status": "failed", "error": str(e)})
        print(f"[worker] req {rid}: failed — {e}")
    finally:
        try:
            page.close()
        except Exception:
            pass


def main(once: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        try:
            while True:
                job = _api("POST", "/worker/claim")
                if job:
                    handle_job(browser, job)
                    if once:
                        return 0
                else:
                    if once:
                        print("[worker] nothing to do")
                        return 0
                    time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            print("\n[worker] interrupted")
            return 130
        finally:
            try:
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main(once="--once" in sys.argv))
