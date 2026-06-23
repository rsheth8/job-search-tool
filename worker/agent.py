"""LLM browser agent — fills public application forms by *reasoning over the page*
like a human, instead of hard-coded selectors.

Why this exists: real apply flows have iframes, "Apply" buttons that reveal the
form, multi-step pages, and endless field-label variety. Hard-coded selectors lose
that fight. Here Claude looks at the page's interactive elements each step and picks
one action (fill / choose / click / upload / scroll), and the worker executes it
against Playwright. The loop repeats until the form is filled.

Safety model (unchanged from the rest of the pipeline):
  • It **never submits.** When the form is complete it calls ``ready_for_review`` and
    hands off to the existing human preview → approve → submit gate.
  • It **never fills EEO/demographic fields** (``app.fieldmatch`` is the guard) and is
    told so in the system prompt.
  • Logins and captchas are a hard stop — it calls ``blocked`` and the job is handed
    to the desktop extension.

Surface: Claude API + tool use, *manual* agentic loop (so we keep the human gate).
Model is ``AGENT_MODEL`` (default ``claude-opus-4-8``); drop to ``claude-haiku-4-5``
to cut cost. Adaptive thinking on; one browser action per step
(``disable_parallel_tool_use``) for safe ordering. The applicant profile is cached
via prompt caching so only the per-step page snapshot is charged at full rate.
"""
from __future__ import annotations

import json
import os

from app import fieldmatch

MODEL = os.environ.get("AGENT_MODEL", "claude-opus-4-8")
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "40"))

# Page perception: every visible, interactive element across every frame, tagged
# with a stable agent id (data-agent-id) so the agent can act on it afterward.
# Returns lean records (label + kind + current value/options) to keep tokens down.
_SNAPSHOT_JS = r"""
(prefix) => {
  const lbl = (el) => {
    const bits = [];
    if (el.id){ const l=document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if(l) bits.push(l.textContent); }
    const w = el.closest('label'); if (w) bits.push(w.textContent);
    if (el.getAttribute('aria-label')) bits.push(el.getAttribute('aria-label'));
    const by = el.getAttribute('aria-labelledby');
    if (by) by.split(/\s+/).forEach(id=>{const n=document.getElementById(id); if(n) bits.push(n.textContent);});
    if (el.placeholder) bits.push(el.placeholder);
    bits.push(el.name||'', el.id||'');
    return bits.join(' ').replace(/\s+/g,' ').trim().slice(0, 160);
  };
  const vis = (el) => (el.offsetParent || el.getClientRects().length) && !el.disabled;
  const out = [];
  let i = 0;
  const tag = (el) => { const aid = prefix + (i++); el.setAttribute('data-agent-id', aid); return aid; };

  document.querySelectorAll('input, textarea, select').forEach(el => {
    const t = (el.type||'').toLowerCase();
    if (['hidden','image','reset'].includes(t)) return;
    if (!vis(el) || el.readOnly) return;
    const tag_ = el.tagName.toLowerCase();
    const rec = { aid: tag(el), label: lbl(el) };
    if (tag_ === 'select') { rec.kind = 'select'; rec.options = [...el.options].map(o=>o.text).slice(0,40); rec.value = (el.options[el.selectedIndex]||{}).text || ''; }
    else if (t === 'file') { rec.kind = 'file'; }
    else if (t === 'checkbox') { rec.kind = 'checkbox'; rec.checked = el.checked; }
    else if (t === 'radio') { rec.kind = 'radio'; rec.checked = el.checked; rec.group = el.name||''; }
    else { rec.kind = 'text'; if (el.value) rec.value = String(el.value).slice(0,60); }
    out.push(rec);
  });
  // Buttons / links that drive multi-step flows (Apply, Next, Continue, Submit…).
  document.querySelectorAll('button, a[href], [role=button]').forEach(el => {
    if (!vis(el)) return;
    const text = (el.innerText||el.textContent||el.getAttribute('aria-label')||'').replace(/\s+/g,' ').trim().slice(0,80);
    if (!text) return;
    out.push({ aid: tag(el), label: text, kind: 'button' });
  });
  return out;
}
"""

