import Foundation

/// The autofill engine, injected into the in-app browser. This is the same
/// field-matching brain as the desktop extension's content.js, ported to run
/// inside WKWebView: identity facts fill text inputs, native `<select>`s,
/// input + Ashby-style `role=combobox` dropdowns, and Yes/No radios;
/// tailored answers fill free-text questions. Autopilot clicks Next/Continue
/// (never Submit). Consent checkboxes stay manual; hard-blocked EEO
/// (orientation, religion, DOB, …) stay manual — optional demographics
/// (gender/race/veteran/disability) fill only when identity has a value.
///
/// Two user scripts are injected per page load:
///   • `dataScript`  (atDocumentStart) — puts the profile on `window.__APPLY`.
///   • `lib`         (atDocumentEnd)    — defines `window.__applyAutofill()`, which
///     the native ⚡ button calls; it returns nothing but posts a count back via
///     `window.webkit.messageHandlers.applyfill`.
enum Autofill {

    /// Profile payload for the page. Plain-interpolation string (no regex), so the
    /// values are JSON-encoded and dropped onto `window.__APPLY`.
    ///
    /// `rules` is the field-matching table fetched from the backend
    /// (`GET /apply/rules`). Passing it keeps this app, the desktop extension, and
    /// `app/fieldmatch.py` on one set of rules; when it's nil (offline, first
    /// launch) the engine falls back to the copy bundled in `lib`.
    static func dataScript(identity: [String: String], answers: [Question],
                           rules: RulesPayload? = nil) -> String {
        let id = (try? JSONSerialization.data(withJSONObject: identity)) ?? Data("{}".utf8)
        let qs = answers.map { ["question": $0.question, "answer": $0.answer] }
        let an = (try? JSONSerialization.data(withJSONObject: qs)) ?? Data("[]".utf8)
        let idStr = String(data: id, encoding: .utf8) ?? "{}"
        let anStr = String(data: an, encoding: .utf8) ?? "[]"
        var rulesStr = "null"
        if let rules, let data = try? JSONEncoder().encode(rules),
           let json = String(data: data, encoding: .utf8) {
            rulesStr = json
        }
        return "window.__APPLY = { identity: \(idStr), answers: \(anStr), rules: \(rulesStr) };"
    }

