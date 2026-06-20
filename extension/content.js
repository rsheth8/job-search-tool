/* Job Apply Autofill — content script.
 *
 * As you focus a field on a job-application page, this offers an inline chip with
 * the right value from your job-search-tool profile (name, email, links, work
 * authorization, …) or, for free-text questions, a "Draft answer" that calls your
 * Haiku-backed /apply/answer endpoint. You click to accept — nothing is ever
 * filled or submitted automatically.
 *
 * Config (base URL / user / token / posting id) is set in the extension options.
 */
(() => {
  "use strict";

  // Map an identity key -> regexes that identify the field from its label/name.
  const RULES = [
    ["first_name", /first.?name|given.?name|legal first/i],
    ["last_name", /last.?name|family.?name|surname/i],
    ["full_name", /full.?name|^\s*name\s*$|your name|preferred name/i],
    ["email", /e-?mail/i],
    ["phone", /phone|mobile|tel(ephone)?/i],
    ["linkedin", /linked.?in/i],
    ["github", /git.?hub/i],
    ["portfolio", /portfolio|personal (web)?site|website|url/i],
    ["city", /\bcity\b|town/i],
    ["state", /\bstate\b|province|region/i],
    ["country", /country/i],
    ["school", /school|university|college|institution/i],
    ["grad_year", /grad(uation)?.{0,6}(year|date)/i],
    ["years_experience", /years.{0,8}experience|experience.{0,8}years|yoe/i],
    ["work_authorized", /authori[sz]ed to work|work authori[sz]ation|legally.{0,12}work|eligible to work/i],
    ["needs_sponsorship", /sponsor(ship)?|require.{0,12}visa|visa.{0,12}status/i],
  ];

  let CFG = null;
  let IDENTITY = {};
  let chip = null;
  let chipFor = null;

  init();

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
    document.addEventListener("focusin", onFocus, true);
    document.addEventListener("scroll", reposition, true);
    window.addEventListener("resize", reposition, true);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideChip(); });
  }

  // ---- field handling -----------------------------------------------------

  function onFocus(e) {
    const el = e.target;
    if (!isFillable(el)) return hideChip();
    const label = fieldLabel(el).toLowerCase();
    const key = matchKey(label);

    if (key && IDENTITY[key] != null && IDENTITY[key] !== "") {
      showChip(el, `Fill: ${truncate(IDENTITY[key], 40)}`, () => fill(el, IDENTITY[key]));
    } else if (isFreeText(el, label)) {
      showChip(el, "✨ Draft answer", () => draftAnswer(el, fieldLabel(el)));
    } else {
      hideChip();
    }
  }

  function isFillable(el) {
    if (!el || !el.tagName) return false;
    const tag = el.tagName.toLowerCase();
    if (tag === "textarea") return true;
    if (tag !== "input") return false;
    const t = (el.type || "text").toLowerCase();
    return ["text", "email", "tel", "url", "search", ""].includes(t);
  }

  function isFreeText(el, label) {
    if (el.tagName.toLowerCase() === "textarea") return true;
    return label.length > 40 || label.includes("?");
  }

  // Best label text for a field: <label for>, aria-label, placeholder, name, id.
  function fieldLabel(el) {
    const bits = [];
    if (el.id) {
      const lab = document.querySelector(`label[for="${cssEscape(el.id)}"]`);
      if (lab) bits.push(lab.textContent);
    }
    const wrapLabel = el.closest("label");
    if (wrapLabel) bits.push(wrapLabel.textContent);
    if (el.getAttribute("aria-label")) bits.push(el.getAttribute("aria-label"));
    const labelledby = el.getAttribute("aria-labelledby");
    if (labelledby) {
      labelledby.split(/\s+/).forEach((id) => {
        const n = document.getElementById(id);
        if (n) bits.push(n.textContent);
      });
    }
    if (el.placeholder) bits.push(el.placeholder);
    bits.push(el.name || "", el.id || "");
    return bits.join(" ").replace(/\s+/g, " ").trim();
  }

  function matchKey(label) {
    for (const [key, re] of RULES) if (re.test(label)) return key;
    return null;
  }

  // ---- filling (React-safe) ----------------------------------------------

  function fill(el, value) {
    const proto = el.tagName.toLowerCase() === "textarea"
      ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.focus();
    flash(el);
    hideChip();
  }

  async function draftAnswer(el, question) {
    setChipBusy("Drafting…");
    try {
      const r = await api("POST", "/apply/answer", {
        user: CFG.user,
        posting_id: CFG.postingId || undefined,
        question,
        company: guessCompany(),
        jd: "",
      });
      const { answer } = await r.json();
      if (answer) fill(el, answer);
    } catch (e) {
      console.warn("[apply-autofill] draft failed:", e);
      setChipBusy("Draft failed — retry?");
    }
  }

  // ---- chip UI ------------------------------------------------------------

  function showChip(el, text, onClick) {
    hideChip();
    chip = document.createElement("button");
    chip.className = "jaf-chip";
    chip.type = "button";
    chip.textContent = text;
    chip.addEventListener("mousedown", (ev) => {
      ev.preventDefault(); // keep focus on the field
      onClick();
    });
    document.body.appendChild(chip);
    chipFor = el;
    positionChip();
  }

  function setChipBusy(text) { if (chip) chip.textContent = text; }

  function positionChip() {
    if (!chip || !chipFor) return;
    const r = chipFor.getBoundingClientRect();
    chip.style.top = `${window.scrollY + r.top - 30}px`;
    chip.style.left = `${window.scrollX + r.left}px`;
  }

  function reposition() { if (chip) positionChip(); }

  function hideChip() {
    if (chip) chip.remove();
    chip = null;
    chipFor = null;
  }

  function flash(el) {
    el.classList.add("jaf-filled");
    setTimeout(() => el.classList.remove("jaf-filled"), 700);
  }

  // ---- helpers ------------------------------------------------------------

  function api(method, path, body) {
    const headers = { "Content-Type": "application/json" };
    if (CFG.token) headers["X-Apply-Token"] = CFG.token;
    return fetch(CFG.baseUrl.replace(/\/$/, "") + path, {
      method, headers, body: body ? JSON.stringify(body) : undefined,
    }).then((r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r;
    });
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
        { baseUrl: "", user: "", token: "", postingId: "" },
        (cfg) => resolve(cfg)
      );
    });
  }

  function cssEscape(s) { return (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/"/g, '\\"'); }
})();