TOOLS = [
    {"name": "fill", "description": "Type text into a text field or textarea.",
     "input_schema": {"type": "object", "properties": {
         "aid": {"type": "string", "description": "The element's agent id."},
         "text": {"type": "string", "description": "The value to type."}},
         "required": ["aid", "text"]}},
    {"name": "choose", "description": "Pick an option in a dropdown, or select a radio button / checkbox.",
     "input_schema": {"type": "object", "properties": {
         "aid": {"type": "string"},
         "option": {"type": "string", "description": "For a dropdown, the option text to select. For a radio/checkbox, omit or pass the label."}},
         "required": ["aid"]}},
    {"name": "click", "description": "Click a button or link — e.g. 'Apply for this job', 'Next', 'Continue', 'Add', to reveal or advance the form. Never click a final 'Submit'/'Send application'.",
     "input_schema": {"type": "object", "properties": {"aid": {"type": "string"}}, "required": ["aid"]}},
    {"name": "upload_resume", "description": "Attach the applicant's resume to a file-upload field (use only on a resume/CV field, never a cover letter).",
     "input_schema": {"type": "object", "properties": {"aid": {"type": "string"}}, "required": ["aid"]}},
    {"name": "scroll", "description": "Scroll to reveal more of the page when the form continues below the fold.",
     "input_schema": {"type": "object", "properties": {
         "direction": {"type": "string", "enum": ["down", "up"]}}, "required": ["direction"]}},
    {"name": "ready_for_review", "description": "Call this when the application form is fully filled and the only step left is the final submit. Hands off to the human to review and submit — do NOT submit yourself.",
     "input_schema": {"type": "object", "properties": {
         "summary": {"type": "string", "description": "One line on what you filled."}},
         "required": ["summary"]}},
    {"name": "blocked", "description": "Stop because something you cannot or must not do is in the way: a login wall, a captcha, a payment, or no application form on the page.",
     "input_schema": {"type": "object", "properties": {
         "reason": {"type": "string"}}, "required": ["reason"]}},
]

_SYSTEM = """You help fill out a job application form in a real browser. A separate \
deterministic step has ALREADY auto-filled the easy fields before your turn — basic \
identity text fields (name, email, phone, links, location…), native dropdowns, and \
the resume upload. Don't redo that work.

You handle only what code can't: advancing through "Apply"/"Next"/"Continue" steps, \
writing the free-text / "why do you want to work here" answers, picking Yes/No radio \
options, and any field too ambiguous for the rules. Each turn you get the page's \
remaining interactive elements (with stable `aid`s); take ONE action via a tool.

When everything that needs a decision is handled, call `ready_for_review`.

Hard rules:
- NEVER submit. Don't click "Submit"/"Send application". Call `ready_for_review` — a \
human approves and submits.
- NEVER fill demographic / EEO / diversity questions (gender, race, ethnicity, \
veteran, disability, sexual orientation). They are the human's to answer.
- Login wall, captcha, payment, or no application form on the page → call `blocked`. \
Never try to solve a captcha.
- Use the drafted answers for free-text questions; the identity values for facts.
- `scroll` down when the page continues below what you can see.

Applicant profile (use these exact values):
"""


def _profile_text(identity: dict, questions: list[dict]) -> str:
    lines = ["IDENTITY:"]
    for k, v in (identity or {}).items():
        if v:
            lines.append(f"  {k}: {v}")
    if questions:
        lines.append("\nDRAFTED ANSWERS (match to the closest question on the form):")
        for q in questions:
            lines.append(f"  Q: {q.get('question','')}")
            lines.append(f"  A: {q.get('answer','')}")
    return "\n".join(lines)


def snapshot(page) -> tuple[list[dict], list]:
    """(elements, frames) — interactive elements across all frames, each tagged with
    a globally-unique aid (``f{frame_index}_{n}``). The parallel ``frames`` list lets
    the action layer resolve an aid back to the frame it lives in."""
    elements, frames = [], []
    for fi, fr in enumerate(page.frames):
        try:
            recs = fr.evaluate(_SNAPSHOT_JS, f"f{fi}_")
        except Exception:  # noqa: BLE001 — a cross-origin/detached frame is just skipped
            continue
        if recs:
            frames.append(fr)
            elements.extend(recs)
    return elements, page.frames


