/* Popup: show whether the extension is configured + a shortcut to settings. */
const state = document.getElementById("state");

chrome.storage.sync.get({ baseUrl: "", user: "" }, (cfg) => {
  if (cfg.baseUrl && cfg.user) {
    state.textContent = `Connected as “${cfg.user}”`;
    state.className = "status ok";
  } else {
    state.textContent = "Not configured — open settings";
    state.className = "status warn";
  }
});

document.getElementById("opts").addEventListener("click", () => {
  chrome.runtime.openOptionsPage();
  window.close();
});
