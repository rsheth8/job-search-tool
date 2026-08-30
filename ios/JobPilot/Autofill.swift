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
/// Fill is not gated on Greenhouse/Lever/Ashby. `formprobe` scores whatever
/// public form is on the page; login walls and CAPTCHAs pause for the human.
///
/// Choice fields (school, location, degree, country) **search then click** the
/// closest allowed option. Typing a value that isn't on the list looks filled
/// and then fails to save.
///
/// Two user scripts are injected per page load, both with
/// `forMainFrameOnly: false`:
///   • `dataScript`  (atDocumentStart) — puts the profile on `window.__APPLY`.
///   • `lib`         (atDocumentEnd)    — defines `window.__applyAutofill()`, which
///     the native ⚡ button calls; it returns `{ filled, essays, skips }` and the
///     *top frame* posts the total back via
///     `window.webkit.messageHandlers.applyfill`.
///
/// Because the engine runs in every frame and they all share one native message
/// handler, two rules hold everywhere in `lib`:
///   1. Only the top frame posts to native (`IS_TOP`). Subframes answer their
///      parent's ping over `postMessage`, and the top frame adds the totals up.
///      Without this an about:blank or reCAPTCHA frame's `filled: 0` lands after
///      the real result and the app reports "No fields matched" over a form it
///      just filled.
///   2. Fill blanks, never overwrite (`hasOwnValue`). A second ⚡ tap, or an
///      autopilot step revisiting a mounted field, must not undo an answer the
///      person typed or a choice an earlier pass committed.
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
      // Only the top frame is allowed to talk to the native side. Both user
      // scripts are injected with `forMainFrameOnly: false`, so every
      // about:blank, reCAPTCHA and analytics frame on the page runs this engine
      // too — and every one of them posts into the SAME native message handler.
      // A noise frame's "filled: 0" used to land after the real result and
      // overwrite it, so the toast read "No fields matched" over a form that had
      // just been filled, and `skips` (the signal that grows the phrasing table)
      // was blanked out on exactly the embedded forms that need it most.
      const IS_TOP = (() => { try { return window.top === window; } catch (e) { return false; } })();
      // Offline fallback, generated from app/fieldmatch.py (rules version
      // 6de898a0bcef). The served rules below take priority — this exists only so a
      // first launch or a dropped connection still fills safely. If you edit these
      // by hand you've reintroduced the drift this design removes; regenerate them
      // from fieldmatch.py instead.
      const FALLBACK_RULES = [
        ["email", /e-?mail/i],
        ["preferred_name", /preferred (first )?name|nick.?name|known as|goes by/i],
        ["first_name", /first.?name|given.?name|legal first|name\s*\(?\s*first|forename/i],
        ["last_name", /last.?name|family.?name|surname|name\s*\(?\s*last/i],
        ["full_name", /full.?name|^\s*name\s*$|your name|legal name/i],
        ["pronouns", /pronouns/i],
        ["phone", /\bphone\b|\bmobile\b|\btel(ephone)?\b|cell.?phone|contact number/i],
        ["linkedin", /linked.?in/i],
        ["github", /git.?hub/i],
        ["portfolio", /portfolio|personal (web)?site|^\s*website\s*$|^url$|other url|personal url|home ?page|personal page/i],
        ["work_arrangement", /preferred work (location|arrangement)|work (from home|arrangement|location preference)|on-?site|onsite|fully remote|remote or hybrid|hybrid or remote/i],
        ["location", /\blocation\b|where are you (based|located)|city.{0,5}state|where do you (live|reside)|currently (based|located|reside|living)|based in|city of residence/i],
        ["address", /street address|address line|mailing address|home address|^\s*address\b|line 1/i],
        ["city", /\bcity\b|town/i],
        ["country", /\bcountry\b|nation/i],
        ["state", /\bstate\b|province|state.?\/?\s*region/i],
        ["zip", /\bzip\b|postal code|post.?code/i],
        ["school", /school|university|college|institution|alma mater|where did you study|name of (the )?school|educational institution/i],
        ["degree", /degree|qualification|level of (education|study)|highest (level of )?education|degree (type|obtained|earned)/i],
        ["discipline", /major|discipline|field of study|concentration|area of study|what did you (study|major)/i],
        ["gpa", /\bgpa\b|grade point|cumulative gpa|overall gpa/i],
        ["grad_month", /end date month|grad(?:uation)? month/i],
        ["grad_year_num", /end date year/i],
        ["grad_year", /when (do|will) you graduate|expected graduation|grad(uation)?.{0,12}(year|date)|class of|completion (year|date)|year of grad|anticipated graduation|graduation date/i],
        ["intern_season", /winter or summer internship|prefer.{0,30}internship|internship.{0,16}(term|season|preference|period|availability)|which (term|season|internship)/i],
        ["current_company", /current (employer|company)|present (employer|company)|where do you (currently )?work|most recent (employer|company)|current or most recent employer|^\s*employer\s*$/i],
        ["current_title", /current (title|role|position)|present (title|role|position)|most recent (title|role|position)|job title/i],
        ["years_experience", /years.{0,16}experience|experience.{0,16}years|\byoe\b|how many years/i],
        ["salary_expectation", /salary (expectation|requirement)|expected (salary|compensation|pay)|desired (salary|pay|compensation)|compensation expectation|pay expectation|target (salary|comp|compensation)/i],
        ["start_date", /start date|available to start|earliest (start|availability)|when (can|could|are) you (start|available)|date available|notice period|availability date/i],
        ["willing_to_relocate", /willing to relocate|open to relocat|able to relocate|relocat/i],
        ["work_authorized", /authori[sz]ed to work|work authori[sz]ation|legally.{0,16}work|eligible to work|right to work|work eligibility/i],
        ["needs_sponsorship", /sponsor(ship)?|require.{0,20}visa|visa.{0,16}status|immigration status|now or in the future.{0,24}sponsor/i],
        ["background_check", /background check|criminal (background|history|record)|background screening/i],
        ["drug_test", /drug (test|screen|screening)|substance (test|screen)/i],
        ["over_18", /over 18|18 years|at least 18|age 18|legal age|18 years of age/i],
        ["can_travel", /willing to travel|able to travel|travel (required|for (work|this))|open to travel|willing and able to travel/i],
        ["previously_applied", /previously applied|applied (here|before|to (this|us))|worked (here|for us|at this)|former employee|prior application|previously (been )?employed|ever (worked for|applied to)/i],
        ["how_heard", /how did you (hear|learn|find)|where did you (hear|learn|find)|hear about (this|us|the)|referral source|source of (this )?application|how.?['’]?d you (find|hear)|find this (role|job|opportunit)/i],
        ["related_to_employee", /related to|relative (at|of)|relatives? who work|know anyone|family member|referred by|employee of/i],
        ["gender", /\bgender\b|^\s*sex\s*$|what is your sex\b/i],
        ["race", /\brace\b|race\s*\/?\s*ethnicity|racial identity/i],
        ["ethnicity", /^\s*ethnicity\s*$|ethnic background/i],
        ["hispanic_latino", /hispanic|latino/i],
        ["veteran_status", /veteran|protected veteran|military status/i],
        ["disability_status", /disabilit(y|ies)|disabled/i],
      ];
      const FALLBACK_EEO = /sexual orientation|pronoun.{0,4}optional|national origin|self.?identif|\beeo\b|equal (employment|opportunity)|protected (class|category)|lgbt|marital status|religio|citizenship status|date of birth|\bdob\b|transgender|lgbtq|gender identity|birth(day|date)|\bbday\b/i;
      const FALLBACK_ATTR_RULES = [
        ["linkedin", /urls\]\[linkedin|linkedin_url|(?:^|\[)linkedin(?:\]|$)/i],
        ["github", /urls\]\[github|github_url|(?:^|\[)github(?:\]|$)/i],
        ["portfolio", /urls\]\[(?:portfolio|website|other)|personal_url|website_url/i],
        ["email", /job_application\[email\]|_systemfield_email|(?:^|\[)email(?:\]|$)/i],
        ["preferred_name", /preferred_first_name|preferred_name/i],
        ["first_name", /first_name|_systemfield_first/i],
        ["last_name", /last_name|_systemfield_last/i],
        ["phone", /job_application\[phone\]|_systemfield_phone|(?:^|\[)phone(?:\]|$)/i],
        ["location", /job_application\[location\]|_systemfield_location|(?:^|\[)location(?:\]|$)/i],
        ["school", /school_name|_systemfield_school|educations?[^\s]*school/i],
        ["degree", /educations?[^\s]*degree/i],
        ["discipline", /educations?[^\s]*discipline/i],
        ["grad_month", /end_date\]\[month/i],
        ["grad_year_num", /end_date\]\[year/i],
        ["current_company", /company_name|_systemfield_company|(?:^|\[)org(?:\]|$)/i],
        ["current_title", /employments?[^\s]*title/i],
        ["full_name", /_systemfield_name$|^name$/i],
        ["address", /street_address|address_line|job_application\[address\]/i],
        ["city", /(?:^|\[)city(?:\]|$)|_systemfield_city/i],
        ["state", /(?:^|\[)state(?:\]|$)|_systemfield_state/i],
        ["zip", /postal_code|(?:^|\[)zip(?:\]|$)|_systemfield_zip/i],
        ["country", /job_application\[country\]|_systemfield_country|(?:^|\[)country(?:\]|$)/i],
      ];
      const FALLBACK_AUTOCOMPLETE = {"given-name":"first_name","family-name":"last_name","name":"full_name","nickname":"preferred_name","email":"email","tel":"phone","tel-national":"phone","street-address":"address","address-line1":"address","address-level2":"city","address-level1":"state","postal-code":"zip","country":"country","country-name":"country","organization":"current_company","organization-title":"current_title","url":"portfolio"};

      // Prefer the rules the backend served (app/fieldmatch.py is the source of
      // truth); tests/test_rules_parity.py proves they behave identically here.
      let RULES = FALLBACK_RULES, ATTR_RULES = FALLBACK_ATTR_RULES;
      let AUTOCOMPLETE = FALLBACK_AUTOCOMPLETE;
      let EEO = FALLBACK_EEO, RULES_SRC = "bundled";
      try {
        const served = window.__APPLY && window.__APPLY.rules;
        if (served && served.rules && served.rules.length && served.never_fill) {
          const flags = served.flags || "i";
          const compiled = served.rules.map(([k, p]) => [k, new RegExp(p, flags)]);
          const eeo = new RegExp(served.never_fill, flags);
          RULES = compiled; EEO = eeo; RULES_SRC = served.version || "served";
          if (served.attr_rules && served.attr_rules.length) {
            ATTR_RULES = served.attr_rules.map(([k, p]) => [k, new RegExp(p, flags)]);
          }
          if (served.autocomplete && typeof served.autocomplete === "object") {
            AUTOCOMPLETE = served.autocomplete;
          }
        }
      } catch (e) {
        // A malformed payload must never leave the page unfillable *or* unsafe —
        // the bundled copy above is already in place.
        RULES = FALLBACK_RULES; ATTR_RULES = FALLBACK_ATTR_RULES;
        AUTOCOMPLETE = FALLBACK_AUTOCOMPLETE;
        EEO = FALLBACK_EEO; RULES_SRC = "bundled (bad payload)";
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
      // `textContent` also hands back whatever sits inside <svg> — a <title>,
      // a <desc>, or the "svgs not supported by this browser" fallback text that
      // icon sets ship — plus any <script>/<style>. Workable's address label
      // reads as "*addresssvgs not supported by this browser. address" that way:
      // it matches no rule, and then lands in the phrasing table as a skip that
      // no future rule could ever match either. Read the text a person can see.
      function labelText(node) {
        if (!node) return "";
        try {
          const clone = node.cloneNode(true);
          for (const junk of clone.querySelectorAll("svg, script, style, noscript")) {
            junk.remove();
          }
          return clone.textContent || "";
        } catch (e) {
          return node.textContent || "";
        }
      }

      function ancestorLabel(el) {
        let n = el.parentElement, hops = 0;
        while (n && hops < 5) {
          // Only a container that wraps this one field can name it. Keep walking
          // up past a container holding several controls and `querySelector`
          // hands back the *first* label under it — which is how Workable's
          // `city`, `postcode` and `country` all came back labelled "first name"
          // and got the first name typed into them.
          if (n.querySelectorAll("input, select, textarea").length > 1) break;
          const cand = n.querySelector('label, legend, [class*="label"], [class*="Label"], [class*="title"], [class*="question"]');
          if (cand && !cand.contains(el)) {
            // A label that names a different field by id is not this one's.
            const forId = cand.getAttribute && cand.getAttribute("for");
            if (!forId || forId === el.id) {
              const t = labelText(cand).replace(/\s+/g, " ").trim();
              if (t && t.length <= 90) return t;
            }
          }
          n = n.parentElement; hops++;
        }
        return "";
      }
      function fieldLabel(el) {
        const bits = [];
        if (el.id) { const l = document.querySelector(`label[for="${cssEscape(el.id)}"]`); if (l) bits.push(labelText(l)); }
        const w = el.closest("label"); if (w) bits.push(labelText(w));
        if (el.getAttribute("aria-label")) bits.push(el.getAttribute("aria-label"));
        const by = el.getAttribute("aria-labelledby");
        if (by) by.split(/\s+/).forEach((id) => { const n = document.getElementById(id); if (n) bits.push(labelText(n)); });
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
        if (fs) { const lg = fs.querySelector("legend"); if (lg) return labelText(lg).replace(/\s+/g," ").trim().toLowerCase(); }
        const g = first.closest('[role="radiogroup"], [class*="question"], [class*="field"]');
        if (g) { const l = g.querySelector("label, legend, .label, [class*='label']"); if (l) return labelText(l).replace(/\s+/g," ").trim().toLowerCase(); }
        return fieldLabel(first);
      }
      function radioLabel(r) {
        if (r.id) { const l = document.querySelector(`label[for="${cssEscape(r.id)}"]`); if (l) return labelText(l); }
        const w = r.closest("label"); if (w) return labelText(w);
        return r.getAttribute("aria-label") || r.value || "";
      }
      const matchKeyLabel = (label) => { for (const [k, re] of RULES) if (re.test(label)) return k; return null; };
      function matchKey(label, el) {
        const text = (label || "").trim();
        if (text && EEO.test(text)) return null;
        if (text) {
          const fromLabel = matchKeyLabel(text);
          if (fromLabel) return fromLabel;
        }
        if (!el) return null;
        const acRaw = (el.getAttribute && el.getAttribute("autocomplete") || "").trim().toLowerCase();
        if (acRaw && acRaw !== "off" && acRaw !== "on") {
          const token = acRaw.split(/\s+/).pop();
          if (AUTOCOMPLETE[token]) return AUTOCOMPLETE[token];
        }
        for (const source of [el.name || "", el.id || ""]) {
          if (!source) continue;
          if (EEO.test(source)) return null;
          for (const [k, re] of ATTR_RULES) if (re.test(source)) return k;
        }
        const typ = (el.type || "").toLowerCase();
        if (typ === "email") return "email";
        if (typ === "tel") return "phone";
        return null;
      }
      function identityValue(key, label) {
        let raw = ID()[key];
        if (raw == null || raw === "") return raw;
        // "authorized to work … without sponsorship" is Yes only if we don't need a visa.
        if (key === "work_authorized" && /sponsor/i.test(label || "")) {
          const need = String(ID().needs_sponsorship || "").trim().toLowerCase();
          if (["yes", "true", "1"].includes(need)) return "No";
        }
        return raw;
      }
      window.__applyMatchKey = (label, name, id, autocomplete, inputType) =>
        matchKey(label, { name: name || "", id: id || "", type: inputType || "",
          getAttribute: (n) => n === "autocomplete" ? (autocomplete || "") : null });

      // Closest allowed option — same rules as app/fieldmatch.py select_value.
      // Typing a value that isn't on the list looks filled and then fails to save.
      const CHOICE_KEYS = new Set(["school","degree","location","country","state","city","discipline","work_arrangement","how_heard","grad_year","grad_month","grad_year_num","gpa","intern_season","hispanic_latino","gender","race","ethnicity","veteran_status","disability_status","years_experience","salary_expectation"]);
      const GENERIC_OPT = new Set(["university","college","school","united","states","city","campus","institute","department","degree","option"]);
      const STOP_OPT = new Set(["the","of","and","a","an","at","to","for","or","on"]);
      const AMBIG_ABBR = new Set(["in","or","me","hi","oh","ok","id","la","ma","md"]);
      const STATE_ABBR = {al:"alabama",ak:"alaska",az:"arizona",ar:"arkansas",ca:"california",co:"colorado",ct:"connecticut",de:"delaware",fl:"florida",ga:"georgia",hi:"hawaii",id:"idaho",il:"illinois",in:"indiana",ia:"iowa",ks:"kansas",ky:"kentucky",la:"louisiana",me:"maine",md:"maryland",ma:"massachusetts",mi:"michigan",mn:"minnesota",ms:"mississippi",mo:"missouri",mt:"montana",ne:"nebraska",nv:"nevada",nh:"new hampshire",nj:"new jersey",nm:"new mexico",ny:"new york",nc:"north carolina",nd:"north dakota",oh:"ohio",ok:"oklahoma",or:"oregon",pa:"pennsylvania",ri:"rhode island",sc:"south carolina",sd:"south dakota",tn:"tennessee",tx:"texas",ut:"utah",vt:"vermont",va:"virginia",wa:"washington",wv:"west virginia",wi:"wisconsin",wy:"wyoming",dc:"district of columbia"};
      const LEVEL_ORDER = ["doctorate","master","bachelor","associate","high school"];
      const PLACEHOLDER_OPT = /^(select|choose|pick|search|type|start typing|please select|n\/?a|--+|—+)?\.?$/i;
      const COMMA_ABBR = /,\s*([A-Za-z]{2})\b/;
      function canonical(text, expandStates) {
        let t = String(text || "").toLowerCase().trim();
        if (!t) return "";
        t = t.replace(/&/g, " and ").replace(/-/g, " ");
        t = t.replace(/\bb\.?\s*sc?\b/g, " bachelor ").replace(/\bb\.?\s*a\.?\b/g, " bachelor ");
        t = t.replace(/\bm\.?\s*sc?\b/g, " master ").replace(/\bm\.?\s*a\.?\b/g, " master ");
        t = t.replace(/\bmba\b/g, " mba master ").replace(/\bph\.?\s*d\.?\b/g, " doctorate ");
        t = t.replace(/\bbachelors?\b/g, " bachelor ").replace(/\bmasters?\b/g, " master ");
        t = t.replace(/\b(doctorate|doctoral|dphil)\b/g, " doctorate ").replace(/\bassociates?\b/g, " associate ");
        t = t.replace(/\b(women|woman)\b/g, " female ").replace(/\b(men|man)\b/g, " male ");
        t = t.replace(/\bnon\s*binary\b/g, " nonbinary ");
        t = t.replace(/\bwfh\b/g, " remote ").replace(/\bwork from home\b/g, " remote ");
        t = t.replace(/\bin office\b/g, " onsite ").replace(/\bon site\b/g, " onsite ");
        t = t.replace(/[^a-z0-9]+/g, " ");
        const out = [];
        for (const tok of t.split(/\s+/).filter(Boolean)) {
          if (tok === "usa" || tok === "us") out.push("united", "states");
          else if (tok === "uk") out.push("united", "kingdom");
          else if (expandStates && STATE_ABBR[tok] && !AMBIG_ABBR.has(tok)) out.push(...STATE_ABBR[tok].split(" "));
          else if (!STOP_OPT.has(tok)) out.push(tok);
        }
        return out.join(" ");
      }
      // "IN" → "Indiana", but only when the list really is state names. A
      // two-letter value that *is* a USPS code is unambiguous, unlike the same
      // letters in running text, so AMBIG_ABBR must not apply here. Without it
      // the state select took the wrong state — "IN" scored highest against
      // "Maine", "LA" against "Alabama" — and "OR"/"MA"/"MD"/"ME" filled nothing.
      // Mirrors _expand_bare_state in app/fieldmatch.py.
      const STATE_NAMES = new Set(Object.values(STATE_ABBR).map((v) => canonical(v, false)));
      const titleCase = (s) => s.replace(/\b[a-z]/g, (c) => c.toUpperCase());
      function expandBareState(raw, options) {
        const code = String(raw || "").trim().toLowerCase();
        if (!/^[a-z]{2}$/.test(code) || !STATE_ABBR[code]) return raw;
        for (const o of options) {
          if (STATE_NAMES.has(canonical(o, false))) return titleCase(STATE_ABBR[code]);
        }
        return raw;
      }
      function levelsIn(c) {
        const found = [];
        const parts = new Set(c.split(" "));
        for (const level of LEVEL_ORDER) {
          if (level === "high school") { if (parts.has("high") && parts.has("school")) found.push(level); }
          else if (parts.has(level) || (level === "master" && parts.has("mba"))) found.push(level);
        }
        return found;
      }
      function optionScore(wantC, optC) {
        if (!wantC || !optC) return 0;
        const wt = new Set(wantC.split(" ")), ot = new Set(optC.split(" "));
        if (!wt.size || !ot.size) return 0;
        const neg = (s) => ["not","no","never","none"].some((w) => s.has(w));
        if (neg(wt) !== neg(ot)) return 0;
        if (wantC === optC) return 10;
        if (wantC.includes(optC) || optC.includes(wantC))
          return 3 + Math.min(wantC.length, optC.length) / Math.max(wantC.length, optC.length);
        const distinct = wantC.split(" ").filter((t) => !GENERIC_OPT.has(t) && t.length > 2);
        if (distinct.length && !ot.has(distinct[0])) return 0;
        let shared = 0; for (const t of wt) if (ot.has(t)) shared++;
        if (!shared) return 0;
        return shared / wt.size + shared / ot.size;
      }
      const MONTH_NUM = {january:1,jan:1,february:2,feb:2,march:3,mar:3,april:4,apr:4,may:5,june:6,jun:6,july:7,jul:7,august:8,aug:8,september:9,sept:9,sep:9,october:10,oct:10,november:11,nov:11,december:12,dec:12};
      function parseMonthsYear(text) {
        const ym = String(text || "").match(/\b((?:19|20)\d{2})\b/);
        const year = ym ? parseInt(ym[1], 10) : null;
        const months = [];
        const low = String(text || "").toLowerCase();
        for (const [name, num] of Object.entries(MONTH_NUM)) {
          if (new RegExp("\\b" + name + "\\b").test(low)) months.push(num);
        }
        return { months: [...new Set(months)].sort((a,b)=>a-b), year };
      }
      function gpaRangePick(cleaned, raw) {
        const gm = String(raw).match(/\b([0-4](?:\.\d+)?)\b/);
        if (!gm) return null;
        const gpa = parseFloat(gm[1]);
        const rangeRe = /(\d+(?:\.\d+)?)\s*[-–—to]+\s*(\d+(?:\.\d+)?)/i;
        const underRe = /(\d+(?:\.\d+)?)\s*or\s*(?:under|below|less)/i;
        const banded = cleaned.filter((o) => rangeRe.test(o) || underRe.test(o));
        if (banded.length < 2) return null;
        const hits = [];
        for (const o of banded) {
          const rm = o.match(rangeRe);
          if (rm) {
            const lo = parseFloat(rm[1]), hi = parseFloat(rm[2]);
            if (gpa >= Math.min(lo, hi) && gpa <= Math.max(lo, hi)) hits.push([o, Math.abs(hi - lo)]);
            continue;
          }
          const um = o.match(underRe);
          if (um && gpa <= parseFloat(um[1])) hits.push([o, 99]);
        }
        if (!hits.length) return null;
        hits.sort((a, b) => a[1] - b[1]);
        return hits[0][0];
      }
      function dateBucketPick(cleaned, raw) {
        const want = parseMonthsYear(raw);
        if (!want.months.length && want.year == null) return null;
        const scored = [];
        let bucketish = 0;
        let monthOnly = true;
        for (const o of cleaned) {
          const p = parseMonthsYear(o);
          if (p.year == null && p.months.length !== 1) monthOnly = false;
          if (p.year != null) monthOnly = false;
          if (p.year == null && !p.months.length) continue;
          if (p.year != null || p.months.length >= 2) bucketish++;
          if (want.year != null && p.year != null && want.year !== p.year) continue;
          if (want.months.length && p.months.length) {
            const lo = Math.min(...p.months), hi = Math.max(...p.months);
            if (want.months.some((m) => m >= lo && m <= hi))
              scored.push([o, Math.abs((lo + hi) / 2 - want.months[0])]);
          } else if (want.year != null && p.year === want.year && !want.months.length) {
            scored.push([o, 0]);
          }
        }
        if (!scored.length) return null;
        if (bucketish < 2 && !monthOnly) return null;
        scored.sort((a, b) => a[1] - b[1]);
        return scored[0][0];
      }
      const DECLINE_OPT = /decline|prefer not|do not wish|don't wish|choose not to|rather not/i;
      // An option that *opens* with Yes or No has already declared its side.
      // Reading the rest for shape flips the commonest work-auth phrasing there
      // is: "Yes, I do not require sponsorship" hits \bnot\b and classifies as
      // No, so both options come back "no", neither side has a candidate, and
      // the question is left blank. Mirrors _LEAD_YES/_LEAD_NO in fieldmatch.py.
      const LEAD_YES = /^\s*(?:yes|y)\b/i;
      const LEAD_NO = /^\s*(?:no|n)\b/i;
      const NO_SHAPE = /\b(?:not|never|none)\b|(?:^|[\s,])no(?:[\s,]|$)|i am not|i do not|do not have|don't have|not hispanic|not latino/i;
      const YES_SHAPE = /(?:^|[\s,])yes(?:[\s,]|$)|^\s*y\s*$|\bi am\b|\bauthorized\b|\bwilling\b|\bable to\b|\bopen to\b|\bhispanic\b|\blatino\b|\bveteran\b|\bdisabilit/i;
      function asYesNo(raw) {
        const t = String(raw || "").trim().toLowerCase();
        if (["yes","y","true","1"].includes(t)) return "yes";
        if (["no","n","false","0"].includes(t)) return "no";
        if (/^\s*yes\b/.test(t) && !/\bno\b/.test(t)) return "yes";
        if (/^\s*no\b/.test(t)) return "no";
        if (/\b(not|never)\b/.test(t) && !/\byes\b/.test(t)) return "no";
        return null;
      }
      function optionYesNo(text) {
        const t = String(text || "").trim();
        if (!t || DECLINE_OPT.test(t)) return null;
        if (LEAD_YES.test(t)) return "yes";
        if (LEAD_NO.test(t)) return "no";
        if (NO_SHAPE.test(t)) return "no";
        if (YES_SHAPE.test(t) || ["yes","y","true"].includes(t.toLowerCase())) return "yes";
        if (["no","n","false"].includes(t.toLowerCase())) return "no";
        return null;
      }
      function yesNoPick(cleaned, raw) {
        const want = asYesNo(raw);
        if (!want) return null;
        const yeses = [], nos = [];
        for (const o of cleaned) {
          const kind = optionYesNo(o);
          if (kind === "yes") yeses.push(o);
          else if (kind === "no") nos.push(o);
        }
        if (!yeses.length || !nos.length) return null;
        return (want === "yes" ? yeses : nos)[0];
      }
      function parseMoneyish(s) {
        const t = String(s || "").replace(/[$,]/g, "").trim();
        const m = t.match(/\d+(?:\.\d+)?/);
        return m ? parseFloat(m[0]) : null;
      }
      function valueNumber(raw) {
        const t = String(raw || "").replace(/[$,]/g, "");
        const nums = (t.match(/\d+(?:\.\d+)?/g) || []).map(Number);
        return nums.length ? Math.max(...nums) : null;
      }
      function optionSpan(text) {
        const range = text.match(/([$]?\s*\d[\d,]*(?:\.\d+)?)\s*[-–—]\s*([$]?\s*\d[\d,]*(?:\.\d+)?)/);
        const rangeTo = text.match(/([$]?\s*\d[\d,]*(?:\.\d+)?)\s+to\s+([$]?\s*\d[\d,]*(?:\.\d+)?)/i);
        const pair = range || rangeTo;
        if (pair) {
          const lo = parseMoneyish(pair[1]), hi = parseMoneyish(pair[2]);
          if (lo != null && hi != null) return [Math.min(lo, hi), Math.max(lo, hi)];
        }
        const plus = text.match(/([$]?\s*\d[\d,]*(?:\.\d+)?)\s*\+/);
        if (plus) {
          const lo = parseMoneyish(plus[1]);
          if (lo != null) return [lo, Infinity];
        }
        const under = text.match(/(?:under|below|less than|<\s*)\s*([$]?\s*\d[\d,]*(?:\.\d+)?)/i);
        if (under) {
          const hi = parseMoneyish(under[1]);
          if (hi != null) return [0, hi];
        }
        return null;
      }
      function numericBucketPick(cleaned, raw) {
        const want = valueNumber(raw);
        if (want == null) return null;
        const spanned = [];
        for (const o of cleaned) {
          const sp = optionSpan(o);
          if (sp) spanned.push([o, sp]);
        }
        if (spanned.length < 2) return null;
        const hits = [];
        for (const [o, [lo, hi]] of spanned) {
          if (want >= lo && want <= hi) hits.push([o, hi === Infinity ? 1e12 : (hi - lo), lo]);
        }
        if (!hits.length) return null;
        hits.sort((a, b) => a[1] - b[1] || b[2] - a[2]);
        return hits[0][0];
      }
      function pickBest(options, value) {
        let raw = String(value || "").trim();
        if (!raw) return null;
        const cleaned = options.map((o) => String(o || "").replace(/\s+/g, " ").trim())
          .filter((t) => t && !PLACEHOLDER_OPT.test(t));
        if (!cleaned.length) return null;
        // An option that *is* the value wins before any heuristic. Canonicalizing
        // first can destroy a short value outright — "OR" and "IN" are stop
        // words, so they normalized to "" and matched nothing.
        const lower = raw.toLowerCase();
        for (const o of cleaned) if (o.toLowerCase() === lower) return o;
        raw = expandBareState(raw, cleaned);
        const expand = COMMA_ABBR.test(raw) || raw.length === 2 || cleaned.some((o) => COMMA_ABBR.test(o));
        const want = canonical(raw, expand);
        if (!want) return null;
        const ranked = [];
        for (const o of cleaned) {
          const lv = levelsIn(canonical(o, false));
          if (lv.length) ranked.push([o, lv[0]]);
        }
        if (ranked.length >= 2) {
          const valLv = levelsIn(want);
          if (valLv.length) {
            const hit = ranked.find((r) => r[1] === valLv[0]);
            if (hit) return hit[0];
          }
        }
        const gpaHit = gpaRangePick(cleaned, raw);
        if (gpaHit) return gpaHit;
        const bucketHit = dateBucketPick(cleaned, raw);
        if (bucketHit) return bucketHit;
        const years = raw.match(/\b(?:19|20)\d{2}\b/g) || [];
        const yearOpts = cleaned.filter((o) => /^(?:19|20)\d{2}$/.test(o));
        if (years.length && yearOpts.length >= 3) {
          const y = years[years.length - 1];
          const hit = yearOpts.find((o) => o === y);
          if (hit) return hit;
        }
        const ynHit = yesNoPick(cleaned, raw);
        if (ynHit) return ynHit;
        const numHit = numericBucketPick(cleaned, raw);
        if (numHit) return numHit;
        let best = null, bestScore = 0;
        for (const o of cleaned) {
          const oc = canonical(o, expand);
          if (!oc) continue;
          if (oc === want) return o;
          const score = optionScore(want, oc);
          if (score > bestScore) { best = o; bestScore = score; }
        }
        return (best && bestScore >= 0.5) ? best : null;
      }
      window.__applyPickBest = pickBest;

      // Autofill fills blanks. It never overwrites an answer that is already
      // there — one the person typed, one the ATS restored from a draft, or one
      // an earlier pass committed. Tapping ⚡ twice, or an autopilot step that
      // revisits a mounted field, must be safe.
      const PLACEHOLDER_RE = /^\s*(select|choose|pick|none|--+|please select|start typing)\b/i;
      function hasOwnValue(el) {
        const tag = el.tagName.toLowerCase();
        if (tag === "select") {
          const opt = el.selectedOptions && el.selectedOptions[0];
          const txt = opt ? (opt.textContent || "").trim() : "";
          return !!(el.value && txt && !PLACEHOLDER_RE.test(txt));
        }
        if (el.isContentEditable) return !!(el.textContent || "").trim();
        return !!String(el.value || "").trim();
      }

      function setText(el, value) {
        const proto = el.tagName.toLowerCase() === "textarea" ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
        setter.call(el, value);
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
        flash(el);
      }
      const optFor = (el, value) => {
        const texts = [...el.options].map((opt) => (opt.text || opt.value || "").trim());
        const best = pickBest(texts, value);
        if (!best) return null;
        const want = canonical(best, true);
        return [...el.options].find((opt) => canonical((opt.text || opt.value || "").trim(), true) === want)
            || [...el.options].find((opt) => (opt.text || "").trim() === best);
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

      // A combobox opens its own popup list. Typing without clicking an option
      // looks filled and then fails validation — we search, then select.
      const isInputCombobox = (el) => {
        if (el.tagName.toLowerCase() !== "input") return false;
        const role = (el.getAttribute("role") || "").toLowerCase();
        if (role === "combobox") return true;
        const pop = (el.getAttribute("aria-haspopup") || "").toLowerCase();
        if (pop === "listbox" || pop === "true") return true;
        if ((el.getAttribute("aria-autocomplete") || "").toLowerCase() === "list") return true;
        return false;
      };
      const isTypeaheadInput = (el) => {
        if (el.tagName.toLowerCase() !== "input") return false;
        if (isInputCombobox(el)) return true;
        const type = (el.type || "text").toLowerCase();
        if (!["text", "search", ""].includes(type)) return false;
        if (el.getAttribute("list") || el.getAttribute("aria-controls")) return true;
        const ph = (el.placeholder || "").toLowerCase();
        const cls = String(el.className || "").toLowerCase();
        if (/start typing|type to|search for|select a|choose |city or zip|look up/.test(ph)) return true;
        if (/select__input|select-input|typeahead|autosuggest|combobox|autocomplete__/.test(cls)) return true;
        const ac = (el.getAttribute("autocomplete") || "").toLowerCase();
        const key = matchKey(fieldLabel(el), el);
        if (ac === "off" && key && CHOICE_KEYS.has(key)) return true;
        return false;
      };
      function optionText(o) {
        return (o.getAttribute("data-value") || o.textContent || "").replace(/\s+/g, " ").trim();
      }
      function collectOptions() {
        const nodes = [
          ...document.querySelectorAll('[role="option"]'),
          ...document.querySelectorAll('[role="listbox"] li'),
          ...document.querySelectorAll('[id*="-option-"]'),
        ];
        const seen = new Set(), out = [];
        for (const o of nodes) {
          if (seen.has(o)) continue;
          seen.add(o);
          if (o.getAttribute("aria-disabled") === "true") continue;
          const t = optionText(o);
          if (!t || /^(loading|searching|no options|no results?|type to|start typing)/i.test(t)) continue;
          const style = window.getComputedStyle(o);
          if (style.display === "none" || style.visibility === "hidden") continue;
          const r = o.getBoundingClientRect();
          if (r.width === 0 && r.height === 0) continue;
          out.push(o);
        }
        return out;
      }
      async function waitForOptions(ms) {
        const t0 = Date.now();
        while (Date.now() - t0 < (ms || 1100)) {
          const opts = collectOptions();
          if (opts.length) return opts;
          await sleep(70);
        }
        return collectOptions();
      }
      function pickNode(opts, value) {
        const best = pickBest(opts.map(optionText), value);
        if (!best) return null;
        const want = canonical(best, true);
        return opts.find((o) => canonical(optionText(o), true) === want)
            || opts.find((o) => optionText(o) === best)
            || null;
      }
      function searchQueries(value, key) {
        const raw = String(value || "").trim();
        const c = canonical(raw, true);
        const words = c.split(" ").filter((w) => w.length > 2 && !GENERIC_OPT.has(w));
        const qs = [];
        if (key === "location" || key === "city") {
          const city = raw.split(",")[0].trim();
          if (city) qs.push(city);
        } else if (key === "degree") {
          const lv = levelsIn(c)[0];
          if (lv) qs.push(lv === "high school" ? "High School" : lv);
        } else if (key === "school") {
          if (words[0]) qs.push(words[0]);
          if (words.length >= 2) qs.push(words.slice(0, 2).join(" "));
        } else if (key === "country" && c) {
          qs.push(c);
        }
        qs.push(raw);
        return [...new Set(qs.filter(Boolean))];
      }
      async function typeSearch(el, query) {
        el.focus();
        try { el.click(); } catch (e) {}
        try { el.select(); } catch (e) {}
        setText(el, "");
        await sleep(40);
        setText(el, query);
        el.dispatchEvent(new InputEvent("input", { bubbles: true, data: query, inputType: "insertText" }));
        el.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: query.slice(-1) || "a" }));
        el.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true, key: query.slice(-1) || "a" }));
      }
      function commitOption(opt, input) {
        for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
          opt.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
        }
        try { opt.click(); } catch (e) {}
        if (input) {
          input.dispatchEvent(new Event("change", { bubbles: true }));
          try { input.blur(); } catch (e) {}
        }
      }
      async function fillInputCombobox(el, value, key) {
        // A field that *declares* itself a choice field — role=combobox,
        // aria-autocomplete=list, or an aria-controls listbox — stores only what
        // was clicked. Typing into it paints text the person can read back while
        // the field the ATS actually submits stays empty: the form looks complete
        // and arrives blank. So for a declared combobox, "no option matched" must
        // end with an empty box and a skip the person can see, never with text.
        //
        // `sawList` alone can't tell the two cases apart: a typeahead whose popup
        // is empty *because the query matched nothing* looks exactly like a plain
        // text input. The declaration is the reliable signal.
        const declared = isInputCombobox(el) || !!el.getAttribute("aria-controls");
        let sawList = false;
        for (const q of searchQueries(value, key)) {
          await typeSearch(el, q);
          const opts = await waitForOptions(1100);
          if (opts.length) sawList = true;
          const pick = pickNode(opts, value);
          if (pick) {
            commitOption(pick, el);
            await sleep(80);
            flash(el);
            return true;
          }
        }
        if (sawList || declared) {
          // Nothing on the list matched — don't leave typed text that won't save.
          setText(el, "");
          try { el.blur(); } catch (e) {}
          try { el.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); } catch (e) {}
          return false;
        }
        // Never declared itself a combobox and never showed a popup — it really
        // was a plain input (we only guessed from a placeholder or class name).
        setText(el, String(value));
        return true;
      }
      async function fillWidgetCombobox(el, value, key) {
        try { el.click(); } catch (e) {}
        el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
        el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
        await sleep(200);
        const inner = el.querySelector("input:not([type=hidden])");
        if (inner && inner !== el) {
          if (await fillInputCombobox(inner, value, key)) { flash(el); return true; }
        }
        const opts = await waitForOptions(900);
        const pick = pickNode(opts, value);
        if (pick) { commitOption(pick, inner || el); flash(el); return true; }
        try { el.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); } catch (e) {}
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

      // A drafted answer is reused only when the form is really asking that
      // question. The old version scored on raw shared words and accepted a
      // score of 1, so a single filler word in common was enough: "If you could
      // have dinner with anyone, who would it be?" shares "you" with "Why do you
      // want to work here?" and got the why-us paragraph typed into it. Every
      // free-text box on the form ended up holding an answer to a different
      // question — worse than leaving it blank, because it reads as finished.
      //
      // Same tokenizer as app/knowledge.py (_STOPWORDS, words longer than two
      // letters), then: at least two meaningful words in common, and a Dice
      // score over both questions so neither a short label nor a long drafted
      // question can carry a match on its own.
      const ANSWER_STOP = new Set(["a","an","the","and","or","of","to","in","on","at","is","are","be","with","this","that","please","do","does","did","you","your","i","my","me","it","us","our","we"]);
      function answerTokens(text) {
        const out = new Set();
        for (const w of String(text || "").toLowerCase().match(/[a-z0-9]+/g) || []) {
          if (w.length > 2 && !ANSWER_STOP.has(w)) out.add(w);
        }
        return out;
      }
      function bestAnswer(label) {
        const asked = answerTokens(label);
        if (!asked.size) return null;
        let best = null, bestScore = 0;
        for (const q of ANS()) {
          const saved = answerTokens(q.question);
          if (!saved.size) continue;
          let shared = 0;
          for (const w of saved) if (asked.has(w)) shared++;
          if (shared < 2) continue;
          const score = (2 * shared) / (asked.size + saved.size);
          if (score > bestScore) { best = q; bestScore = score; }
        }
        return (best && bestScore >= 0.5) ? best.answer : null;
      }
      window.__applyBestAnswer = bestAnswer;

      const radioOptionText = (r) => {
        const l = String(radioLabel(r) || "").replace(/\s+/g, " ").trim();
        return l || String(r.value || "").trim();
      };
      // A radio group is a choice field, so it goes through the same picker as
      // <select> and the ARIA combobox — pickBest, i.e. fieldmatch.select_value.
      //
      // It used to substring-test each option against the wanted value, and
      // `"yes, i know someone who works here".includes("no")` is true ("know").
      // So a No answer selected Yes and the form submitted the opposite of what
      // the person said, silently, on questions about sponsorship and referrals.
      // The same test also never filled a group whose options aren't yes/no —
      // "Woman" does not appear in "Female" — which pickBest maps correctly.
      //
      // A group the person has already answered is left alone (rule 2: fill
      // blanks, never overwrite). Checking only the target let a second ⚡ tap
      // move their answer.
      function fillRadios(noteSkip) {
        const groups = {};
        for (const r of document.querySelectorAll('input[type="radio"]')) {
          if (!isVisible(r) || r.disabled || !r.name) continue;
          (groups[r.name] = groups[r.name] || []).push(r);
        }
        let n = 0;
        for (const name in groups) {
          const radios = groups[name];
          if (radios.some((r) => r.checked)) continue;        // already answered
          const gl = groupLabel(radios);
          if (EEO.test(gl)) continue;                         // never touch demographics
          const key = matchKey(gl, radios[0]);
          if (!key) { if (noteSkip) noteSkip(gl, "unmatched"); continue; }
          const raw = identityValue(key, gl);
          if (raw == null || raw === "") { if (noteSkip) noteSkip(gl, "empty", key); continue; }
          const want = yesNoWant(gl, key, raw);
          const texts = radios.map(radioOptionText);
          const best = pickBest(texts, want);
          if (!best) { if (noteSkip) noteSkip(gl, "no_option", key); continue; }
          const wantC = canonical(best, true);
          const t = radios.find((r) => canonical(radioOptionText(r), true) === wantC)
                 || radios[texts.indexOf(best)];
          if (!t || t.checked) continue;
          t.checked = true;
          t.dispatchEvent(new Event("click", { bubbles: true }));
          t.dispatchEvent(new Event("change", { bubbles: true }));
          flash(t.closest("label") || t);
          n++;
        }
        return n;
      }
      window.__applyRadioPick = (optionTexts, want) => pickBest(optionTexts, want);

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
          // Case-insensitive: boards that render the buttons as YES / NO left
          // both words glued onto the question text the rules then matched on.
          const q = raw.replace(/\byes\b/gi, "").replace(/\bno\b/gi, "").replace(/\s+/g, " ").trim();
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
          const key = matchKey(label, parent);
          const raw = key && identityValue(key, label);
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
      // The one place a fill result reaches the app. Subframes never call it.
      function reportFill(r) {
        if (!IS_TOP) return;
        try {
          window.webkit.messageHandlers.applyfill.postMessage({
            filled: r.filled, essays: r.essays, skips: r.skips || [],
            rules: RULES_SRC, status: "filled",
            url: (location && location.href) || "",
          });
        } catch (e) {}
      }

      // Pass `{ report: false }` when a caller is aggregating across frames and
      // will report the total itself.
      window.__applyAutofill = async function (opts) {
        let filled = 0, essays = 0;
        const skips = [];
        const skipSeen = new Set();
        function noteSkip(label, reason, key, extra) {
          const t = String(label || "").replace(/\s+/g, " ").trim().slice(0, 160);
          if (!t || t.length < 4) return;
          if (EEO.test(t)) return;
          if (/^(search|select|choose|type here|start typing)/i.test(t)) return;
          const sig = reason + "\n" + t.toLowerCase();
          if (skipSeen.has(sig) || skips.length >= 40) return;
          skipSeen.add(sig);
          const row = { label: t, reason };
          if (key) row.key = key;
          if (extra) row.detail = String(extra).slice(0, 200);
          skips.push(row);
        }
        // Pass 1 — typeaheads first (async popup resolve, then click an option).
        for (const el of document.querySelectorAll("input")) {
          if (!isVisible(el) || el.disabled || el.readOnly || !isTypeaheadInput(el)) continue;
          const label = fieldLabel(el);
          if (EEO.test(label)) continue;
          if (hasOwnValue(el)) continue;   // already answered — never retype it
          const key = matchKey(label, el);
          if (!key) { noteSkip(label, "unmatched"); continue; }
          const val = identityValue(key, label);
          if (val == null || val === "") { noteSkip(label, "empty", key); continue; }
          if (await fillInputCombobox(el, val, key)) filled++;
          else { essays++; noteSkip(label, "no_option", key); }
        }
        // Pass 1b — Ashby/custom <div role="combobox"> widgets (not inputs).
        for (const el of document.querySelectorAll('[role="combobox"]')) {
          if (el.tagName.toLowerCase() === "input") continue;
          if (!isVisible(el) || el.getAttribute("aria-disabled") === "true") continue;
          const label = fieldLabel(el);
          if (EEO.test(label)) continue;
          const key = matchKey(label, el);
          if (!key) { noteSkip(label, "unmatched"); continue; }
          const val = identityValue(key, label);
          if (val == null || val === "") { noteSkip(label, "empty", key); continue; }
          const cur = (el.getAttribute("data-selected") || el.textContent || "").replace(/\s+/g, " ").trim().toLowerCase();
          if (cur && !/^(select|choose|pick)\b/.test(cur)) {
            const want = String(val).toLowerCase();
            if (cur === want || cur.includes(want) || want.includes(cur)) continue;
          }
          if (await fillWidgetCombobox(el, val, key)) filled++;
          else { essays++; noteSkip(label, "no_option", key); }
        }
        // Pass 2 — plain inputs, textareas, native selects, contenteditable essays.
        for (const el of document.querySelectorAll('input, textarea, select, [contenteditable="true"]')) {
          if (!isVisible(el) || el.disabled || el.readOnly) continue;
          const tag = el.tagName.toLowerCase(), type = (el.type||"").toLowerCase();
          if (["radio","checkbox","file","hidden","submit","button","date","datetime-local","password"].includes(type)) continue;
          if (isTypeaheadInput(el)) continue;
          const label = fieldLabel(el);
          if (EEO.test(label)) continue;
          if (hasOwnValue(el)) continue;   // already answered — never overwrite it
          const editable = el.isContentEditable;
          if (tag === "textarea" || editable) {
            const a = bestAnswer(label);
            if (a) { editable ? setEditable(el, a) : setText(el, a); filled++; }
            else { essays++; noteSkip(label, "essay"); }
            continue;
          }
          const key = matchKey(label, el);
          if (!key) { noteSkip(label, "unmatched"); continue; }
          const val = identityValue(key, label);
          if (val == null || val === "") { noteSkip(label, "empty", key); continue; }
          if (tag === "select") {
            if (setSelect(el, val)) filled++;
            else noteSkip(label, "no_option", key);
            continue;
          }
          if (el.list) {
            const texts = [...el.list.options].map((o) => (o.value || o.text || "").trim());
            const best = pickBest(texts, val);
            if (best) { setText(el, best); filled++; continue; }
            noteSkip(label, "no_option", key);
            continue;
          }
          setText(el, val); filled++;
        }
        filled += fillRadios(noteSkip);
        filled += fillYesNoButtons();
        const result = { filled, essays, skips };
        if (!(opts && opts.report === false)) reportFill(result);
        return result;
      };

      // Careers pages often put the real ATS form in a cross-origin iframe, and
      // native evaluateJavaScript only reaches the top frame. So the top frame
      // pings each child; each child fills its own document, fans out to *its*
      // children in turn, and answers with a running total. The old version fired
      // the pings and returned immediately, so the count it handed back was the
      // top frame's alone — usually zero on exactly the pages that need the hop.
      //
      // Frames that can never hold a form burn the wait budget for nothing.
      const SKIP_FRAME_SRC = /^\s*$|^about:blank|^javascript:|recaptcha|hcaptcha|turnstile|funcaptcha|arkose|challenge-platform|captcha-delivery|datadome|perimeterx|gstatic\.com|googletagmanager|google-analytics|doubleclick|facebook\.com|\.js(\?|$)/i;
      // A frame answers a ping twice: an ack straight away, then the result when
      // it has finished. Filling a real ATS form means resolving typeaheads one
      // option at a time and can take twenty seconds, so a single short deadline
      // either cuts the embed off mid-fill (the form fills, the app reports zero)
      // or makes every page wait on frames that were never going to answer. The
      // ack separates the two: no ack, drop it fast; acked, give it real time.
      const FRAME_ACK_MS = 700;
      const FRAME_FILL_MS = 30000;
      const _pending = {};
      let _rid = 0;

      async function fillTree() {
        const own = await window.__applyAutofill({ report: false });
        const total = { filled: own.filled, essays: own.essays, skips: (own.skips || []).slice() };
        const waits = [];
        for (const f of document.querySelectorAll("iframe")) {
          if (SKIP_FRAME_SRC.test(f.getAttribute("src") || "")) continue;
          if (!f.contentWindow) continue;
          const rid = "r" + ++_rid + "." + Math.random().toString(36).slice(2, 8);
          waits.push(new Promise((resolve) => {
            let done = false;
            const finish = (d) => {
              if (done) return;
              done = true;
              delete _pending[rid];
              resolve(d || null);
            };
            let timer = setTimeout(() => finish(null), FRAME_ACK_MS);
            _pending[rid] = (d) => {
              if (d.__apply === "ack") {          // engine is there and working
                clearTimeout(timer);
                timer = setTimeout(() => finish(null), FRAME_FILL_MS);
                return;
              }
              clearTimeout(timer);
              finish(d);
            };
            try { f.contentWindow.postMessage({ __apply: "fill", rid }, "*"); }
            catch (e) { finish(null); }
          }));
        }
        for (const d of await Promise.all(waits)) {
          if (!d) continue;
          total.filled += d.filled || 0;
          total.essays += d.essays || 0;
          for (const sk of d.skips || []) if (total.skips.length < 40) total.skips.push(sk);
        }
        return total;
      }

      window.addEventListener("message", async (e) => {
        const d = e.data;
        if (!d || typeof d !== "object") return;
        if (d.__apply === "fill") {
          // The top frame is driven natively and never takes a ping — otherwise
          // any ad frame on the page could start a fill and read the reply.
          if (IS_TOP) return;
          const reply = e.source || window.parent;
          try { reply.postMessage({ __apply: "ack", rid: d.rid }, "*"); } catch (err) {}
          const total = await fillTree();
          try {
            reply.postMessage({
              __apply: "filled", rid: d.rid, filled: total.filled,
              essays: total.essays, skips: total.skips,
            }, "*");
          } catch (err) {}
          return;
        }
        // Only answers to a ping we actually sent.
        if ((d.__apply === "filled" || d.__apply === "ack")
            && d.rid && _pending[d.rid]) _pending[d.rid](d);
      });

      window.__applyAutofillAll = async function () {
        const total = await fillTree();
        reportFill(total);
        return total;
      };

      // Careers pages wrap GH/Lever/Ashby/Workable/SR in an iframe. Native
      // evaluateJavaScript only hits the top frame, so we hop into that embed.
      // Never hop into a CAPTCHA / challenge frame.
      window.__applyFindApplyEmbed = function () {
        const skip = /recaptcha|hcaptcha|turnstile|funcaptcha|arkose|challenge-platform|px-captcha/i;
        const ats = /greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|smartrecruiters\.com/i;
        for (const f of document.querySelectorAll("iframe[src]")) {
          try {
            const u = new URL(f.getAttribute("src"), location.href).href;
            if (!u || skip.test(u)) continue;
            if (ats.test(u)) return u;
          } catch (e) {}
        }
        return null;
      };

      // --- form probe + Simplify-style autopilot (fill → Next → refill) -----
      const ADVANCE_RE = /^\s*(next|continue|save\s*&\s*continue|review(\s+application)?|proceed|forward|keep going)\s*$|\b(next step|continue to|go to next)\b/i;
      const NOT_ADVANCE_RE = /submit|send application|finish|complete application|cancel|back|previous|save(?!\s*&\s*continue)|add another|upload|sign\s*in|log\s*in|create account|register/i;
      const SUBMIT_RE = /submit(\s+application)?|send\s+application|finish\s+application|complete\s+application/i;
      // What the button that opens the application actually says, across the ATSs
      // we see. SmartRecruiters labels it "I'm interested", which matched nothing
      // here — so every SmartRecruiters posting dead-ended on "No application
      // form on this page" with the button sitting right there.
      const REVEAL_RE = /apply\s+(for|to)\s+this(\s+(job|position|role|opening|opportunity))?|apply\s+now|^\s*apply\s*$|start\s+(your\s+)?application|begin\s+application|i\s*'?\s*m\s+interested|^\s*interested\s*$/i;
      const LOGIN_RE = /sign\s*in|log\s*in|create\s+account|register|forgot\s+password/i;
      const CAPTCHA_RE = /recaptcha|hcaptcha|captcha|cf-turnstile|challenge-platform|px-captcha|funcaptcha|arkose/i;
      const SIGNAL = new Set(["email","first_name","last_name","full_name","phone","linkedin","work_authorized","needs_sponsorship"]);

      function btnText(el) {
        return (el.innerText || el.textContent || el.getAttribute("aria-label") || el.value || "")
          .replace(/\s+/g, " ").trim().slice(0, 80);
      }
      // An invisible, score-based reCAPTCHA sits on almost every modern ATS form
      // — Greenhouse puts one on every posting it serves. It asks nothing of the
      // person and blocks nothing, so matching on "the string recaptcha appears
      // somewhere" meant the app declared "CAPTCHA in the way" over a perfectly
      // fillable form and refused to touch it. Only a challenge a human can
      // actually see counts: the image-challenge popup, or a checkbox widget
      // that is really rendered and is not the invisible variant.
      function isRendered(el, min) {
        try {
          const cs = getComputedStyle(el);
          if (cs.display === "none" || cs.visibility === "hidden" || cs.opacity === "0") return false;
          const r = el.getBoundingClientRect();
          return r.width >= min && r.height >= min;
        } catch (e) { return false; }
      }
      const CAPTCHA_CHALLENGE_SRC = /\/(bframe|challenge)|challenge-platform|px-captcha|funcaptcha|arkose|captcha-delivery|datadome|perimeterx/i;
      function detectCaptcha() {
        // A widget the page declares for itself — reCAPTCHA v2, hCaptcha,
        // Turnstile — is a challenge someone has to tick, unless it turns out to
        // be running in invisible mode. Two ways to tell it is:
        //   • it says so (data-size="invisible"), or
        //   • its script has already drawn itself (it holds an iframe) and what
        //     it drew has no height. Lever does this on every application page:
        //     an .h-captcha with two child iframes and zero height. Reading the
        //     bare declaration as a challenge made the app refuse to fill 49
        //     visible fields on a form nobody was being challenged for.
        // A widget that has not drawn anything yet is still taken at its word —
        // absence of a render is not evidence of invisibility.
        for (const n of document.querySelectorAll(
            ".g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey]")) {
          if ((n.getAttribute("data-size") || "").toLowerCase() === "invisible") continue;
          if (n.querySelector("iframe") && !isRendered(n, 40)) continue;
          return true;
        }
        // Otherwise only a challenge actually on screen counts. All an invisible,
        // score-based reCAPTCHA puts in the page is its anchor badge, and that
        // asks nothing of anybody.
        for (const f of document.querySelectorAll("iframe")) {
          if (!CAPTCHA_CHALLENGE_SRC.test(f.getAttribute("src") || "")) continue;
          if (isRendered(f, 80)) return true;
        }
        return false;
      }
      function postDrive(msg) {
        if (!IS_TOP) return;
        try { window.webkit.messageHandlers.applyfill.postMessage(msg); } catch (e) {}
      }

      // One-shot Fill: never type into a login wall or CAPTCHA page.
      window.__applyFillOrPause = async function () {
        const p = typeof window.__applyFormProbe === "function" ? window.__applyFormProbe() : null;
        if (p && (p.kind === "login" || p.kind === "captcha")) {
          postDrive({
            status: "needsHuman", blocker: p.blockerReason || p.kind,
            probe: p, filled: 0, essays: 0, step: 0,
          });
          return 0;
        }
        const runner = window.__applyAutofillAll || window.__applyAutofill;
        if (typeof runner === "function") {
          const r = await runner();
          return typeof r === "number" ? r : (r && r.filled) || 0;
        }
        return 0;
      };

      window.__applyFormProbe = function () {
        const labels = [];
        const matched = [];
        let hasPassword = false, hasFile = false;
        for (const el of document.querySelectorAll("input, textarea, select")) {
          if (!isVisible(el)) continue;
          const t = (el.type || "").toLowerCase();
          if (["hidden","submit","button","image","reset"].includes(t)) continue;
          if (t === "password") { hasPassword = true; continue; }
          if (t === "file") hasFile = true;
          const label = fieldLabel(el);
          labels.push(label);
          const key = matchKey(label, el);
          if (key && !matched.includes(key)) matched.push(key);
        }
        if (detectCaptcha()) {
          return { kind: "captcha", score: 0, fillableCount: 0, matchedKeys: [],
            advanceLabel: null, submitVisible: false, revealLabel: null,
            captcha: true, blockerReason: "CAPTCHA or bot check in the way" };
        }
        const buttons = [];
        for (const el of document.querySelectorAll("button, a[href], [role=button], input[type=submit], input[type=button]")) {
          if (!isVisible(el)) continue;
          const t = btnText(el);
          if (t) buttons.push({ el, t });
        }
        if (hasFile && !matched.includes("resume")) matched.push("resume");
        let advanceLabel = null, submitVisible = false, revealLabel = null;
        for (const { t } of buttons) {
          if (SUBMIT_RE.test(t)) submitVisible = true;
          if (ADVANCE_RE.test(t) && !NOT_ADVANCE_RE.test(t)) advanceLabel = advanceLabel || t;
          if (REVEAL_RE.test(t) && !SUBMIT_RE.test(t)) revealLabel = revealLabel || t;
        }
        // A "Log in" link in the site nav is not a login wall. Nearly every
        // company careers page carries one, and counting it made the app tell
        // people to sign in manually while an Apply button sat right there. A
        // wall wants credentials *to continue*: a visible password box, or a
        // login control that is part of the page's own content rather than its
        // chrome — and never one offered alongside an Apply button.
        let loginish = hasPassword;
        if (!loginish) {
          for (const { el, t } of buttons) {
            if (!LOGIN_RE.test(t) || el.closest("nav, header, footer")) continue;
            loginish = true;
            break;
          }
          if (loginish && revealLabel) loginish = false;
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
          advanceLabel, submitVisible, revealLabel, captcha: false, blockerReason: null };
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

      // A page whose top frame has no fields has not necessarily got no form —
      // on Greenhouse, Workable and most careers sites the form is in an embed.
      // A rendered, non-noise iframe is reason enough to try filling.
      function hasEmbedFrames() {
        for (const f of document.querySelectorAll("iframe")) {
          if (SKIP_FRAME_SRC.test(f.getAttribute("src") || "")) continue;
          if (isRendered(f, 120)) return true;
        }
        return false;
      }
      const MAX_REVEAL_CLICKS = 2;

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
        // Watch a login/CAPTCHA page until it clears. Does not fill.
        if (opts.mode === "watch") {
          if (ctrl.running) return { status: "watching" };
          ctrl.paused = false; ctrl.running = true;
          let probe = window.__applyFormProbe();
          if (probe.kind !== "captcha" && probe.kind !== "login") {
            postDrive({ status: "probe", probe, filled: 0, essays: 0, step: 0 });
            ctrl.running = false;
            return probe;
          }
          // Say it once. Re-posting the same blocker every 1.5s for as long as
          // the person is solving it churns SwiftUI state for no new information.
          let said = probe.blockerReason || probe.kind;
          postDrive({ status: "needsHuman", blocker: said,
            probe, filled: 0, essays: 0, step: 0 });
          while (ctrl.running && !ctrl.paused) {
            await sleep(1500);
            probe = window.__applyFormProbe();
            if (probe.kind !== "captcha" && probe.kind !== "login") {
              postDrive({ status: "watchingClear", probe, filled: 0, essays: 0, step: 0, blocker: null });
              ctrl.running = false;
              return { status: "watchingClear", probe };
            }
            const now = probe.blockerReason || probe.kind;
            if (now !== said) {
              said = now;
              postDrive({ status: "needsHuman", blocker: said,
                probe, filled: 0, essays: 0, step: 0 });
            }
          }
          ctrl.running = false;
          return { status: ctrl.paused ? "paused" : "needsHuman", probe };
        }

        ctrl.paused = false; ctrl.running = true;
        let step = 0, totalFilled = 0, totalEssays = 0, revealClicks = 0;

        while (ctrl.running && !ctrl.paused && step < maxSteps) {
          let probe = window.__applyFormProbe();

          if (probe.kind === "captcha" || probe.kind === "login") {
            let said = probe.blockerReason || probe.kind;
            postDrive({ status: "needsHuman", blocker: said,
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
              const now = probe.blockerReason || probe.kind;
              if (now !== said) {
                said = now;
                postDrive({ status: "needsHuman", blocker: said,
                  probe, filled: totalFilled, essays: totalEssays, step });
              }
            }
            return { status: ctrl.paused ? "paused" : "needsHuman", probe };
          }

          // Try to fill before deciding there is nothing here — the fill fans out
          // into embeds, and the probe only ever sees the top frame. Clicking
          // "Apply now" on the strength of the top frame alone used to loop the
          // maximum number of steps on a live Greenhouse page and then report
          // "ready" over a form it had never touched.
          if (probe.kind === "unknown" && probe.fillableCount === 0
              && !probe.revealLabel && !hasEmbedFrames()) {
            postDrive({ status: "failed", blocker: "No application form on this page",
              filled: totalFilled, essays: totalEssays, step, probe });
            ctrl.running = false;
            return { status: "failed", probe };
          }

          postDrive({ status: "filling", step, filled: totalFilled, essays: totalEssays, probe });
          let filled = 0;
          const runner = window.__applyAutofillAll || window.__applyAutofill;
          if (typeof runner === "function") {
            const r = await runner();
            filled = typeof r === "number" ? r : (r && r.filled) || 0;
          }
          totalFilled += filled || 0;

          // Nothing to fill anywhere, but the page offers a way in. Open it —
          // twice at most, so a button that isn't really a reveal can't spin.
          if (filled === 0 && probe.revealLabel && revealClicks < MAX_REVEAL_CLICKS) {
            const rev = findRevealEl();
            if (rev) {
              revealClicks++;
              postDrive({ status: "advancing", step, filled: totalFilled,
                essays: totalEssays, detail: "reveal" });
              stepVeil(); rev.click(); await sleep(1800); step++; continue;
            }
          }

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