def _frame_for(aid: str, frames: list):
    """Resolve an aid (``f{idx}_{n}``) to its frame."""
    try:
        idx = int(aid.split("_", 1)[0][1:])
        return frames[idx]
    except (ValueError, IndexError):
        return None


def act(page, frames, name: str, inp: dict, resume: dict | None,
        label_of) -> tuple[str, bool, dict | None]:
    """Execute one agent tool against the browser. Returns
    ``(result_text, done, outcome)`` — ``done`` ends the loop; ``outcome`` is the
    terminal payload for ``ready_for_review`` / ``blocked`` (else None).

    ``label_of(aid)`` maps an aid to its snapshot label (for the preview + the EEO
    guard); injected so this stays unit-testable without a real page."""
    if name == "ready_for_review":
        return "ok", True, {"status": "ready", "summary": inp.get("summary", "")}
    if name == "blocked":
        return "ok", True, {"status": "blocked", "reason": inp.get("reason", "")}
    if name == "scroll":
        page.mouse.wheel(0, -700 if inp.get("direction") == "up" else 700)
        page.wait_for_timeout(400)
        return "scrolled", False, None

    aid = inp.get("aid", "")
    label = label_of(aid) or ""
    # Belt-and-suspenders EEO guard: even if the agent is told not to, never let it
    # fill a demographic field. fieldmatch.match_key returns None for those.
    if name in ("fill", "choose") and label and fieldmatch.match_key(label) is None \
            and _is_eeo(label):
        return "skipped: demographic/EEO field, left for the human", False, None

    fr = _frame_for(aid, frames)
    if fr is None:
        return f"error: no element {aid}", False, None
    sel = f'[data-agent-id="{aid}"]'
    try:
        if name == "fill":
            fr.fill(sel, str(inp.get("text", "")))
            return "filled", False, None
        if name == "choose":
            opt = inp.get("option")
            if opt:
                try:
                    fr.select_option(sel, label=opt)
                except Exception:  # not a <select> — treat as a radio/checkbox click
                    fr.check(sel)
            else:
                fr.check(sel)
            return "chosen", False, None
        if name == "click":
            fr.click(sel)
            page.wait_for_timeout(1200)   # let navigation / reveal settle
            return "clicked", False, None
        if name == "upload_resume":
            if not resume:
                return "no resume available to attach", False, None
            fr.set_input_files(sel, files=[resume])
            return "resume attached", False, None
    except Exception as e:  # noqa: BLE001 — one bad action never aborts the run
        return f"error: {e.__class__.__name__}", False, None
    return f"error: unknown tool {name}", False, None


def _is_eeo(label: str) -> bool:
    import re
    return bool(re.search(
        r"gender|sex\b|race|ethnic|hispanic|latino|veteran|disab|sexual orientation",
        label, re.I))


def auto_fill(page, frames, elements: list[dict], identity: dict,
              resume: dict | None, attempted: set) -> list[dict]:
    """Deterministic pass — fill everything code can decide *without* an LLM: identity
    text fields, native dropdowns (option matched to an identity value), and the
    resume upload. Returns what it filled. ``attempted`` (per-field, persisted across
    steps) guarantees we never act on the same field twice, so the pass terminates."""
    acted = []
    for e in elements:
        aid, label, kind = e.get("aid", ""), e.get("label", ""), e.get("kind")
        memo = (aid.split("_", 1)[0], label)   # (frame, label) — stable across snapshots
        if memo in attempted:
            continue
        target = do = None
        if kind == "text":
            key = fieldmatch.match_key(label)
            if key and identity.get(key) and e.get("value", "") != str(identity[key]):
                target, do = str(identity[key]), "fill"
        elif kind == "select":
            key = fieldmatch.match_key(label)
            if key and identity.get(key):
                opt = fieldmatch.select_value(e.get("options", []), identity[key])
                if opt and e.get("value", "") != opt:
                    target, do = opt, "select"
        elif kind == "file":
            if resume and fieldmatch.is_resume_field(label):
                do = "upload"
        if not do:
            continue
        attempted.add(memo)
        fr = _frame_for(aid, frames)
        if fr is None:
            continue
        sel = f'[data-agent-id="{aid}"]'
        try:
            if do == "fill":
                fr.fill(sel, target); acted.append({"label": label, "value": target})
            elif do == "select":
                fr.select_option(sel, label=target)
                acted.append({"label": label, "value": target})
            elif do == "upload":
                fr.set_input_files(sel, files=[resume])
                acted.append({"label": label or "Resume", "value": resume["name"]})
        except Exception:  # noqa: BLE001 — a stubborn field falls through to the LLM
            pass
    return acted


