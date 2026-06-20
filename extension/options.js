/* Options page: persists connection settings locally and syncs the applicant
 * identity to the server (/apply/identity). */
const CONN = ["baseUrl", "user", "token", "postingId"];
const TEXT = ["first_name", "last_name", "email", "phone", "city", "state",
  "country", "linkedin", "github", "portfolio", "school", "grad_year",
  "years_experience"];
const BOOL = ["work_authorized", "needs_sponsorship"];
const $ = (id) => document.getElementById(id);

function api(method, path, cfg, body) {
  const headers = { "Content-Type": "application/json" };
  if (cfg.token) headers["X-Apply-Token"] = cfg.token;
  return fetch(cfg.baseUrl.replace(/\/$/, "") + path, {
    method, headers, body: body ? JSON.stringify(body) : undefined,
  });
}

// Load saved connection settings, then pull the identity from the server.
chrome.storage.sync.get(
  { baseUrl: "", user: "", token: "", postingId: "" },
  async (cfg) => {
    CONN.forEach((k) => ($(k).value = cfg[k] || ""));
    if (!cfg.baseUrl || !cfg.user) return;
    try {
      const r = await api("GET", `/apply/identity?user=${encodeURIComponent(cfg.user)}`, cfg);
      const f = (await r.json()).fields || {};
      TEXT.forEach((k) => ($(k).value = f[k] || ""));
      BOOL.forEach((k) => ($(k).checked = String(f[k]).toLowerCase() === "yes" || f[k] === true));
    } catch (e) { /* server may be unreachable; leave identity blank */ }
  }
);

$("save").addEventListener("click", async () => {
  const cfg = {};
  CONN.forEach((k) => (cfg[k] = $(k).value.trim()));
  chrome.storage.sync.set(cfg);

  const fields = {};
  TEXT.forEach((k) => (fields[k] = $(k).value.trim()));
  BOOL.forEach((k) => (fields[k] = $(k).checked));

  let msg = "Saved locally.";
  if (cfg.baseUrl && cfg.user) {
    try {
      const r = await api("POST", "/apply/identity", cfg, { user: cfg.user, fields });
      msg = r.ok ? "Saved + synced to server ✓" : `Saved locally (server ${r.status})`;
    } catch (e) { msg = "Saved locally (server unreachable)"; }
  }
  $("status").textContent = msg;
  setTimeout(() => ($("status").textContent = ""), 4000);
});
