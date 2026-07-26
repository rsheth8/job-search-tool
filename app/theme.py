"""One palette for every surface, in both light and dark.

Before this, each web page carried its own hardcoded colours: the dashboard was
light, `/apply` was dark, `/train` was a *different* dark, and the extension chip a
third blue (#6ea8fe vs #6ea8ff vs #2563eb — near-identical, all separately authored).
Nothing shared, so nothing could be changed in one place, and no surface could switch
modes at all.

The fix is semantic tokens. Pages never name a colour; they say what a thing *is*
(`var(--ink)`, `var(--accent)`) and the mode decides what that means. Light is the
default and dark overrides it twice:

  * ``@media (prefers-color-scheme: dark)`` — follow the OS automatically.
  * ``:root[data-theme="dark"]`` — an explicit choice, which must win in *both*
    directions, hence the matching ``[data-theme="light"]`` block. Without that pair,
    a user on a dark-mode OS could never force light.

``no_flash_js()`` has to run before first paint, or a user who chose dark gets a white
flash on every navigation while the stylesheet waits on the toggle script.

Adding a colour means adding it to both blocks. If a value only exists in one, the
other mode silently falls back to whatever it inherited — usually invisible in the
mode you're developing in, obvious in the one you aren't.
"""
from __future__ import annotations

# --- Tokens ----------------------------------------------------------------
# The signature accent is deliberately teal rather than the blue every surface had
# grown independently: it reads as distinct from the semantic states (ok = green,
# warn = amber, bad = red) and holds contrast in both modes, which a lime or a pale
# blue does not. It's one variable per mode — swap those two lines to rebrand.
#
# Contrast is chosen for AA body text, not just for looks: the light accent is
# darkened well past the "pretty" teal so it stays legible as link text on the page
# background, and `dim` clears 4.5:1 in both modes.
_TOKENS = """
:root{
  color-scheme: light;
  --bg:#F7F8FA; --panel:#FFFFFF; --panel-2:#F1F3F7; --line:#E2E6EE;
  --ink:#0E1420; --dim:#5C6779;
  --accent:#0B7373; --accent-ink:#FFFFFF; --accent-soft:#E3F4F4;
  --ok:#1F8A4C; --warn:#9A6600; --bad:#C0392B;
  --ok-soft:#E6F5EC; --warn-soft:#FDF3E0; --bad-soft:#FBEAE7;
  --shadow:0 1px 3px rgba(14,20,32,.10);
  --shadow-lg:0 6px 24px rgba(14,20,32,.12);
  --radius:14px; --radius-sm:10px;
  --font:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,sans-serif;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    color-scheme: dark;
    --bg:#0C0F16; --panel:#141924; --panel-2:#1B2130; --line:#263041;
    --ink:#E9EDF5; --dim:#98A3B8;
    --accent:#2ED9D9; --accent-ink:#06202A; --accent-soft:#0E2A2E;
    --ok:#3DD68C; --warn:#FFC861; --bad:#FF6B6B;
    --ok-soft:#0F2B1D; --warn-soft:#2C2312; --bad-soft:#2C1417;
    --shadow:0 1px 3px rgba(0,0,0,.5);
    --shadow-lg:0 6px 24px rgba(0,0,0,.55);
  }
}
:root[data-theme="dark"]{
  color-scheme: dark;
  --bg:#0C0F16; --panel:#141924; --panel-2:#1B2130; --line:#263041;
  --ink:#E9EDF5; --dim:#98A3B8;
  --accent:#2ED9D9; --accent-ink:#06202A; --accent-soft:#0E2A2E;
  --ok:#3DD68C; --warn:#FFC861; --bad:#FF6B6B;
  --ok-soft:#0F2B1D; --warn-soft:#2C2312; --bad-soft:#2C1417;
  --shadow:0 1px 3px rgba(0,0,0,.5);
  --shadow-lg:0 6px 24px rgba(0,0,0,.55);
}
"""