def _needs_llm(e: dict) -> bool:
    """Trim already-satisfied identity/select fields from what the LLM sees, so it
    only spends tokens on navigation, essays, radios, and ambiguous fields."""
    if e.get("kind") in ("text", "select") and e.get("value"):
        return False   # already has a value (auto-filled or pre-filled)
    return True


def run_agent(page, job: dict, client) -> dict:
    """Drive the form with the LLM agent. Returns a preview dict compatible with the
    worker's preview/approve flow: ``{filled, skipped, screenshot_url, status,
    summary|reason}``. ``client`` is an Anthropic client (injected for testability)."""
    identity = job.get("identity", {})
    questions = job.get("questions", [])
    resume = job.get("resume")
    system = [
        {"type": "text", "text": _SYSTEM + _profile_text(identity, questions),
         "cache_control": {"type": "ephemeral"}},  # cache the stable profile
    ]
    page.goto(job["url"], wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(1500)

    messages: list = []
    filled, skipped = [], []
    attempted: set = set()
    outcome = {"status": "incomplete"}

    for step in range(MAX_STEPS):
        elements, frames = snapshot(page)

        # 1) Deterministic pass — free, no LLM. Loop while it keeps making progress.
        auto = auto_fill(page, frames, elements, identity, resume, attempted)
        if auto:
            filled.extend(auto)
            print(f"[agent] step {step}: auto-filled {len(auto)} field(s) (no LLM)")
            continue

        # 2) Only the parts that need reasoning go to the LLM.
        labels = {e["aid"]: e["label"] for e in elements}
        remaining = [e for e in elements if _needs_llm(e)]
        user_block = (
            f"URL: {page.url}\n"
            "Identity fields and dropdowns have been auto-filled by code. "
            "Elements still needing a decision (navigation, free-text answers, "
            "Yes/No radios, ambiguous fields):\n"
            f"{json.dumps(remaining, ensure_ascii=False)}\n\n"
            "Take the single next action, or call ready_for_review if nothing is left."
        )
        messages.append({"role": "user", "content": user_block})
        resp = client.messages.create(
            model=MODEL, max_tokens=1024, system=system, tools=TOOLS,
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            thinking={"type": "adaptive"}, messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})
        calls = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        if not calls:
            break   # the model didn't act — stop rather than spin

        tc = calls[0]
        result, done, out = act(page, frames, tc.name, tc.input or {}, resume,
                                labels.get)
        print(f"[agent] step {step}: {tc.name}({json.dumps(tc.input)}) -> {result}")
        if tc.name in ("fill", "choose", "upload_resume") and not result.startswith(("error", "skipped", "no resume")):
            v = tc.input.get("text") or tc.input.get("option") or "✓"
            filled.append({"label": labels.get(tc.input.get("aid", ""), tc.name),
                           "value": str(v)[:60]})
        elif result.startswith("skipped"):
            skipped.append(labels.get(tc.input.get("aid", ""), ""))
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tc.id, "content": result}]})
        if done:
            outcome = out
            break

    preview = {"filled": filled, "skipped": [s for s in skipped if s][:20],
               "screenshot_url": _shot(page), "status": outcome.get("status")}
    if outcome.get("status") == "blocked":
        preview["reason"] = outcome.get("reason", "")
    if outcome.get("status") == "ready":
        preview["summary"] = outcome.get("summary", "")
    return preview


def _shot(page) -> str | None:
    import base64
    try:
        png = page.screenshot(full_page=True, type="jpeg", quality=55)
    except Exception:  # noqa: BLE001
        return None
    return "data:image/jpeg;base64," + base64.b64encode(png).decode()
