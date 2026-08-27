"""DOM-based application-form detection — shared brain for worker, iOS, extension.

Host allowlists (Greenhouse/Lever/Ashby) are a confidence boost, not a hard gate.
This module scores the *page* as an application form, login wall, captcha challenge,
or unknown — so autopilot can fill/advance or soft-pause for the human.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from . import fieldmatch

# Buttons that advance a multi-step application (never final submit).
_ADVANCE = re.compile(
    r"^\s*(next|continue|save\s*&\s*continue|review(\s+application)?|"
    r"proceed|forward|keep going)\s*$|"
    r"\b(next step|continue to|go to next)\b",
    re.I,
)
_SUBMIT = re.compile(
    r"submit(\s+application)?|send\s+application|finish\s+application|"
    r"complete\s+application|apply\s+now$|^\s*apply\s*$",
    re.I,
)
_NOT_ADVANCE = re.compile(
    r"submit|send application|finish|complete application|cancel|back|"
    r"previous|save(?!\s*&\s*continue)|add another|upload|sign\s*in|log\s*in|"
    r"create account|register",
    re.I,
)
_APPLY_REVEAL = re.compile(
    r"apply\s+for\s+this\s+job|apply\s+now|^\s*apply\s*$|start\s+application|"
    r"begin\s+application",
    re.I,
)
_LOGIN = re.compile(
    r"sign\s*in|log\s*in|create\s+account|register|password|forgot\s+password",
    re.I,
)
# Identity keys that strongly signal an application form.
_SIGNAL_KEYS = frozenset({
    "email", "first_name", "last_name", "full_name", "phone", "linkedin",
    "resume", "work_authorized", "needs_sponsorship",
})


def probe_signals(
    *,
    labels: list[str],
    button_texts: list[str],
    has_password: bool = False,
    has_file: bool = False,
    captcha_hit: bool = False,
    known_ats: bool = False,
) -> dict[str, Any]:
    """Score a page from extracted signals (no DOM). Used by tests + Python callers."""
    if captcha_hit:
        return {
            "kind": "captcha",
            "score": 0,
            "fillable_count": 0,
            "matched_keys": [],
            "advance_label": None,
            "submit_visible": False,
            "reveal_label": None,
            "blocker_reason": "CAPTCHA or bot check in the way",
        }

    matched: list[str] = []
    for label in labels:
        if fieldmatch.is_eeo(label):
            continue
        key = fieldmatch.match_key(label)
        if key and key not in matched:
            matched.append(key)
        elif has_file and fieldmatch.is_resume_field(label) and "resume" not in matched:
            matched.append("resume")

    if has_file and "resume" not in matched:
        # Bare file input without a clear label still counts lightly.
        matched.append("resume")

    fillable = len(matched)
    signal = sum(1 for k in matched if k in _SIGNAL_KEYS)

    advance_label = None
    submit_visible = False
    reveal_label = None
    loginish = has_password
    for text in button_texts:
        t = (text or "").strip()
        if not t:
            continue
        if _LOGIN.search(t):
            loginish = True
        if is_submit_text(t):
            submit_visible = True
        if is_advance_text(t):
            advance_label = advance_label or t
        if _APPLY_REVEAL.search(t) and not re.search(r"submit|send", t, re.I):
            reveal_label = reveal_label or t

    # Login wall: password field + sign-in chrome, few application signals.
    if loginish and signal < 2 and not submit_visible:
        return {
            "kind": "login",
            "score": 0,
            "fillable_count": fillable,
            "matched_keys": matched,
            "advance_label": advance_label,
            "submit_visible": submit_visible,
            "reveal_label": reveal_label,
            "blocker_reason": "Login or account wall",
        }

    score = fillable * 2 + signal * 3
    if submit_visible:
        score += 4
    if advance_label:
        score += 2
    if known_ats:
        score += 5
    if reveal_label and fillable == 0:
        score += 1  # JD page with Apply — not fillable yet

    if fillable >= 2 or signal >= 2 or (known_ats and fillable >= 1):
        kind = "application"
    elif reveal_label and fillable == 0:
        kind = "unknown"  # description page — caller should reveal
    elif score >= 4:
        kind = "application"
    else:
        kind = "unknown"

    return {
        "kind": kind,
        "score": score,
        "fillable_count": fillable,
        "matched_keys": matched,
        "advance_label": advance_label,
        "submit_visible": submit_visible,
        "reveal_label": reveal_label,
        "blocker_reason": None,
    }


def is_advance_text(text: str | None) -> bool:
    t = (text or "").strip()
    if not t or _NOT_ADVANCE.search(t):
        return False
    return bool(_ADVANCE.search(t))


def is_submit_text(text: str | None) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if is_advance_text(t):
        return False
    return bool(re.search(r"submit|send application|finish application|complete application", t, re.I))


def payload() -> dict:
    """Frozen probe helpers for clients (patterns + version)."""
    body = {
        "advance": _ADVANCE.pattern,
        "submit": _SUBMIT.pattern,
        "not_advance": _NOT_ADVANCE.pattern,
        "apply_reveal": _APPLY_REVEAL.pattern,
        "login": _LOGIN.pattern,
        "signal_keys": sorted(_SIGNAL_KEYS),
        "flags": "i",
    }
    blob = json.dumps(body, sort_keys=True).encode()
    body["version"] = hashlib.sha256(blob).hexdigest()[:12]
    return body


# Injected on iOS / extension / Playwright. Posts nothing by itself — callers
# invoke window.__applyFormProbe() and window.__applyDrive(opts).
PROBE_AND_DRIVE_JS = r"""
(() => {
  if (window.__applyFormProbe) return;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const ACCENT = "#5B7C6E";
  const vis = (el) => !!(el && (el.offsetParent || el.getClientRects().length) && !el.disabled);

  const ADVANCE_RE = /^\s*(next|continue|save\s*&\s*continue|review(\s+application)?|proceed|forward|keep going)\s*$|\b(next step|continue to|go to next)\b/i;
  const NOT_ADVANCE_RE = /submit|send application|finish|complete application|cancel|back|previous|save(?!\s*&\s*continue)|add another|upload|sign\s*in|log\s*in|create account|register/i;
  const SUBMIT_RE = /submit(\s+application)?|send\s+application|finish\s+application|complete\s+application/i;
  const REVEAL_RE = /apply\s+for\s+this\s+job|apply\s+now|^\s*apply\s*$|start\s+application|begin\s+application/i;
  const LOGIN_RE = /sign\s*in|log\s*in|create\s+account|register|forgot\s+password/i;
  const CAPTCHA_RE = /recaptcha|hcaptcha|captcha|cf-turnstile|challenge-platform|px-captcha|funcaptcha|arkose/i;
  const SIGNAL = new Set(["email","first_name","last_name","full_name","phone","linkedin","work_authorized","needs_sponsorship"]);

  function btnText(el) {
    return (el.innerText || el.textContent || el.getAttribute("aria-label") || el.value || "")
      .replace(/\s+/g, " ").trim().slice(0, 80);
  }

  function detectCaptcha() {
    const nodes = document.querySelectorAll("iframe, div, #captcha, [class*='captcha'], [id*='captcha']");
    for (const n of nodes) {
      const sig = ((n.src || "") + " " + (n.id || "") + " " + (n.className || "") + " " + (n.title || "")).toLowerCase();
      if (CAPTCHA_RE.test(sig)) return true;
    }
    if (document.querySelector("[data-sitekey], .g-recaptcha, .h-captcha, .cf-turnstile")) return true;
    return false;
  }

  function ensureMagicStyle() {
    if (document.getElementById("__apply_magic_css")) return;
    const s = document.createElement("style");
    s.id = "__apply_magic_css";
    s.textContent = `
      @keyframes __applyPulse {
        0% { box-shadow: 0 0 0 0 rgba(91,124,110,0.45); }
        70% { box-shadow: 0 0 0 10px rgba(91,124,110,0); }
        100% { box-shadow: 0 0 0 0 rgba(91,124,110,0); }
      }
      @keyframes __applyShimmer {
        0% { background-position: 0% 50%; opacity: 0.0; }
        40% { opacity: 0.55; }
        100% { background-position: 100% 50%; opacity: 0; }
      }
      .__apply_flash {
        outline: 2px solid ${ACCENT} !important;
        outline-offset: 2px;
        animation: __applyPulse 0.85s ease-out;
        transition: outline-color 0.3s ease;
      }
      .__apply_step_veil {
        pointer-events: none;
        position: fixed; inset: 0; z-index: 2147483646;
        background: linear-gradient(110deg, transparent 30%, rgba(91,124,110,0.12) 50%, transparent 70%);
        background-size: 200% 100%;
        animation: __applyShimmer 0.9s ease-out;
      }
    `;
    document.documentElement.appendChild(s);
  }

  function magicFlash(el) {
    try {
      ensureMagicStyle();
      el.classList.add("__apply_flash");
      setTimeout(() => el.classList.remove("__apply_flash"), 900);
    } catch (e) {}
  }

  function stepVeil() {
    try {
      ensureMagicStyle();
      const v = document.createElement("div");
      v.className = "__apply_step_veil";
      document.documentElement.appendChild(v);
      setTimeout(() => v.remove(), 950);
    } catch (e) {}
  }

  // Prefer host page's matchKey from Autofill if present; else a tiny fallback.
  function matchKeySafe(label) {
    if (typeof matchKey === "function") return matchKey(label);
    const rules = (window.__APPLY && window.__APPLY.rules && window.__APPLY.rules.rules) || [];
    const flags = (window.__APPLY && window.__APPLY.rules && window.__APPLY.rules.flags) || "i";
    for (const [k, p] of rules) {
      try { if (new RegExp(p, flags).test(label)) return k; } catch (e) {}
    }
    return null;
  }

  function fieldLabelSafe(el) {
    if (typeof fieldLabel === "function") return fieldLabel(el);
    const bits = [];
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) bits.push(l.textContent);
    }
    const w = el.closest("label"); if (w) bits.push(w.textContent);
    if (el.getAttribute("aria-label")) bits.push(el.getAttribute("aria-label"));
    if (el.placeholder) bits.push(el.placeholder);
    bits.push(el.name || "", el.id || "");
    return bits.join(" ").replace(/\s+/g, " ").trim().toLowerCase();
  }

  window.__applyFormProbe = function () {
    const labels = [];
    let hasPassword = false, hasFile = false;
    for (const el of document.querySelectorAll("input, textarea, select")) {
      if (!vis(el)) continue;
      const t = (el.type || "").toLowerCase();
      if (t === "hidden" || t === "submit" || t === "button" || t === "image" || t === "reset") continue;
      if (t === "password") { hasPassword = true; continue; }
      if (t === "file") { hasFile = true; }
      labels.push(fieldLabelSafe(el));
    }
    const buttons = [];
    for (const el of document.querySelectorAll("button, a[href], [role=button], input[type=submit], input[type=button]")) {
      if (!vis(el)) continue;
      const t = btnText(el);
      if (t) buttons.push(t);
    }

    if (detectCaptcha()) {
      return {
        kind: "captcha", score: 0, fillableCount: 0, matchedKeys: [],
        advanceLabel: null, submitVisible: false, revealLabel: null,
        blockerReason: "CAPTCHA or bot check in the way",
      };
    }

    const matched = [];
    for (const label of labels) {
      const key = matchKeySafe(label);
      if (key && !matched.includes(key)) matched.push(key);
    }
    if (hasFile && !matched.includes("resume")) matched.push("resume");

    let advanceLabel = null, submitVisible = false, revealLabel = null, loginish = hasPassword;
    for (const t of buttons) {
      if (LOGIN_RE.test(t)) loginish = true;
      if (SUBMIT_RE.test(t)) submitVisible = true;
      if (ADVANCE_RE.test(t) && !NOT_ADVANCE_RE.test(t)) advanceLabel = advanceLabel || t;
      if (REVEAL_RE.test(t) && !SUBMIT_RE.test(t)) revealLabel = revealLabel || t;
    }

    const fillable = matched.length;
    const signal = matched.filter((k) => SIGNAL.has(k)).length;
    if (loginish && signal < 2 && !submitVisible) {
      return {
        kind: "login", score: 0, fillableCount: fillable, matchedKeys: matched,
        advanceLabel, submitVisible, revealLabel,
        blockerReason: "Login or account wall",
      };
    }

    let score = fillable * 2 + signal * 3;
    if (submitVisible) score += 4;
    if (advanceLabel) score += 2;
    if (revealLabel && fillable === 0) score += 1;

    let kind = "unknown";
    if (fillable >= 2 || signal >= 2) kind = "application";
    else if (score >= 4) kind = "application";
    else if (revealLabel && fillable === 0) kind = "unknown";

    return {
      kind, score, fillableCount: fillable, matchedKeys: matched,
      advanceLabel, submitVisible, revealLabel, blockerReason: null,
    };
  };

  function findAdvanceEl() {
    const cands = [...document.querySelectorAll("button, a[href], [role=button], input[type=button]")];
    for (const el of cands) {
      if (!vis(el)) continue;
      const t = btnText(el);
      if (!t || NOT_ADVANCE_RE.test(t)) continue;
      if (ADVANCE_RE.test(t)) return el;
    }
    return null;
  }

  function findRevealEl() {
    const cands = [...document.querySelectorAll("button, a[href], [role=button]")];
    for (const el of cands) {
      if (!vis(el)) continue;
      const t = btnText(el);
      if (!t || SUBMIT_RE.test(t)) continue;
      if (REVEAL_RE.test(t)) return el;
    }
    return null;
  }

  function post(msg) {
    try {
      if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.applyfill) {
        window.webkit.messageHandlers.applyfill.postMessage(msg);
      }
    } catch (e) {}
    try {
      window.dispatchEvent(new CustomEvent("applyfill", { detail: msg }));
    } catch (e) {}
  }

  // Autopilot control flag on window — Pause sets this false.
  window.__applyDriveControl = window.__applyDriveControl || { running: false, paused: false };

  window.__applyDrive = async function (opts) {
    opts = opts || {};
    const maxSteps = opts.maxSteps || 8;
    const ctrl = window.__applyDriveControl;
    if (opts.mode === "pause") {
      ctrl.paused = true;
      ctrl.running = false;
      post({ status: "paused", filled: 0, essays: 0, step: 0 });
      return { status: "paused" };
    }
    if (opts.mode === "probe") {
      const p = window.__applyFormProbe();
      post({ status: "probe", probe: p, filled: 0, essays: 0, step: 0 });
      return p;
    }

    ctrl.paused = false;
    ctrl.running = true;
    let step = 0;
    let totalFilled = 0;
    let totalEssays = 0;

    // Upgrade flash used by Autofill if present.
    const prevFlash = typeof flash === "function" ? flash : null;
    try {
      if (typeof flash === "function") {
        // can't reassign const; wrap via prototype — just call magicFlash after fills in drive
      }
    } catch (e) {}

    while (ctrl.running && !ctrl.paused && step < maxSteps) {
      let probe = window.__applyFormProbe();

      if (probe.kind === "captcha" || probe.kind === "login") {
        post({
          status: "needsHuman",
          blocker: probe.blockerReason || probe.kind,
          probe, filled: totalFilled, essays: totalEssays, step,
        });
        // Watch until clear or paused/cancelled.
        while (ctrl.running && !ctrl.paused) {
          await sleep(1500);
          probe = window.__applyFormProbe();
          if (probe.kind !== "captcha" && probe.kind !== "login") {
            post({
              status: "watchingClear",
              probe, filled: totalFilled, essays: totalEssays, step,
              blocker: null,
            });
            // Wait for explicit resume (mode resume re-enters __applyDrive).
            ctrl.running = false;
            return { status: "watchingClear", probe };
          }
          post({
            status: "needsHuman",
            blocker: probe.blockerReason || probe.kind,
            probe, filled: totalFilled, essays: totalEssays, step,
          });
        }
        return { status: ctrl.paused ? "paused" : "needsHuman", probe };
      }

      if (probe.kind === "unknown" && probe.fillableCount === 0 && probe.revealLabel) {
        const rev = findRevealEl();
        if (rev) {
          post({ status: "advancing", step, filled: totalFilled, essays: totalEssays, detail: "reveal" });
          stepVeil();
          rev.click();
          await sleep(1600);
          step++;
          continue;
        }
      }

      if (probe.kind === "unknown" && probe.fillableCount === 0 && !probe.revealLabel) {
        post({ status: "failed", blocker: "No application form on this page",
               filled: totalFilled, essays: totalEssays, step, probe });
        ctrl.running = false;
        return { status: "failed", probe };
      }

      post({ status: "filling", step, filled: totalFilled, essays: totalEssays, probe });
      let filled = 0, essays = 0;
      if (typeof window.__applyAutofill === "function") {
        // Run core fill; patch flash via temporary override on elements after
        const before = Date.now();
        filled = await window.__applyAutofill();
        // __applyAutofill posts its own message; count from return value
        if (typeof filled !== "number") filled = 0;
      }
      // Re-flash visible filled-looking inputs lightly
      for (const el of document.querySelectorAll("input, textarea, select")) {
        if (!vis(el)) continue;
        const t = (el.type || "").toLowerCase();
        if (["hidden","submit","button","file","password"].includes(t)) continue;
        if ((el.value || "").trim()) magicFlash(el);
      }
      totalFilled += filled || 0;

      probe = window.__applyFormProbe();
      if (probe.submitVisible && !probe.advanceLabel) {
        post({ status: "ready", filled: totalFilled, essays: totalEssays, step, probe });
        ctrl.running = false;
        return { status: "ready", probe, filled: totalFilled };
      }

      const adv = findAdvanceEl();
      if (!adv) {
        post({ status: "ready", filled: totalFilled, essays: totalEssays, step, probe,
               detail: "no advance control" });
        ctrl.running = false;
        return { status: "ready", probe, filled: totalFilled };
      }

      post({ status: "advancing", step, filled: totalFilled, essays: totalEssays,
             detail: btnText(adv) });
      stepVeil();
      magicFlash(adv);
      await sleep(280);
      adv.click();
      await sleep(1400);
      step++;
    }

    ctrl.running = false;
    if (ctrl.paused) {
      post({ status: "paused", filled: totalFilled, essays: totalEssays, step });
      return { status: "paused" };
    }
    const probe = window.__applyFormProbe();
    post({ status: "ready", filled: totalFilled, essays: totalEssays, step, probe });
    return { status: "ready", probe, filled: totalFilled };
  };
})();
"""


def inject_js() -> str:
    """Full probe+drive script for Playwright ``page.add_init_script`` / evaluate."""
    return PROBE_AND_DRIVE_JS