# --- Base + shared primitives ----------------------------------------------
_BASE = """
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--bg);color:var(--ink);font-family:var(--font);
  font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
a{color:var(--accent)}
h1{font-size:21px;margin:0 0 2px;letter-spacing:-.01em}
h2{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);
  font-weight:700;margin:26px 0 10px}
.muted,.sub{color:var(--dim);font-size:13px}
.wrap{max-width:960px;margin:0 auto;padding:0 16px}

.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow)}

.badge,.pill{display:inline-block;border-radius:999px;font-size:12px;font-weight:600;
  padding:2px 10px;white-space:nowrap}
.pill{background:var(--panel-2);border:1px solid var(--line);color:var(--dim)}
.pill.ok{background:var(--ok-soft);border-color:transparent;color:var(--ok)}
.pill.warn{background:var(--warn-soft);border-color:transparent;color:var(--warn)}
.pill.bad{background:var(--bad-soft);border-color:transparent;color:var(--bad)}

button,.btn{font:inherit;font-weight:600;padding:9px 14px;border-radius:var(--radius-sm);
  border:1px solid var(--line);background:var(--panel-2);color:var(--ink);
  cursor:pointer;text-decoration:none;display:inline-block}
button:hover,.btn:hover{border-color:var(--accent)}
button.primary,.btn.primary{background:var(--accent);color:var(--accent-ink);
  border-color:var(--accent)}
button.ghost,.btn.ghost{background:transparent;color:var(--dim)}
button:disabled{opacity:.5;cursor:not-allowed}

/* Focus is a token too — a hardcoded outline disappears against one of the modes. */
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

input,textarea,select{font:inherit;color:var(--ink);background:var(--panel);
  border:1px solid var(--line);border-radius:var(--radius-sm);padding:9px 12px;width:100%}

table{width:100%;background:var(--panel);border:1px solid var(--line);
  border-radius:var(--radius);border-collapse:separate;border-spacing:0;overflow:hidden}
td,th{padding:9px 13px;border-bottom:1px solid var(--line);text-align:left}
tr:last-child td{border-bottom:none}
code{background:var(--panel-2);padding:1px 5px;border-radius:4px;font-size:13px}

/* Theme toggle — fixed so every page gets it without touching their layouts. */
.theme-toggle{position:fixed;top:12px;right:12px;z-index:99;width:36px;height:36px;
  padding:0;border-radius:50%;background:var(--panel);border:1px solid var(--line);
  color:var(--dim);box-shadow:var(--shadow);line-height:34px;text-align:center;
  font-size:15px}
.theme-toggle:hover{color:var(--accent)}
:root[data-theme="dark"] .theme-toggle .sun,
:root:not([data-theme="light"]) .theme-toggle .sun{display:inline}
:root[data-theme="dark"] .theme-toggle .moon,
:root:not([data-theme="light"]) .theme-toggle .moon{display:none}
@media (prefers-color-scheme: light){
  :root:not([data-theme="dark"]) .theme-toggle .sun{display:none}
  :root:not([data-theme="dark"]) .theme-toggle .moon{display:inline}
}
:root[data-theme="light"] .theme-toggle .sun{display:none}
:root[data-theme="light"] .theme-toggle .moon{display:inline}
"""

CSS = _TOKENS + _BASE


def no_flash_js() -> str:
    """Apply the saved theme before first paint.

    Must be inline in <head> and *before* any rendered markup — if it runs after,
    a user who chose dark sees a white flash on every page load."""
    return ("(function(){try{var t=localStorage.getItem('jsi-theme');"
            "if(t)document.documentElement.setAttribute('data-theme',t);}catch(e){}})();")


def toggle_html() -> str:
    """The toggle button. Both glyphs ship; CSS decides which is visible, so the
    button shows the mode you'd switch *to* without needing JS to render it."""
    return ('<button class="theme-toggle" onclick="jsiToggleTheme()" '
            'aria-label="Switch between light and dark">'
            '<span class="sun">☀</span><span class="moon">☾</span></button>')


def toggle_js() -> str:
    """Flip the mode and remember it.

    Reads the *computed* mode rather than assuming light, so the first click from an
    OS-dark page goes to light instead of appearing to do nothing."""
    return ("function jsiToggleTheme(){var r=document.documentElement;"
            "var cur=r.getAttribute('data-theme')||"
            "(window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');"
            "var next=cur==='dark'?'light':'dark';"
            "r.setAttribute('data-theme',next);"
            "try{localStorage.setItem('jsi-theme',next)}catch(e){}}")


def head(title: str, extra_css: str = "") -> str:
    """The full <head> for a themed page: viewport, no-flash script, tokens, and
    whatever page-specific layout CSS the caller adds on top."""
    return (
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title}</title>"
        f"<script>{no_flash_js()}</script>"
        f"<style>{CSS}{extra_css}</style>"
    )
