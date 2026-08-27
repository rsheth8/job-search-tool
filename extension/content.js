/* Job Apply Autofill — content script.
 *
 * Two ways to fill an application:
 *   1. Per-field — focus a field and accept the inline chip (a known fact, or a
 *      drafted answer for free-text questions).
 *   2. One click — the floating "⚡ Autofill this page" button fills everything it
 *      recognizes at once (text, dropdowns, and Yes/No questions), Simplify-style,
 *      and tells you how many questions still need you.
 *
 * Nothing is ever submitted. Config (server / user / token / posting id) lives in
 * the extension options.
 */
(() => {
  "use strict";

  // identity key -> regex that identifies a field from its label text.
  // Order matters: more specific patterns first (email before address, preferred
  // before first, location before city).
  // Offline fallback, generated from app/fieldmatch.py (rules version
  // db530a7f5e7c). The rules fetched from the backend take priority —
  // don't hand-edit these; regenerate them from fieldmatch.py, or you have
  // reintroduced exactly the drift this design removes.
  const FALLBACK_RULES = [
    ["email", /e-?mail/i],
    ["preferred_name", /preferred (first )?name|nick.?name|known as|goes by/i],
    ["first_name", /first.?name|given.?name|legal first|name\s*\(?\s*first/i],
    ["last_name", /last.?name|family.?name|surname|name\s*\(?\s*last/i],
    ["full_name", /full.?name|^\s*name\s*$|your name|legal name/i],
    ["pronouns", /pronouns/i],
    ["phone", /phone|mobile|tel(ephone)?|contact number/i],
    ["linkedin", /linked.?in/i],
    ["github", /git.?hub/i],
    ["portfolio", /portfolio|personal (web)?site|^\s*website\s*$|^url$|other url|personal url|home ?page|personal page/i],
    ["location", /\blocation\b|where are you (based|located)|city.{0,5}state|where do you (live|reside)|currently (based|located|reside)|based in/i],
    ["address", /street address|address line|mailing address|home address|^\s*address\b/i],
    ["city", /\bcity\b|town/i],
    ["state", /\bstate\b|province|region/i],
    ["zip", /\bzip\b|postal code|post.?code/i],
    ["country", /\bcountry\b|nation/i],
    ["school", /school|university|college|institution|alma mater|where did you study/i],
    ["degree", /degree|qualification|level of (education|study)/i],
    ["discipline", /major|discipline|field of study|concentration/i],
    ["gpa", /\bgpa\b|grade point/i],
    ["grad_year", /grad(uation)?.{0,8}(year|date)|class of|completion (year|date)|year of grad/i],
    ["current_company", /current (employer|company)|present (employer|company)|where do you (currently )?work/i],
    ["current_title", /current (title|role|position)|present (title|role|position)/i],
    ["years_experience", /years.{0,10}experience|experience.{0,10}years|\byoe\b|how many years/i],
    ["salary_expectation", /salary (expectation|requirement)|expected (salary|compensation|pay)|desired (salary|pay|compensation)|compensation expectation|pay expectation/i],
    ["start_date", /start date|available to start|earliest (start|availability)|when (can|could) you start|date available|notice period|availability date/i],
    ["willing_to_relocate", /willing to relocate|open to relocat|able to relocate|relocat/i],
    ["work_authorized", /authori[sz]ed to work|work authori[sz]ation|legally.{0,12}work|eligible to work|right to work/i],
    ["needs_sponsorship", /sponsor(ship)?|require.{0,12}visa|visa.{0,12}status|immigration status/i],
    ["background_check", /background check|criminal (background|history|record)|background screening/i],
    ["drug_test", /drug (test|screen|screening)|substance (test|screen)/i],
    ["over_18", /over 18|18 years|at least 18|age 18|legal age/i],
    ["can_travel", /willing to travel|able to travel|travel (required|for (work|this))|open to travel/i],
    ["previously_applied", /previously applied|applied (here|before|to (this|us))|worked (here|for us|at this)|former employee|prior application/i],
    ["related_to_employee", /related to|relative (at|of)|know anyone|family member|referral|employee of/i],
    ["work_arrangement", /remote|hybrid|on-?site|onsite|work (from home|arrangement|location preference)/i],
    ["how_heard", /how did you (hear|learn|find)|where did you hear|referral source|source of (this )?application/i],
    ["gender", /^\s*gender\s*$|^\s*sex\s*$/i],
    ["race", /^\s*race\s*$|race\s*\/?\s*ethnicity|racial identity/i],
    ["ethnicity", /^\s*ethnicity\s*$|ethnic background/i],
    ["veteran_status", /veteran|protected veteran|military status/i],
    ["disability_status", /disabilit(y|ies)|disabled/i],
  ];
  // Labels we never auto-fill or offer to draft (hard-blocked EEO).
  const FALLBACK_EEO = /sexual orientation|pronoun.{0,4}optional|national origin|self.?identif|\beeo\b|equal (employment|opportunity)|protected (class|category)|lgbt|marital status|religio|citizenship status|date of birth|\bdob\b|transgender|lgbtq|hispanic|latino|gender identity/i;

  // Replaced by the served rules once init() fetches them.
  let RULES = FALLBACK_RULES;
  let EEO = FALLBACK_EEO;
  let RULES_SRC = "bundled";

  let CFG = null;
  let IDENTITY = {};
  let chip = null;
  let chipFor = null;

  async function init() {
    CFG = await getConfig();
    if (!CFG.baseUrl || !CFG.user) {
      console.info("[apply-autofill] not configured — open the extension options.");
      return;
    }
    try {
      const r = await api("GET", `/apply/identity?user=${encodeURIComponent(CFG.user)}`);
      IDENTITY = (await r.json()).fields || {};
    } catch (e) {
      console.warn("[apply-autofill] could not load identity:", e);
    }
    await loadRules();
    document.addEventListener("focusin", onFocus, true);
    document.addEventListener("scroll", reposition, true);
    window.addEventListener("resize", reposition, true);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideChip(); });
    addAutofillButton();
  }

  // ---- per-field chips ----------------------------------------------------

  function onFocus(e) {
    const el = e.target;
    const tag = (el.tagName || "").toLowerCase();
    if (tag === "select") {
      const key = matchKey(fieldLabel(el));
      const val = key && IDENTITY[key];
      if (val != null && val !== "" && selectOptionFor(el, val)) {
        showChip(el, `Set: ${truncate(val, 30)}`, () => { setSelect(el, val); hideChip(); });
      } else hideChip();
      return;
    }
    if (!isTextField(el)) return hideChip();
    const key = matchKey(fieldLabel(el));
    if (key && IDENTITY[key] != null && IDENTITY[key] !== "") {
      showChip(el, `Fill: ${truncate(IDENTITY[key], 40)}`, () => fill(el, IDENTITY[key]));
    } else if (tag === "textarea" && !isEeo(fieldLabel(el))) {
      // Only textareas get a drafted answer — short <input>s are facts, not essays.
      // Demographic questions never get one, however essay-shaped they look.
      showChip(el, "✨ Draft answer", () => draftAnswer(el, fieldLabel(el)));
    } else {
      hideChip();
    }
  }

  // ---- one-click "autofill this page" -------------------------------------

  function bulkAutofill() {
    let filled = 0;
    let essays = 0;
    for (const el of document.querySelectorAll("input, textarea, select")) {
      if (!isVisible(el) || el.disabled || el.readOnly) continue;
      const tag = el.tagName.toLowerCase();
      const type = (el.type || "").toLowerCase();
      if (type === "radio" || type === "checkbox" || type === "file" ||
          type === "hidden" || type === "submit" || type === "button") continue;
      if (tag === "textarea") {
        if (!el.value.trim() && !isEeo(fieldLabel(el))) essays++;
        continue;
      }
      const key = matchKey(fieldLabel(el));
      const val = key && IDENTITY[key];
      if (val == null || val === "") continue;
      if (tag === "select" ? setSelect(el, val) : (setText(el, val), true)) filled++;
    }
    filled += fillRadioGroups();
    const more = essays ? ` · ${essays} question${essays === 1 ? "" : "s"} need you` : "";
    // Naming the rule set makes a stale "bundled" run visible rather than invisible.
    const src = RULES_SRC === "bundled" ? " · bundled rules" : "";
    toast(`Filled ${filled} field${filled === 1 ? "" : "s"}${more}${src}`);
  }

  // Yes/No (and similar) questions rendered as radio groups.
  function fillRadioGroups() {
    const groups = {};
    for (const r of document.querySelectorAll('input[type="radio"]')) {
      if (!isVisible(r) || r.disabled || !r.name) continue;
      (groups[r.name] = groups[r.name] || []).push(r);
    }
    let n = 0;
    for (const name in groups) {
      const radios = groups[name];
      const key = matchKey(groupLabel(radios));
      const val = key && IDENTITY[key];
      if (val == null || val === "") continue;
      const want = String(val).toLowerCase();
      const target = radios.find((r) => {
        const t = (radioLabel(r) + " " + r.value).toLowerCase();
        return t.includes(want) || (want === "yes" && /\byes\b/.test(t)) ||
               (want === "no" && /\bno\b/.test(t));
      });
      if (target && !target.checked) {
        target.checked = true;
        target.dispatchEvent(new Event("click", { bubbles: true }));
        target.dispatchEvent(new Event("change", { bubbles: true }));
        flash(target.closest("label") || target);
        n++;
      }
    }
    return n;
  }

  // ---- value application --------------------------------------------------

  function fill(el, value) { setText(el, value); flash(el); hideChip(); }

  function setText(el, value) {
    const proto = el.tagName.toLowerCase() === "textarea"
      ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function selectOptionFor(el, value) {
    const v = String(value).trim().toLowerCase();
    return [...el.options].find((o) => {
      const t = o.text.trim().toLowerCase(), ov = o.value.trim().toLowerCase();
      if (!t) return false;
      return t === v || ov === v || t.includes(v) || v.includes(t);
    });
  }

  function setSelect(el, value) {
    const opt = selectOptionFor(el, value);
    if (!opt) return false;
    el.value = opt.value;
    el.dispatchEvent(new Event("change", { bubbles: true }));
    flash(el);
    return true;
  }

  async function draftAnswer(el, question) {
    setChipBusy("Drafting…");
    try {
      const r = await api("POST", "/apply/answer", {
        user: CFG.user, posting_id: CFG.postingId || undefined,
        question, company: guessCompany(), jd: "",
      });
      const { answer } = await r.json();
      if (answer) fill(el, answer);
    } catch (e) {
      console.warn("[apply-autofill] draft failed:", e);
      setChipBusy("Draft failed — retry?");
    }
  }

  // ---- label extraction ---------------------------------------------------

  function fieldLabel(el) {
    const bits = [];
    if (el.id) {
      const lab = document.querySelector(`label[for="${cssEscape(el.id)}"]`);
      if (lab) bits.push(lab.textContent);
    }
    const wrap = el.closest("label");
    if (wrap) bits.push(wrap.textContent);
    if (el.getAttribute("aria-label")) bits.push(el.getAttribute("aria-label"));
    const by = el.getAttribute("aria-labelledby");
    if (by) by.split(/\s+/).forEach((id) => {
      const n = document.getElementById(id);
      if (n) bits.push(n.textContent);
    });
    if (el.placeholder) bits.push(el.placeholder);
    let s = bits.join(" ").replace(/\s+/g, " ").trim().toLowerCase();
    // name/id is last-resort only — stapling them onto a real label breaks
    // anchored rules ("Gender" + id=gender never matches /^\s*gender\s*$/).
    if (s.length < 3) {
      s = [el.name || "", el.id || ""].join(" ").replace(/\s+/g, " ").trim().toLowerCase();
    }
    return s;
  }

  // Question text for a radio group: a fieldset legend, an aria-labelledby, or the
  // nearest preceding heading/label of the group's container.
  function groupLabel(radios) {
    const first = radios[0];
    const fs = first.closest("fieldset");
    if (fs) {
      const legend = fs.querySelector("legend");
      if (legend) return legend.textContent.replace(/\s+/g, " ").trim().toLowerCase();
    }
    const group = first.closest('[role="radiogroup"], [class*="question"], [class*="field"]');
    if (group) {
      const lab = group.querySelector("label, legend, .label, [class*='label']");
      if (lab) return lab.textContent.replace(/\s+/g, " ").trim().toLowerCase();
    }
    return fieldLabel(first);
  }

  function radioLabel(r) {
    if (r.id) {
      const lab = document.querySelector(`label[for="${cssEscape(r.id)}"]`);
      if (lab) return lab.textContent;
    }
    const wrap = r.closest("label");
    if (wrap) return wrap.textContent;
    return r.getAttribute("aria-label") || r.value || "";
  }

  // The field-matching rules come from the backend (app/fieldmatch.py is the one
  // source of truth for all three autofill surfaces). The bundled copy above is
  // only a fallback — it drifted behind Python once already, which is why this
  // fetch exists. A failure here is safe, never silent.
  async function loadRules() {
    try {
      const r = await api("GET", "/apply/rules");
      const served = await r.json();
      if (!served || !Array.isArray(served.rules) || !served.rules.length ||
          !served.never_fill) throw new Error("malformed rules payload");
      const flags = served.flags || "i";
      const compiled = served.rules.map(([k, pat]) => [k, new RegExp(pat, flags)]);
      const eeo = new RegExp(served.never_fill, flags);
      RULES = compiled; EEO = eeo; RULES_SRC = served.version || "served";
    } catch (e) {
      console.warn("[apply-autofill] using bundled rules (" + e.message + ")");
    }
  }

  function matchKey(label) {
    const text = (label || "").trim().toLowerCase();
    if (!text || EEO.test(text)) return null;   // demographics are the human's alone
    for (const [key, re] of RULES) if (re.test(text)) return key;
    return null;
  }

  function isEeo(label) {
    return EEO.test((label || "").trim().toLowerCase());
  }

  // ---- chip + button UI ---------------------------------------------------

  function showChip(el, text, onClick) {
    hideChip();
    chip = document.createElement("button");
    chip.className = "jaf-chip";
    chip.type = "button";
    chip.textContent = text;
    chip.addEventListener("mousedown", (ev) => { ev.preventDefault(); onClick(); });
    document.body.appendChild(chip);
    chipFor = el;
    positionChip();
  }
  function setChipBusy(t) { if (chip) chip.textContent = t; }
  function positionChip() {
    if (!chip || !chipFor) return;
    const r = chipFor.getBoundingClientRect();
    chip.style.top = `${window.scrollY + r.top - 30}px`;
    chip.style.left = `${window.scrollX + r.left}px`;
  }
  function reposition() { if (chip) positionChip(); }
  function hideChip() { if (chip) chip.remove(); chip = null; chipFor = null; }
  function flash(el) { if (!el) return; el.classList.add("jaf-filled"); setTimeout(() => el.classList.remove("jaf-filled"), 800); }

  function addAutofillButton() {
    const btn = document.createElement("button");
    btn.className = "jaf-fab";
    btn.type = "button";
    btn.textContent = "⚡ Autofill this page";
    btn.addEventListener("click", bulkAutofill);
    document.body.appendChild(btn);
  }

  let toastEl = null;
  function toast(msg) {
    if (toastEl) toastEl.remove();
    toastEl = document.createElement("div");
    toastEl.className = "jaf-toast";
    toastEl.textContent = msg;
    document.body.appendChild(toastEl);
    setTimeout(() => { if (toastEl) { toastEl.remove(); toastEl = null; } }, 4000);
  }

  // ---- helpers ------------------------------------------------------------

  function isTextField(el) {
    if (!el || !el.tagName) return false;
    const tag = el.tagName.toLowerCase();
    if (tag === "textarea") return true;
    if (tag !== "input") return false;
    const t = (el.type || "text").toLowerCase();
    return ["text", "email", "tel", "url", "search", ""].includes(t);
  }
  function isVisible(el) {
    return !!(el.offsetParent || el.getClientRects().length);
  }
  function api(method, path, body) {
    const headers = { "Content-Type": "application/json" };
    if (CFG.token) headers["X-Apply-Token"] = CFG.token;
    return fetch(CFG.baseUrl.replace(/\/$/, "") + path, {
      method, headers, body: body ? JSON.stringify(body) : undefined,
    }).then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r; });
  }
  function guessCompany() {
    const og = document.querySelector('meta[property="og:site_name"]');
    if (og && og.content) return og.content;
    return (document.title || "").split(/[-|•]/)[0].trim();
  }
  function truncate(s, n) { s = String(s); return s.length > n ? s.slice(0, n - 1) + "…" : s; }
  function getConfig() {
    return new Promise((resolve) => {
      chrome.storage.sync.get(
        { baseUrl: "", user: "", token: "", postingId: "" }, resolve);
    });
  }
  function cssEscape(s) { return (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/"/g, '\\"'); }

  // Test seam: Playwright injects __APPLY_TEST, then identity + rules, then
  // bulkAutofill. Production still boots via init() as before.
  window.__applyFieldLabel = fieldLabel;
  window.__applyBulkAutofill = bulkAutofill;
  window.__applyUseIdentity = (fields) => { IDENTITY = fields || {}; };
  window.__applyUseRules = (payload) => {
    if (!payload || !Array.isArray(payload.rules) || !payload.rules.length ||
        !payload.never_fill) {
      RULES = FALLBACK_RULES; EEO = FALLBACK_EEO; RULES_SRC = "bundled";
      return;
    }
    try {
      const flags = payload.flags || "i";
      RULES = payload.rules.map(([k, pat]) => [k, new RegExp(pat, flags)]);
      EEO = new RegExp(payload.never_fill, flags);
      RULES_SRC = payload.version || "served";
    } catch (e) {
      RULES = FALLBACK_RULES; EEO = FALLBACK_EEO; RULES_SRC = "bundled";
    }
  };
  window.__applyRulesSrc = () => RULES_SRC;
  if (!window.__APPLY_TEST) init();
})();
