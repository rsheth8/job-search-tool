import Foundation

/// The autofill engine, injected into the in-app browser. This is the same
/// field-matching brain as the desktop extension's content.js, ported to run
/// inside WKWebView: identity facts fill text inputs + dropdowns + Yes/No radios,
/// and the tailored answers fill the free-text questions.
///
/// Two user scripts are injected per page load:
///   • `dataScript`  (atDocumentStart) — puts the profile on `window.__APPLY`.
///   • `lib`         (atDocumentEnd)    — defines `window.__applyAutofill()`, which
///     the native ⚡ button calls; it returns nothing but posts a count back via
///     `window.webkit.messageHandlers.applyfill`.
enum Autofill {

    /// Profile payload for the page. Plain-interpolation string (no regex), so the
    /// values are JSON-encoded and dropped onto `window.__APPLY`.
    static func dataScript(identity: [String: String], answers: [Question]) -> String {
        let id = (try? JSONSerialization.data(withJSONObject: identity)) ?? Data("{}".utf8)
        let qs = answers.map { ["question": $0.question, "answer": $0.answer] }
        let an = (try? JSONSerialization.data(withJSONObject: qs)) ?? Data("[]".utf8)
        let idStr = String(data: id, encoding: .utf8) ?? "{}"
        let anStr = String(data: an, encoding: .utf8) ?? "[]"
        return "window.__APPLY = { identity: \(idStr), answers: \(anStr) };"
    }

