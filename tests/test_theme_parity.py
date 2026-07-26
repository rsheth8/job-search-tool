"""The design tokens exist in three places; this stops them drifting apart again.

Before there was a shared system, each surface had grown its own palette and they had
quietly diverged into three near-identical blues (#6ea8fe / #6ea8ff / #2563eb). The
fix was app/theme.py, but Python can't style a Chrome extension or a SwiftUI view, so
copies exist — and copies drift. Same reasoning as tests/test_rules_parity.py: the
Python is the source of truth and everything else is pinned against it.
"""
from __future__ import annotations

import re
from pathlib import Path

from app import theme

_ROOT = Path(__file__).resolve().parent.parent
_EXT_CSS = _ROOT / "extension" / "theme.css"
_OVERLAY = _ROOT / "extension" / "overlay.css"
_SWIFT = _ROOT / "ios" / "Apply" / "Theme.swift"

# The tokens a surface can't get wrong without looking like a different product.
_CHECKED = ("bg", "panel", "line", "ink", "dim", "accent", "ok", "warn", "bad")


def _vars_in(block: str) -> dict[str, str]:
    return {m.group(1): m.group(2).strip().upper()
            for m in re.finditer(r"--([a-z0-9-]+)\s*:\s*([^;]+);", block)}


def _mode_blocks(css: str) -> tuple[dict[str, str], dict[str, str]]:
    """(light, dark) token maps. Light is the first :root; dark is whichever block
    sits inside the prefers-color-scheme media query."""
    dark_match = re.search(r"@media\s*\(prefers-color-scheme:\s*dark\)\s*\{(.+?)\n\}",
                           css, re.S)
    assert dark_match, "no dark-mode block found"
    dark = _vars_in(dark_match.group(1))
    light = _vars_in(css[:dark_match.start()])
    return light, dark


def test_extension_tokens_match_the_python():
    py_light, py_dark = _mode_blocks(theme.CSS)
    ext_light, ext_dark = _mode_blocks(_EXT_CSS.read_text())

    for name in _CHECKED:
        assert name in py_light, f"--{name} missing from app/theme.py light"
        assert ext_light.get(name) == py_light[name], (
            f"--{name} light: extension has {ext_light.get(name)}, "
            f"Python has {py_light[name]}")
        assert ext_dark.get(name) == py_dark[name], (
            f"--{name} dark: extension has {ext_dark.get(name)}, "
            f"Python has {py_dark[name]}")


def test_every_token_is_defined_in_both_modes():
    """A token that only exists in one mode silently inherits the other's value —
    invisible in whichever mode you happen to be developing in."""
    light, dark = _mode_blocks(theme.CSS)
    # Structural tokens (radius, font, shadow) legitimately don't change with mode.
    colour_only = {k for k in light if not k.startswith(("radius", "font"))}
    missing = {k for k in colour_only if k not in dark and not k.startswith("shadow")}
    assert not missing, f"defined for light but not dark: {sorted(missing)}"


def test_overlay_uses_the_brand_accent_literally():
    """overlay.css is injected into third-party pages, so it can't use var() — a host
    site defining --accent would repaint our chip. It must still be the brand colour,
    which means the literal has to be checked rather than assumed."""
    _, py_dark = _mode_blocks(theme.CSS)
    raw = _OVERLAY.read_text()
    # Strip comments first — the file *explains* why it avoids var(), and matching
    # that prose instead of a declaration is a false positive.
    declarations = re.sub(r"/\*.*?\*/", "", raw, flags=re.S).upper()
    assert py_dark["accent"] in declarations, (
        f"overlay.css should use the brand accent {py_dark['accent']}")
    assert "VAR(--" not in declarations, (
        "overlay.css must not use CSS variables — a host page could override them")


def test_ios_theme_matches_the_python():
    """The iOS colours are hand-written hex in Theme.swift; pin them the same way."""
    py_light, py_dark = _mode_blocks(theme.CSS)
    swift = _SWIFT.read_text()
    # `static let accent = Palette(light: 0x0B7373, dark: 0x2ED9D9)`
    found = {m.group(1): (m.group(2).upper(), m.group(3).upper())
             for m in re.finditer(
                 r"static let (\w+)\s*=\s*Palette\(light:\s*0x([0-9A-Fa-f]{6})\s*,\s*"
                 r"dark:\s*0x([0-9A-Fa-f]{6})\s*\)", swift)}
    assert found, "no Palette definitions parsed out of Theme.swift"
    for name in _CHECKED:
        swift_name = {"bg": "background", "panel": "panel", "line": "line",
                      "ink": "ink", "dim": "dim", "accent": "accent",
                      "ok": "ok", "warn": "warn", "bad": "bad"}[name]
        assert swift_name in found, f"Theme.swift is missing {swift_name}"
        light_hex, dark_hex = found[swift_name]
        assert "#" + light_hex == py_light[name], (
            f"{swift_name} light: Swift #{light_hex} vs Python {py_light[name]}")
        assert "#" + dark_hex == py_dark[name], (
            f"{swift_name} dark: Swift #{dark_hex} vs Python {py_dark[name]}")