    /// The matcher + filler. Raw string so the regexes survive verbatim.
    static let lib = #"""
    (() => {
      if (window.__applyAutofill) return;
      const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
      // Offline fallback, generated from app/fieldmatch.py (rules version
      // db530a7f5e7c). The served rules below take priority — this exists only so a
      // first launch or a dropped connection still fills safely. If you edit these
      // by hand you've reintroduced the drift this design removes; regenerate them
      // from fieldmatch.py instead.
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
      const FALLBACK_EEO = /sexual orientation|pronoun.{0,4}optional|national origin|self.?identif|\beeo\b|equal (employment|opportunity)|protected (class|category)|lgbt|marital status|religio|citizenship status|date of birth|\bdob\b|transgender|lgbtq|hispanic|latino|gender identity/i;

      // Prefer the rules the backend served (app/fieldmatch.py is the source of
      // truth); tests/test_rules_parity.py proves they behave identically here.
      let RULES = FALLBACK_RULES, EEO = FALLBACK_EEO, RULES_SRC = "bundled";
      try {
        const served = window.__APPLY && window.__APPLY.rules;
        if (served && served.rules && served.rules.length && served.never_fill) {
          const flags = served.flags || "i";
          const compiled = served.rules.map(([k, p]) => [k, new RegExp(p, flags)]);
          const eeo = new RegExp(served.never_fill, flags);
          RULES = compiled; EEO = eeo; RULES_SRC = served.version || "served";
        }
      } catch (e) {
        // A malformed payload must never leave the page unfillable *or* unsafe —
        // the bundled copy above is already in place.
        RULES = FALLBACK_RULES; EEO = FALLBACK_EEO; RULES_SRC = "bundled (bad payload)";
      }
      const ID = () => (window.__APPLY && window.__APPLY.identity) || {};
      const ANS = () => (window.__APPLY && window.__APPLY.answers) || [];

      const cssEscape = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/"/g, '\\"');
      const isVisible = (el) => !!(el.offsetParent || el.getClientRects().length);
      const isTextField = (el) => {
        const tag = (el.tagName || "").toLowerCase();
        if (tag === "textarea") return true;
        if (tag !== "input") return false;
        return ["text","email","tel","url","search",""].includes((el.type||"text").toLowerCase());
      };
      // The question text for a field whose own label/placeholder is weak: walk up a
      // few wrappers and take the nearest short label/legend/heading. Custom React
      // widgets (Ashby/Greenhouse comboboxes) keep the question in an ancestor, not a
      // <label for>, so without this they never match a rule.
      function ancestorLabel(el) {
        let n = el.parentElement, hops = 0;
        while (n && hops < 5) {
          const cand = n.querySelector('label, legend, [class*="label"], [class*="Label"], [class*="title"], [class*="question"]');
          if (cand && !cand.contains(el)) {
            const t = cand.textContent.replace(/\s+/g, " ").trim();
            if (t && t.length <= 90) return t;
          }
          n = n.parentElement; hops++;
        }
        return "";
      }
      function fieldLabel(el) {
        const bits = [];
        if (el.id) { const l = document.querySelector(`label[for="${cssEscape(el.id)}"]`); if (l) bits.push(l.textContent); }
        const w = el.closest("label"); if (w) bits.push(w.textContent);
        if (el.getAttribute("aria-label")) bits.push(el.getAttribute("aria-label"));
        const by = el.getAttribute("aria-labelledby");
        if (by) by.split(/\s+/).forEach((id) => { const n = document.getElementById(id); if (n) bits.push(n.textContent); });
        if (el.placeholder) bits.push(el.placeholder);
        let s = bits.join(" ").replace(/\s+/g, " ").trim().toLowerCase();
        // Placeholder-only labels ("start typing…", "select…") don't name the field.
        if (s.length < 3 || /^(start typing|select|choose|search|type here)/.test(s)) {
          const a = ancestorLabel(el);
          if (a) s = a.replace(/\s+/g, " ").trim().toLowerCase();
        }
        // name/id is last-resort only. Stapling them onto a real label breaks
        // anchored rules ("Gender" + id=gender never matches /^\s*gender\s*$/).
        if (s.length < 3) {
          s = [el.name || "", el.id || ""].join(" ").replace(/\s+/g, " ").trim().toLowerCase();
        }
        return s;
      }
      function groupLabel(radios) {
        const first = radios[0];
        const fs = first.closest("fieldset");
        if (fs) { const lg = fs.querySelector("legend"); if (lg) return lg.textContent.replace(/\s+/g," ").trim().toLowerCase(); }
        const g = first.closest('[role="radiogroup"], [class*="question"], [class*="field"]');
        if (g) { const l = g.querySelector("label, legend, .label, [class*='label']"); if (l) return l.textContent.replace(/\s+/g," ").trim().toLowerCase(); }
        return fieldLabel(first);
      }
      function radioLabel(r) {
        if (r.id) { const l = document.querySelector(`label[for="${cssEscape(r.id)}"]`); if (l) return l.textContent; }
        const w = r.closest("label"); if (w) return w.textContent;
        return r.getAttribute("aria-label") || r.value || "";
      }
      const matchKey = (label) => { for (const [k, re] of RULES) if (re.test(label)) return k; return null; };

      function setText(el, value) {
        const proto = el.tagName.toLowerCase() === "textarea" ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
        setter.call(el, value);
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
        flash(el);
      }
      const optFor = (el, value) => {
        const v = String(value).trim().toLowerCase();
        let o = [...el.options].find((opt) => { const t=opt.text.trim().toLowerCase(), ov=opt.value.trim().toLowerCase();
          return t && (t===v || ov===v || t.includes(v) || v.includes(t)); });
        if (!o && /united states|^usa$|^us$/.test(v))
          o = [...el.options].find((opt) => /united states|^usa$|^u\.s/.test(opt.text.trim().toLowerCase()+" "+opt.value.toLowerCase()));
        if (!o && /^(mn|minnesota)$/.test(v))
          o = [...el.options].find((opt) => /\bminnesota\b|^mn$/.test(opt.text.trim().toLowerCase()+" "+opt.value.toLowerCase()));
        return o;
      };
      function setSelect(el, value) { const o = optFor(el, value); if (!o) return false; el.value = o.value; el.dispatchEvent(new Event("change",{bubbles:true})); flash(el); return true; }
      function flash(el) {
        try {
          if (!document.getElementById("__apply_magic_css")) {
            const s = document.createElement("style");
            s.id = "__apply_magic_css";
            s.textContent = `@keyframes __applyPulse{0%{box-shadow:0 0 0 0 rgba(91,124,110,.45)}70%{box-shadow:0 0 0 10px rgba(91,124,110,0)}100%{box-shadow:0 0 0 0 rgba(91,124,110,0)}}@keyframes __applyShimmer{0%{background-position:0% 50%;opacity:0}40%{opacity:.55}100%{background-position:100% 50%;opacity:0}}.__apply_flash{outline:2px solid #5B7C6E!important;outline-offset:2px;animation:__applyPulse .85s ease-out}.__apply_step_veil{pointer-events:none;position:fixed;inset:0;z-index:2147483646;background:linear-gradient(110deg,transparent 30%,rgba(91,124,110,.12) 50%,transparent 70%);background-size:200% 100%;animation:__applyShimmer .9s ease-out}`;
            document.documentElement.appendChild(s);
          }
          el.classList.add("__apply_flash");
          setTimeout(() => el.classList.remove("__apply_flash"), 900);
        } catch (e) {}
      }
      function stepVeil() {
        try {
          flash(document.body);
          const v = document.createElement("div");
          v.className = "__apply_step_veil";
          document.documentElement.appendChild(v);
          setTimeout(() => v.remove(), 950);
        } catch (e) {}
      }

      // A combobox opens its own popup list. Two shapes show up in the wild:
      //   1. <input role="combobox"> (React-select / Greenhouse location)
      //   2. <div role="combobox">   (Ashby / custom ARIA widgets — NOT an input)
      // Native <select> is handled separately via setSelect.
      const isInputCombobox = (el) => {
        if (el.tagName.toLowerCase() !== "input") return false;
        const role = (el.getAttribute("role") || "").toLowerCase();
        if (role === "combobox") return true;
        const pop = (el.getAttribute("aria-haspopup") || "").toLowerCase();
        if (pop === "listbox" || pop === "true") return true;
        if ((el.getAttribute("aria-autocomplete") || "").toLowerCase() === "list") return true;
        return false;
      };
      const pickOption = (opts, value) => {
        const v = String(value).trim().toLowerCase();
        const first = v.split(",")[0].trim();
        const txt = (o) => o.textContent.replace(/\s+/g, " ").trim().toLowerCase();
        let pick = opts.find((o) => txt(o) === v)
                || opts.find((o) => txt(o).startsWith(first) && first.length >= 2)
                || opts.find((o) => txt(o).includes(v) && v.length >= 3)
                || opts.find((o) => v.includes(txt(o)) && txt(o).length >= 3);
        if (!pick && /^(yes|no)$/.test(v))
          pick = opts.find((o) => new RegExp(`\\b${v}\\b`).test(txt(o)));
        if (!pick && /united states|^usa$|^u\.s\.?a?\.?$|^us$/.test(v))
          pick = opts.find((o) => /united states|^usa$|^u\.s\.?a?\.?$/.test(txt(o)));
        if (!pick && /^(mn|minnesota)$/.test(v))
          pick = opts.find((o) => /\bminnesota\b|^mn$/.test(txt(o)));
        return pick || null;
      };
      // Type into an <input> combobox, let the popup render, click best option.
      // If nothing matches we leave the typed text so the human just taps a suggestion.
      async function fillInputCombobox(el, value) {
        el.focus();
        setText(el, String(value));
        el.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "a" }));
        await sleep(650);
        const opts = [...document.querySelectorAll('[role="option"], li[id*="option"], [class*="option"], [class*="Option"]')]
          .filter((o) => (o.offsetParent || o.getClientRects().length) && o.textContent.trim());
        const pick = pickOption(opts, value);
        if (pick) { pick.click(); flash(el); return true; }
        return false;
      }
      // Ashby-style widget: click the combobox open, then click a role=option.
      async function fillWidgetCombobox(el, value) {
        try { el.click(); } catch (e) {}
        el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
        el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
        await sleep(450);
        const opts = [...document.querySelectorAll('[role="option"], li[id*="option"], [class*="option"], [class*="Option"]')]
          .filter((o) => (o.offsetParent || o.getClientRects().length) && o.textContent.trim());
        const pick = pickOption(opts, value);
        if (pick) { pick.click(); flash(el); return true; }
        // Dismiss the open list so we don't leave a stale popup over the form.
        try { el.click(); } catch (e) {}
        return false;
      }

      function setEditable(el, value) {
        el.focus();
        el.textContent = value;
        el.dispatchEvent(new InputEvent("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
        flash(el);
      }

      function bestAnswer(label) {
        const words = new Set(label.split(/\W+/).filter(Boolean));
        let best = null, score = -1;
        for (const q of ANS()) {
          const qw = new Set((q.question||"").toLowerCase().split(/\W+/).filter(Boolean));
          let n = 0; for (const w of words) if (qw.has(w)) n++;
          if (n > score) { score = n; best = q; }
        }
        return (score > 0 && best) ? best.answer : null;
      }

      function fillRadios() {
        const groups = {};
        for (const r of document.querySelectorAll('input[type="radio"]')) {
          if (!isVisible(r) || r.disabled || !r.name) continue;
          (groups[r.name] = groups[r.name] || []).push(r);
        }
        let n = 0;
        for (const name in groups) {
          const radios = groups[name];
          const gl = groupLabel(radios);
          if (EEO.test(gl)) continue;                         // never touch demographics
          const key = matchKey(gl);
          const raw = key && ID()[key];
          if (raw == null || raw === "") continue;
          const want = yesNoWant(gl, key, raw);
          const t = radios.find((r) => { const s=(radioLabel(r)+" "+r.value).toLowerCase();
            return s.includes(want) || (want==="yes" && /\byes\b/.test(s)) || (want==="no" && /\bno\b/.test(s)); });
          if (t && !t.checked) { t.checked = true; t.dispatchEvent(new Event("click",{bubbles:true})); t.dispatchEvent(new Event("change",{bubbles:true})); flash(t.closest("label")||t); n++; }
        }
        return n;
      }

      // Ashby (and some Greenhouse) render Yes/No as two big <button>s, not radios.
      // Find sibling Yes/No controls under a shared parent, map the nearby question
      // text through RULES, and click the matching choice.
      function countryMentioned(label) {
        if (/\bcanada\b/i.test(label)) return "canada";
        if (/united states|\bUSA\b|\bU\.S\.?A?\b/i.test(label)) return "united states";
        if (/united kingdom|\bUK\b|\bbritain\b/i.test(label)) return "united kingdom";
        return null;
      }
      function yesNoWant(label, key, rawVal) {
        let want = String(rawVal).trim().toLowerCase();
        if (want === "true") want = "yes";
        if (want === "false") want = "no";
        // Do not coerce an email/name/etc. into yes — that undoes a correct radio fill
        // when questionNear accidentally matches a different field.
        // "Authorized to work in Canada?" must not inherit a US work_authorized=Yes.
        if (key === "work_authorized") {
          const mentioned = countryMentioned(label);
          const mine = String(ID().country || "").toLowerCase();
          if (mentioned && mine) {
            const matches =
              (mentioned === "united states" && /united states|\busa\b/.test(mine)) ||
              (mentioned === "canada" && /\bcanada\b/.test(mine)) ||
              (mentioned === "united kingdom" && /united kingdom|\buk\b/.test(mine)) ||
              mine.includes(mentioned);
            if (!matches) return "no";
          }
        }
        return want;
      }
      function questionNear(el) {
        let node = el;
        for (let hops = 0; node && hops < 6; hops++, node = node.parentElement) {
          const raw = (node.innerText || "").replace(/\s+/g, " ").trim();
          const q = raw.replace(/\bYes\b/g, "").replace(/\bNo\b/g, "").replace(/\s+/g, " ").trim();
          if (q.length >= 12 && q.length <= 280) return q.toLowerCase();
        }
        return fieldLabel(el);
      }
      function fillYesNoButtons() {
        const cands = [...document.querySelectorAll(
          'button, [role="button"], [role="radio"], [role="option"], label, div, span'
        )].filter((el) => {
          if (!isVisible(el) || el.disabled) return false;
          const t = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
          if (!/^(yes|no)$/i.test(t)) return false;
          // Native radios are handled by fillRadios. Clicking their <label> here
          // undoes a correct No with a misfired Yes.
          if (el.querySelector('input[type="radio"], input[type="checkbox"]')) return false;
          if (el.tagName === "LABEL" && el.control &&
              /^(radio|checkbox)$/i.test(el.control.type || "")) return false;
          // Prefer leaf controls — skip wrappers that contain nested Yes/No nodes.
          if (el.querySelector('button, [role="button"], [role="radio"]')) return false;
          return true;
        });
        const byParent = new Map();
        for (const el of cands) {
          const p = el.parentElement;
          if (!p) continue;
          const t = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim().toLowerCase();
          (byParent.get(p) || (byParent.set(p, []), byParent.get(p))).push({ el, t });
        }
        let n = 0;
        const seen = new Set();
        for (const [, pair] of byParent) {
          const yes = pair.find((x) => x.t === "yes");
          const no = pair.find((x) => x.t === "no");
          if (!yes || !no) continue;
          const parent = yes.el.parentElement;
          if (!parent || seen.has(parent)) continue;
          seen.add(parent);
          const label = questionNear(parent);
          if (!label || EEO.test(label)) continue;
          const key = matchKey(label);
          const raw = key && ID()[key];
          if (raw == null || raw === "") continue;
          const want = yesNoWant(label, key, raw);
          if (want !== "yes" && want !== "no") continue;
          const target = want === "yes" ? yes.el : no.el;
          const already =
            target.getAttribute("aria-pressed") === "true" ||
            target.getAttribute("aria-checked") === "true" ||
            target.getAttribute("data-selected") === "true" ||
            /\b(selected|active|checked)\b/i.test(target.className || "");
          if (already) { n++; continue; }
          try { target.click(); } catch (e) {}
          target.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
          target.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
          target.dispatchEvent(new MouseEvent("click", { bubbles: true }));
          flash(target);
          n++;
        }
        return n;
      }

      window.__applyFieldLabel = fieldLabel;
      window.__applyAutofill = async function () {
        let filled = 0, essays = 0;
        // Pass 1 — <input> comboboxes/autocompletes first (async popup resolve).
        for (const el of document.querySelectorAll("input")) {
          if (!isVisible(el) || el.disabled || el.readOnly || !isInputCombobox(el)) continue;
          const label = fieldLabel(el);
          if (EEO.test(label)) continue;
          const key = matchKey(label);
          const val = key && ID()[key];
          if (val == null || val === "") continue;
          if (await fillInputCombobox(el, val)) filled++; else essays++;
        }
        // Pass 1b — Ashby/custom <div role="combobox"> widgets (not inputs).
        for (const el of document.querySelectorAll('[role="combobox"]')) {
          if (el.tagName.toLowerCase() === "input") continue;
          if (!isVisible(el) || el.getAttribute("aria-disabled") === "true") continue;
          const label = fieldLabel(el);
          if (EEO.test(label)) continue;
          const key = matchKey(label);
          const val = key && ID()[key];
          if (val == null || val === "") continue;
          const cur = (el.getAttribute("data-selected") || el.textContent || "").replace(/\s+/g, " ").trim().toLowerCase();
          if (cur && !/^(select|choose|pick)\b/.test(cur)) {
            const want = String(val).toLowerCase();
            if (cur === want || cur.includes(want) || want.includes(cur)) continue;
          }
          if (await fillWidgetCombobox(el, val)) filled++; else essays++;
        }
        // Pass 2 — plain inputs, textareas, native selects, contenteditable essays.
        for (const el of document.querySelectorAll('input, textarea, select, [contenteditable="true"]')) {
          if (!isVisible(el) || el.disabled || el.readOnly) continue;
          const tag = el.tagName.toLowerCase(), type = (el.type||"").toLowerCase();
          if (["radio","checkbox","file","hidden","submit","button"].includes(type)) continue;
          if (isInputCombobox(el)) continue;
          const label = fieldLabel(el);
          if (EEO.test(label)) continue;
          const editable = el.isContentEditable;
          if (tag === "textarea" || editable) {
            const cur = (editable ? el.textContent : el.value).trim();
            if (cur) continue;
            const a = bestAnswer(label);
            if (a) { editable ? setEditable(el, a) : setText(el, a); filled++; } else essays++;
            continue;
          }
          const key = matchKey(label);
          const val = key && ID()[key];
          if (val == null || val === "") continue;
          if (tag === "select" ? setSelect(el, val) : (setText(el, val), true)) filled++;
        }
        filled += fillRadios();
        filled += fillYesNoButtons();
        try { window.webkit.messageHandlers.applyfill.postMessage({ filled, essays, rules: RULES_SRC, status: "filled" }); } catch (e) {}
        return filled;
      };

      // Careers pages often put the real ATS form in a cross-origin iframe.
      // Native evaluateJavaScript only hits the main frame, so we also listen for a
      // fill ping from the parent and expose __applyAutofillAll to fan out.
      window.addEventListener("message", (e) => {
        if (!e.data || e.data.__apply !== "fill") return;
        if (typeof window.__applyAutofill === "function") window.__applyAutofill();
      });
      window.__applyAutofillAll = async function () {
        let n = 0;
        if (typeof window.__applyAutofill === "function") {
          const r = await window.__applyAutofill();
          if (typeof r === "number") n += r;
        }
        for (const f of document.querySelectorAll("iframe")) {
          try { f.contentWindow && f.contentWindow.postMessage({ __apply: "fill" }, "*"); }
          catch (e) {}
        }
        return n;
      };

      // --- form probe + Simplify-style autopilot (fill → Next → refill) -----
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
        for (const n of document.querySelectorAll("iframe, div, [class*='captcha'], [id*='captcha']")) {
          const sig = ((n.src || "") + " " + (n.id || "") + " " + (n.className || "")).toLowerCase();
          if (CAPTCHA_RE.test(sig)) return true;
        }
        return !!document.querySelector("[data-sitekey], .g-recaptcha, .h-captcha, .cf-turnstile");
      }
      function postDrive(msg) {
        try { window.webkit.messageHandlers.applyfill.postMessage(msg); } catch (e) {}
      }

      window.__applyFormProbe = function () {
        const labels = [];
        let hasPassword = false, hasFile = false;
        for (const el of document.querySelectorAll("input, textarea, select")) {
          if (!isVisible(el)) continue;
          const t = (el.type || "").toLowerCase();
          if (["hidden","submit","button","image","reset"].includes(t)) continue;
          if (t === "password") { hasPassword = true; continue; }
          if (t === "file") hasFile = true;
          labels.push(fieldLabel(el));
        }
        const buttons = [];
        for (const el of document.querySelectorAll("button, a[href], [role=button], input[type=submit], input[type=button]")) {
          if (!isVisible(el)) continue;
          const t = btnText(el);
          if (t) buttons.push(t);
        }
        if (detectCaptcha()) {
          return { kind: "captcha", score: 0, fillableCount: 0, matchedKeys: [],
            advanceLabel: null, submitVisible: false, revealLabel: null,
            blockerReason: "CAPTCHA or bot check in the way" };
        }
        const matched = [];
        for (const label of labels) {
          const key = matchKey(label);
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
          return { kind: "login", score: 0, fillableCount: fillable, matchedKeys: matched,
            advanceLabel, submitVisible, revealLabel, blockerReason: "Login or account wall" };
        }
        let score = fillable * 2 + signal * 3;
        if (submitVisible) score += 4;
        if (advanceLabel) score += 2;
        let kind = "unknown";
        if (fillable >= 2 || signal >= 2 || score >= 4) kind = "application";
        return { kind, score, fillableCount: fillable, matchedKeys: matched,
          advanceLabel, submitVisible, revealLabel, blockerReason: null };
      };

      function findAdvanceEl() {
        for (const el of document.querySelectorAll("button, a[href], [role=button], input[type=button]")) {
          if (!isVisible(el)) continue;
          const t = btnText(el);
          if (!t || NOT_ADVANCE_RE.test(t)) continue;
          if (ADVANCE_RE.test(t)) return el;
        }
        return null;
      }
      function findRevealEl() {
        for (const el of document.querySelectorAll("button, a[href], [role=button]")) {
          if (!isVisible(el)) continue;
          const t = btnText(el);
          if (!t || SUBMIT_RE.test(t)) continue;
          if (REVEAL_RE.test(t)) return el;
        }
        return null;
      }

      window.__applyDriveControl = { running: false, paused: false };

      window.__applyDrive = async function (opts) {
        opts = opts || {};
        const maxSteps = opts.maxSteps || 8;
        const ctrl = window.__applyDriveControl;
        if (opts.mode === "pause") {
          ctrl.paused = true; ctrl.running = false;
          postDrive({ status: "paused", filled: 0, essays: 0, step: 0 });
          return { status: "paused" };
        }
        if (opts.mode === "probe") {
          const p = window.__applyFormProbe();
          postDrive({ status: "probe", probe: p, filled: 0, essays: 0, step: 0 });
          return p;
        }

        ctrl.paused = false; ctrl.running = true;
        let step = 0, totalFilled = 0, totalEssays = 0;

        while (ctrl.running && !ctrl.paused && step < maxSteps) {
          let probe = window.__applyFormProbe();

          if (probe.kind === "captcha" || probe.kind === "login") {
            postDrive({ status: "needsHuman", blocker: probe.blockerReason || probe.kind,
              probe, filled: totalFilled, essays: totalEssays, step });
            while (ctrl.running && !ctrl.paused) {
              await sleep(1500);
              probe = window.__applyFormProbe();
              if (probe.kind !== "captcha" && probe.kind !== "login") {
                postDrive({ status: "watchingClear", probe, filled: totalFilled,
                  essays: totalEssays, step, blocker: null });
                ctrl.running = false;
                return { status: "watchingClear", probe };
              }
              postDrive({ status: "needsHuman", blocker: probe.blockerReason || probe.kind,
                probe, filled: totalFilled, essays: totalEssays, step });
            }
            return { status: ctrl.paused ? "paused" : "needsHuman", probe };
          }

          if (probe.kind === "unknown" && probe.fillableCount === 0 && probe.revealLabel) {
            const rev = findRevealEl();
            if (rev) {
              postDrive({ status: "advancing", step, filled: totalFilled, essays: totalEssays, detail: "reveal" });
              stepVeil(); rev.click(); await sleep(1600); step++; continue;
            }
          }

          if (probe.kind === "unknown" && probe.fillableCount === 0 && !probe.revealLabel) {
            postDrive({ status: "failed", blocker: "No application form on this page",
              filled: totalFilled, essays: totalEssays, step, probe });
            ctrl.running = false;
            return { status: "failed", probe };
          }

          postDrive({ status: "filling", step, filled: totalFilled, essays: totalEssays, probe });
          let filled = 0;
          const runner = window.__applyAutofillAll || window.__applyAutofill;
          if (typeof runner === "function") {
            filled = await runner();
            if (typeof filled !== "number") filled = 0;
          }
          totalFilled += filled || 0;

          probe = window.__applyFormProbe();
          if (probe.submitVisible && !probe.advanceLabel) {
            postDrive({ status: "ready", filled: totalFilled, essays: totalEssays, step, probe });
            ctrl.running = false;
            return { status: "ready", probe, filled: totalFilled };
          }
          const adv = findAdvanceEl();
          if (!adv) {
            postDrive({ status: "ready", filled: totalFilled, essays: totalEssays, step, probe });
            ctrl.running = false;
            return { status: "ready", probe, filled: totalFilled };
          }
          postDrive({ status: "advancing", step, filled: totalFilled, essays: totalEssays, detail: btnText(adv) });
          stepVeil(); flash(adv); await sleep(280); adv.click(); await sleep(1400); step++;
        }

        ctrl.running = false;
        if (ctrl.paused) {
          postDrive({ status: "paused", filled: totalFilled, essays: totalEssays, step });
          return { status: "paused" };
        }
        const probe = window.__applyFormProbe();
        postDrive({ status: "ready", filled: totalFilled, essays: totalEssays, step, probe });
        return { status: "ready", probe, filled: totalFilled };
      };
    })();
    """#
}