    /// The matcher + filler. Raw string so the regexes survive verbatim.
    static let lib = #"""
    (() => {
      if (window.__applyAutofill) return;
      const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
      const RULES = [
        ["email", /e-?mail/i],
        ["preferred_name", /preferred (first )?name|nick.?name|known as|goes by/i],
        ["first_name", /first.?name|given.?name|legal first/i],
        ["last_name", /last.?name|family.?name|surname/i],
        ["full_name", /full.?name|^\s*name\s*$|your name|legal name/i],
        ["pronouns", /pronouns/i],
        ["phone", /phone|mobile|tel(ephone)?/i],
        ["linkedin", /linked.?in/i],
        ["github", /git.?hub/i],
        ["portfolio", /portfolio|personal (web)?site|^\s*website\s*$|^url$|other url|personal url/i],
        ["location", /\blocation\b|where are you (based|located)|city.{0,5}state/i],
        ["address", /street address|address line|mailing address|home address|^\s*address\b/i],
        ["city", /\bcity\b|town/i],
        ["state", /\bstate\b|province|region/i],
        ["zip", /\bzip\b|postal code|post.?code/i],
        ["country", /\bcountry\b|nation/i],
        ["school", /school|university|college|institution|alma mater/i],
        ["degree", /degree|qualification|level of (education|study)/i],
        ["discipline", /major|discipline|field of study|concentration/i],
        ["gpa", /\bgpa\b|grade point/i],
        ["grad_year", /grad(uation)?.{0,8}(year|date)|class of|completion (year|date)/i],
        ["current_company", /current (employer|company)|present (employer|company)|where do you (currently )?work/i],
        ["current_title", /current (title|role|position)|present (title|role|position)/i],
        ["years_experience", /years.{0,10}experience|experience.{0,10}years|\byoe\b/i],
        ["salary_expectation", /salary (expectation|requirement)|expected (salary|compensation|pay)|desired (salary|pay|compensation)|compensation expectation|pay expectation/i],
        ["start_date", /start date|available to start|earliest (start|availability)|when can you start|date available/i],
        ["willing_to_relocate", /willing to relocate|open to relocat|able to relocate|relocat/i],
        ["work_authorized", /authori[sz]ed to work|work authori[sz]ation|legally.{0,12}work|eligible to work|right to work/i],
        ["needs_sponsorship", /sponsor(ship)?|require.{0,12}visa|visa.{0,12}status|immigration status/i],
      ];
      const EEO = /gender|sex\b|race|ethnic|hispanic|latino|veteran|disab|sexual orientation/i;
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
        bits.push(el.name || "", el.id || "");
        let s = bits.join(" ").replace(/\s+/g, " ").trim().toLowerCase();
        // Placeholder-only labels ("start typing…", "select…") don't name the field.
        if (s.length < 3 || /^(start typing|select|choose|search|type here)/.test(s)) {
          const a = ancestorLabel(el);
          if (a) s = (a + " " + s).replace(/\s+/g, " ").trim().toLowerCase();
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
        return [...el.options].find((o) => { const t=o.text.trim().toLowerCase(), ov=o.value.trim().toLowerCase();
          return t && (t===v || ov===v || t.includes(v) || v.includes(t)); });
      };
      function setSelect(el, value) { const o = optFor(el, value); if (!o) return false; el.value = o.value; el.dispatchEvent(new Event("change",{bubbles:true})); flash(el); return true; }
      function flash(el) { try { el.style.outline = "2px solid #16a34a"; setTimeout(()=>{el.style.outline="";}, 800); } catch(e){} }

      // A combobox is an input that opens its own popup list (React-select, Ashby/
      // Greenhouse location & "how did you hear" widgets). Native <select> is handled
      // separately. We detect it by role/aria, not class, so it works across ATSs.
      const isCombobox = (el) => {
        if (el.tagName.toLowerCase() !== "input") return false;
        const role = (el.getAttribute("role") || "").toLowerCase();
        if (role === "combobox") return true;
        const pop = (el.getAttribute("aria-haspopup") || "").toLowerCase();
        if (pop === "listbox" || pop === "true") return true;
        if ((el.getAttribute("aria-autocomplete") || "").toLowerCase() === "list") return true;
        return false;
      };
      // Type the value, let the popup render, then click the best-matching option.
      // If nothing matches we leave the typed text so the human just taps a suggestion.
      async function fillCombobox(el, value) {
        el.focus();
        setText(el, String(value));
        el.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "a" }));
        await sleep(650);
        const v = String(value).trim().toLowerCase();
        const first = v.split(",")[0].trim();
        const opts = [...document.querySelectorAll('[role="option"], li[id*="option"], [class*="option"], [class*="Option"]')]
          .filter((o) => (o.offsetParent || o.getClientRects().length) && o.textContent.trim());
        const txt = (o) => o.textContent.replace(/\s+/g, " ").trim().toLowerCase();
        let pick = opts.find((o) => txt(o) === v)
                || opts.find((o) => txt(o).startsWith(first) && first.length >= 2)
                || opts.find((o) => txt(o).includes(v) && v.length >= 3);
        if (pick) { pick.click(); flash(el); return true; }
        return false;   // typed but unresolved — counts as "needs you"
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
          const val = key && ID()[key];
          if (val == null || val === "") continue;
          const want = String(val).toLowerCase();
          const t = radios.find((r) => { const s=(radioLabel(r)+" "+r.value).toLowerCase();
            return s.includes(want) || (want==="yes" && /\byes\b/.test(s)) || (want==="no" && /\bno\b/.test(s)); });
          if (t && !t.checked) { t.checked = true; t.dispatchEvent(new Event("click",{bubbles:true})); t.dispatchEvent(new Event("change",{bubbles:true})); flash(t.closest("label")||t); n++; }
        }
        return n;
      }

      window.__applyAutofill = async function () {
        let filled = 0, essays = 0;
        // Pass 1 — comboboxes/autocompletes first (they're async, and resolving them
        // can insert hidden inputs the standard pass would otherwise double-handle).
        for (const el of document.querySelectorAll("input")) {
          if (!isVisible(el) || el.disabled || el.readOnly || !isCombobox(el)) continue;
          const label = fieldLabel(el);
          if (EEO.test(label)) continue;
          const key = matchKey(label);
          const val = key && ID()[key];
          if (val == null || val === "") continue;
          if (await fillCombobox(el, val)) filled++; else essays++;
        }
        // Pass 2 — plain inputs, textareas, native selects, contenteditable essays.
        for (const el of document.querySelectorAll('input, textarea, select, [contenteditable="true"]')) {
          if (!isVisible(el) || el.disabled || el.readOnly) continue;
          const tag = el.tagName.toLowerCase(), type = (el.type||"").toLowerCase();
          if (["radio","checkbox","file","hidden","submit","button"].includes(type)) continue;
          if (isCombobox(el)) continue;                       // handled in pass 1
          const label = fieldLabel(el);
          if (EEO.test(label)) continue;                      // never auto-fill demographics
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
        try { window.webkit.messageHandlers.applyfill.postMessage({ filled, essays }); } catch (e) {}
        return filled;
      };
    })();
    """#
}
