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

_HEADERS = {"X-Apply-Token": TOKEN} if TOKEN else {}

# Injected into the page to read every fillable field's label + options, tagging
# each with data-jaf-id so we can act on it afterward. Mirrors the extension's
# label extraction.
_EXTRACT_JS = r"""
() => {
  const lbl = (el) => {
    const bits = [];
    if (el.id){ const l=document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if(l) bits.push(l.textContent); }
    const w = el.closest('label'); if (w) bits.push(w.textContent);
    if (el.getAttribute('aria-label')) bits.push(el.getAttribute('aria-label'));
    const by = el.getAttribute('aria-labelledby');
    if (by) by.split(/\s+/).forEach(id=>{const n=document.getElementById(id); if(n) bits.push(n.textContent);});
    if (el.placeholder) bits.push(el.placeholder);
    bits.push(el.name||'', el.id||'');
    return bits.join(' ').replace(/\s+/g,' ').trim();
  };
  const out = [];
  let i = 0;
  document.querySelectorAll('input, textarea, select').forEach(el => {
    const t = (el.type||'').toLowerCase();
    if (['hidden','submit','button','file','checkbox','image','reset'].includes(t)) return;
    if (!(el.offsetParent || el.getClientRects().length) || el.disabled || el.readOnly) return;
    el.setAttribute('data-jaf-id', i);
    const tag = el.tagName.toLowerCase();
    out.push({ id: i, label: lbl(el), tag, type: t,
      options: tag==='select' ? [...el.options].map(o=>o.text) : [] });
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


def _wait_for_form(page, timeout_ms: int = 20000) -> list:
    """Poll until some frame exposes fillable fields (forms render after JS on SPAs),
    up to timeout_ms. Returns the same shape as _extract_frames (possibly empty)."""
    deadline = time.time() + timeout_ms / 1000
    while True:
        frames = _extract_frames(page)
        if frames or time.time() >= deadline:
            return frames
        page.wait_for_timeout(500)


def _reveal_form(page) -> bool:
    """Click an 'Apply' button/link to open the real form when the URL landed on a
    description page. Returns True if it clicked something."""
    for sel in _APPLY_TRIGGERS:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(1500)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def fill_form(page, job: dict) -> dict:
    """Fill the page from the job's identity + answers. Returns a preview summary."""
    identity = job.get("identity", {})
    questions = job.get("questions", [])
    page.goto(job["url"], wait_until="domcontentloaded", timeout=45000)

    frames = _wait_for_form(page)
    if not frames and _reveal_form(page):   # landed on a description — open the form
        frames = _wait_for_form(page)
    frame, fields = frames[0] if frames else (page.main_frame, [])
    print(f"[worker] {page.url} — {len(fields)} fillable field(s) "
          f"across {len(page.frames)} frame(s)")

    filled, skipped = [], []
    for f in fields:
        sel = f'[data-jaf-id="{f["id"]}"]'
        label, tag = f["label"], f["tag"]
        key = fieldmatch.match_key(label)
        try:
            if key and identity.get(key):
                value = identity[key]
                if tag == "select":
                    opt = fieldmatch.select_value(f["options"], value)
                    if opt:
                        frame.select_option(sel, label=opt)
                        filled.append({"label": label, "value": opt})
                    else:
                        skipped.append(label)
                else:
                    frame.fill(sel, str(value))
                    filled.append({"label": label, "value": str(value)})
            elif tag == "textarea" or fieldmatch.is_essay_label(label):
                ans = _pick_answer(label, questions)
                if ans:
                    frame.fill(sel, ans)
                    filled.append({"label": label, "value": ans[:60] + "…"})
                else:
                    skipped.append(label)
            elif label:
                skipped.append(label)
        except Exception as e:  # noqa: BLE001 — one stubborn field never aborts the fill
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


def submit_form(page) -> None:
    """Click the form's submit button. Heuristic; tune per ATS."""
    for sel in ('button[type="submit"]', 'input[type="submit"]',
                'button:has-text("Submit")', 'button:has-text("Apply")'):
        btn = page.query_selector(sel)
        if btn:
            btn.click()
            page.wait_for_timeout(3000)
            return
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
        page.close()


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
        finally:
            browser.close()


if __name__ == "__main__":
    raise SystemExit(main(once="--once" in sys.argv))
