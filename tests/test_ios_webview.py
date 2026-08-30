"""Contracts the iOS WebView must keep: persistent cookies, probe-driven Fill."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEBVIEW = ROOT / "ios" / "JobPilot" / "WebView.swift"
AUTOFILL = ROOT / "ios" / "JobPilot" / "Autofill.swift"


def test_webview_uses_the_persistent_cookie_store():
    src = WEBVIEW.read_text()
    assert "WKWebsiteDataStore.default()" in src
    assert "nonPersistent" not in src
    assert "cfg.websiteDataStore = ApplyBrowserSession.dataStore" in src
    assert "__applyFindApplyEmbed" in src
    assert "__applyFillOrPause" in src
    assert "mode: 'watch'" in src


def test_autofill_lib_exposes_embed_probe_and_watch():
    src = AUTOFILL.read_text()
    assert "window.__applyFindApplyEmbed" in src
    assert "window.__applyFillOrPause" in src
    assert 'opts.mode === "watch"' in src
    assert r"workable\.com" in src
    assert r"smartrecruiters\.com" in src


def test_only_the_top_frame_talks_to_the_native_side():
    """Both user scripts are injected with `forMainFrameOnly: false`, so every
    about:blank / reCAPTCHA / analytics frame runs the engine — and they all post
    into the SAME native handler. Every route to that handler must therefore be
    behind the IS_TOP check, or a noise frame's zero lands on top of the real
    result and the app reports "No fields matched" over a form it just filled."""
    src = AUTOFILL.read_text()
    assert "const IS_TOP =" in src
    posts = [ln.strip() for ln in src.splitlines()
             if "messageHandlers.applyfill.postMessage" in ln and not ln.lstrip().startswith("///")]
    assert len(posts) == 2, f"new route to the native handler — is it top-only? {posts}"
    for fn in ("function reportFill(r) {", "function postDrive(msg) {"):
        body = src.split(fn, 1)[1].split("\n      }", 1)[0]
        assert "if (!IS_TOP) return;" in body, f"{fn} can post from a subframe"
