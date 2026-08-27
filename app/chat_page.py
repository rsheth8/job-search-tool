"""Minimal web chat page — Sign in with Apple + message thread.

Kept deliberately thin: one HTML document, no SPA framework. iOS is the
primary client; this is the desktop companion.
"""
from __future__ import annotations

from html import escape

from .config import get_settings


def render_chat_page() -> str:
    s = get_settings()
    services_id = (s.apple_services_id or "").strip()
    client_ids = escape(s.apple_client_ids or "")
    allow_dev = s.auth_allow_dev_login

    apple_block = ""
    if services_id:
        apple_block = f"""
  <script type="text/javascript"
    src="https://appleid.cdn-apple.com/appleauth/static/jsapi/appleid/1/en_US/appleid.auth.js"></script>
  <div id="appleid-signin" data-color="black" data-border="true"
       data-type="sign in" data-border-radius="8" data-width="280" data-height="40"></div>
  <script>
    AppleID.auth.init({{
      clientId: {services_id!r},
      scope: 'name email',
      redirectURI: window.location.origin + '/chat',
      usePopup: true
    }});
    document.addEventListener('AppleIDSignInOnSuccess', async (event) => {{
      const t = event.detail.authorization.id_token;
      const name = event.detail.user && event.detail.user.name;
      const display = name ? [name.firstName, name.lastName].filter(Boolean).join(' ') : null;
      await doAppleLogin(t, display);
    }});
    document.addEventListener('AppleIDSignInOnFailure', (event) => {{
      setStatus('Sign in failed: ' + (event.detail && event.detail.error || 'unknown'));
    }});
  </script>
"""
    else:
        apple_block = """
  <p class="muted">Web Sign in with Apple needs <code>APPLE_SERVICES_ID</code> on the
  server. Use the iOS app, or enable dev login below.</p>
"""

    dev_block = ""
    if allow_dev:
        dev_block = """
  <button type="button" id="devLogin" class="secondary">Dev sign-in</button>
"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Apply · Chat</title>
<style>
  :root {{
    --fog: #f3f5f2;
    --ink: #1c2421;
    --accent: #5b7c6e;
    --soft: rgba(91,124,110,0.55);
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh;
    font-family: ui-rounded, "SF Pro Rounded", system-ui, sans-serif;
    background: var(--fog); color: var(--ink);
  }}
  main {{
    max-width: 640px; margin: 0 auto; padding: 24px 16px 96px;
    display: flex; flex-direction: column; gap: 16px; min-height: 100vh;
  }}
  h1 {{ font-size: 1.6rem; font-weight: 600; margin: 0; }}
  .muted {{ color: var(--soft); font-size: 0.95rem; }}
  #gate, #thread {{ display: none; }}
  #gate.show, #thread.show {{ display: block; }}
  #messages {{
    display: flex; flex-direction: column; gap: 10px;
    flex: 1; min-height: 50vh; padding-bottom: 12px;
  }}
  .bubble {{
    max-width: 85%; padding: 10px 14px; border-radius: 16px;
    white-space: pre-wrap; line-height: 1.4; font-size: 0.95rem;
  }}
  .bubble.user {{
    align-self: flex-end; background: var(--accent); color: #fff;
  }}
  .bubble.assistant {{
    align-self: flex-start; background: #fff;
    border: 1px solid rgba(91,124,110,0.15);
  }}
  form#composer {{
    position: sticky; bottom: 0; display: flex; gap: 8px;
    padding: 12px 0; background: linear-gradient(transparent, var(--fog) 30%);
  }}
  #composer input {{
    flex: 1; border: 1px solid rgba(91,124,110,0.25); border-radius: 12px;
    padding: 12px 14px; font: inherit; background: #fff;
  }}
  button, .secondary {{
    border: none; border-radius: 12px; padding: 12px 16px;
    font: inherit; font-weight: 600; cursor: pointer;
    background: var(--accent); color: #fff;
  }}
  .secondary {{ background: transparent; color: var(--accent);
    border: 1px solid rgba(91,124,110,0.35); }}
  #status {{ font-size: 0.85rem; color: var(--soft); min-height: 1.2em; }}
  header {{ display: flex; justify-content: space-between; align-items: baseline; }}
  a {{ color: var(--accent); }}
</style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>Chat</h1>
      <p class="muted">Same assistant as the iPhone app. Apply stays on Apply.</p>
    </div>
    <a href="/apply">Apply →</a>
  </header>

  <div id="gate" class="show">
    <p class="muted">Sign in to continue. Audiences: {client_ids}</p>
    {apple_block}
    {dev_block}
    <p id="status"></p>
  </div>

  <div id="thread">
    <div id="messages"></div>
    <form id="composer">
      <input id="text" autocomplete="off" placeholder="Message…" />
      <button type="submit">Send</button>
    </form>
    <p id="who" class="muted"></p>
  </div>
</main>
<script>
const TOKEN_KEY = 'apply_session';
let token = localStorage.getItem(TOKEN_KEY) || '';

function setStatus(msg) {{
  const el = document.getElementById('status');
  if (el) el.textContent = msg || '';
}}

function showThread(user) {{
  document.getElementById('gate').classList.remove('show');
  document.getElementById('thread').classList.add('show');
  const who = document.getElementById('who');
  if (user) who.textContent = (user.display_name || user.email || user.id) +
    ' · <a href="#" id="signout">Sign out</a>'.replace('<a', '<a');
  who.innerHTML = (user.display_name || user.email || user.id) +
    ' · <a href="#" id="signout">Sign out</a>';
  document.getElementById('signout').onclick = (e) => {{
    e.preventDefault();
    fetch('/auth/logout', {{ method: 'POST', headers: authHeaders() }});
    localStorage.removeItem(TOKEN_KEY);
    token = '';
    location.reload();
  }};
}}

function authHeaders() {{
  return {{
    'Content-Type': 'application/json',
    'Authorization': 'Bearer ' + token,
  }};
}}

async function doAppleLogin(idToken, displayName) {{
  setStatus('Signing in…');
  const body = {{ identity_token: idToken }};
  if (displayName) body.display_name = displayName;
  const res = await fetch('/auth/apple', {{
    method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify(body),
  }});
  if (!res.ok) {{ setStatus('Login failed (' + res.status + ')'); return; }}
  const data = await res.json();
  token = data.token;
  localStorage.setItem(TOKEN_KEY, token);
  showThread(data.user);
  await loadHistory();
}}

async function bootstrap() {{
  if (!token) return;
  const res = await fetch('/auth/me', {{ headers: authHeaders() }});
  if (!res.ok) {{ localStorage.removeItem(TOKEN_KEY); token = ''; return; }}
  const data = await res.json();
  showThread(data.user);
  await loadHistory();
}}

async function loadHistory() {{
  const res = await fetch('/chat/history?limit=100', {{ headers: authHeaders() }});
  if (!res.ok) return;
  const data = await res.json();
  const box = document.getElementById('messages');
  box.innerHTML = '';
  for (const m of data.messages || []) {{
    const div = document.createElement('div');
    div.className = 'bubble ' + m.role;
    div.textContent = m.body;
    box.appendChild(div);
  }}
  box.scrollTop = box.scrollHeight;
}}

document.getElementById('composer').addEventListener('submit', async (e) => {{
  e.preventDefault();
  const input = document.getElementById('text');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  const box = document.getElementById('messages');
  const u = document.createElement('div');
  u.className = 'bubble user';
  u.textContent = text;
  box.appendChild(u);
  const res = await fetch('/chat', {{
    method: 'POST', headers: authHeaders(),
    body: JSON.stringify({{ text }}),
  }});
  const data = await res.json().catch(() => ({{}}));
  const a = document.createElement('div');
  a.className = 'bubble assistant';
  a.textContent = res.ok ? (data.reply || '') : ('Error ' + res.status);
  box.appendChild(a);
  box.scrollTop = box.scrollHeight;
}});

const devBtn = document.getElementById('devLogin');
if (devBtn) {{
  devBtn.onclick = async () => {{
    const res = await fetch('/auth/dev', {{
      method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{}}),
    }});
    if (!res.ok) {{ setStatus('Dev login failed'); return; }}
    const data = await res.json();
    token = data.token;
    localStorage.setItem(TOKEN_KEY, token);
    showThread(data.user);
    await loadHistory();
  }};
}}

bootstrap();
</script>
</body>
</html>
"""
