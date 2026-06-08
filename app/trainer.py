"""Swipe trainer — bootstrap the re-ranker fast.

The re-ranker (``app/reranker.py``) learns from the user's own apply/dismiss
history, but a brand-new user hasn't applied to enough roles for it to engage.
This module powers a Tinder-style web UI (served from ``main.py`` at ``/train``)
that shows **real** postings pulled from the ATS directory and records a quick
"would I apply?" yes/no on each. Those become training labels (``training_labels``
table) the re-ranker reads alongside real applications — so the user can warm up
the model in a couple of minutes of swiping.

Kept deliberately separate from ``job_postings`` so swipes never look like real
applications to the rest of the system.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from . import matcher, profile
from .db import connect
from .jobsources import JobPosting
from .jobsources import directory

_SUMMARY_CHARS = 280


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _already_labeled_ids(user_id: str) -> set[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT source || '|' || external_id AS k FROM training_labels WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    return {r["k"] for r in rows}


def build_deck(user_id: str, *, limit: int = 15, fetch=None) -> list[dict]:
    """A batch of fresh, un-swiped real postings to judge, best-match first.

    ``fetch`` injects the posting source in tests; in production it's the ATS
    directory batch (which advances a global cursor, so repeat calls bring new
    companies). Each card carries a relevance score (the matcher's own) so the
    re-ranker's ``relevance`` feature is populated from the swipe.
    """
    fetcher = fetch or (lambda: directory.fetch_directory_batch(boards_to_probe=12))
    try:
        postings: list[JobPosting] = fetcher() or []
    except Exception:  # noqa: BLE001 — a bad board never breaks the deck
        postings = []

    seen = _already_labeled_ids(user_id)
    fresh: list[JobPosting] = []
    batch_keys: set[str] = set()
    for p in postings:
        key = f"{p.source}|{p.external_id}"
        if not p.external_id or key in seen or key in batch_keys:
            continue
        batch_keys.add(key)
        fresh.append(p)

    if not fresh:
        return []

    prof = profile.get_profile(user_id)
    scored = matcher.score(fresh, prof)  # [(posting, score)]; never raises
    scored.sort(key=lambda t: t[1], reverse=True)
    return [_card(p, sc) for p, sc in scored[:limit]]


def _card(p: JobPosting, score: float) -> dict:
    desc = (p.description or "").strip()
    if len(desc) > _SUMMARY_CHARS:
        desc = desc[:_SUMMARY_CHARS].rstrip() + "…"
    return {
        "source": p.source,
        "external_id": p.external_id,
        "company": p.company or p.source,
        "title": p.title or "Role",
        "location": p.location or "",
        "url": p.url or "",
        "description": desc,
        "relevance_score": round(float(score), 3),
    }


def record_label(user_id: str, item: dict, label: str) -> bool:
    """Persist one swipe. ``label`` is 'like' (would apply) or 'pass'. Returns
    False if this posting was already labeled (idempotent)."""
    label = "like" if label == "like" else "pass"
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO training_labels
                (user_id, source, external_id, company, title, location, url,
                 description, relevance_score, label, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, item.get("source", ""), item.get("external_id", ""),
                item.get("company"), item.get("title"), item.get("location"),
                item.get("url"), item.get("description"),
                item.get("relevance_score"), label, _now(),
            ),
        )
        return cur.rowcount > 0


def render_page(user_id: str) -> str:
    """Self-contained swipe UI (no external deps). Talks to /train/* endpoints."""
    safe_user = user_id.replace("\\", "\\\\").replace('"', '\\"')
    return _PAGE.replace("__USER__", safe_user)


_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Train your matcher</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #0f1115; color: #e6e6e6; display: flex; flex-direction: column;
         align-items: center; min-height: 100vh; padding: 16px; }
  h1 { font-size: 18px; font-weight: 600; margin: 8px 0 2px; }
  #banner { font-size: 13px; padding: 8px 14px; border-radius: 999px; margin: 8px 0 16px;
            background: #1b1e26; border: 1px solid #2a2f3a; }
  #banner.trained { background: #11301d; border-color: #1f7a44; color: #7ee2a8; }
  #stack { position: relative; width: min(440px, 94vw); height: 460px; }
  .card { position: absolute; inset: 0; background: #161922; border: 1px solid #2a2f3a;
          border-radius: 18px; padding: 22px; display: flex; flex-direction: column;
          box-shadow: 0 10px 30px rgba(0,0,0,.4); transition: transform .25s, opacity .25s; }
  .title { font-size: 22px; font-weight: 700; line-height: 1.2; }
  .company { font-size: 15px; color: #9aa4b2; margin-top: 4px; }
  .meta { font-size: 13px; color: #7c8595; margin-top: 8px; display: flex; gap: 10px; flex-wrap: wrap; }
  .score { background: #1f2430; border-radius: 8px; padding: 2px 8px; }
  .desc { margin-top: 14px; font-size: 14px; line-height: 1.5; color: #c7ccd6; overflow: auto; flex: 1; }
  .link { font-size: 12px; color: #6ea8ff; margin-top: 10px; text-decoration: none; word-break: break-all; }
  #buttons { display: flex; gap: 18px; margin-top: 22px; }
  button { font-size: 16px; font-weight: 600; padding: 14px 28px; border-radius: 14px;
           border: 1px solid #2a2f3a; cursor: pointer; background: #1b1e26; color: #e6e6e6; }
  #pass:hover { background: #3a1d22; border-color: #b3424f; }
  #like:hover { background: #11301d; border-color: #1f7a44; }
  .hint { font-size: 12px; color: #69707d; margin-top: 14px; }
  .empty { display: flex; align-items: center; justify-content: center; text-align: center;
           color: #808997; height: 100%; font-size: 15px; }
</style>
</head>
<body>
  <h1>Train your matcher &mdash; <span style="color:#9aa4b2">__USER__</span></h1>
  <div id="banner">loading&hellip;</div>
  <div id="stack"></div>
  <div id="buttons">
    <button id="pass">&#128078; Pass</button>
    <button id="like">&#128077; Would apply</button>
  </div>
  <div class="hint">&larr; pass &nbsp;&middot;&nbsp; &rarr; would apply &nbsp;&middot;&nbsp; swipes train your re-ranker live</div>

<script>
const USER = "__USER__";
let deck = [];
let busy = false;

const $ = (id) => document.getElementById(id);

function esc(s){ const d=document.createElement('div'); d.textContent = s==null?'':s; return d.innerHTML; }

function renderBanner(st){
  const b = $('banner');
  if (st.model_trained){
    b.className = 'trained';
    b.innerHTML = `&#9989; Model trained on ${st.model_n_labels} labels &nbsp;&middot;&nbsp; ${st.likes} would-apply &middot; ${st.passes} pass`;
  } else {
    b.className = '';
    b.innerHTML = `${st.likes} would-apply &middot; ${st.passes} pass &nbsp;&middot;&nbsp; need ${st.need_likes} more yes &amp; ${st.need_passes} more no to train`;
  }
}

function renderTop(){
  const stack = $('stack');
  if (!deck.length){
    stack.innerHTML = '<div class="card"><div class="empty">No more postings right now.<br>Swipe again later &mdash; the deck refreshes from new boards each time.</div></div>';
    return;
  }
  const c = deck[0];
  stack.innerHTML = `
    <div class="card">
      <div class="title">${esc(c.title)}</div>
      <div class="company">${esc(c.company)}</div>
      <div class="meta">
        ${c.location ? `<span>&#128205; ${esc(c.location)}</span>` : ''}
        <span class="score">match ${Math.round((c.relevance_score||0)*100)}%</span>
        <span>${esc(c.source)}</span>
      </div>
      <div class="desc">${esc(c.description) || '<em>No description.</em>'}</div>
      ${c.url ? `<a class="link" href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.url)}</a>` : ''}
    </div>`;
}

async function loadDeck(){
  const r = await fetch(`/train/deck?user=${encodeURIComponent(USER)}&n=15`);
  const data = await r.json();
  deck = deck.concat(data.cards || []);
  renderBanner(data.stats);
  renderTop();
}

async function swipe(label){
  if (busy || !deck.length) return;
  busy = true;
  const card = deck.shift();
  const cardEl = $('stack').querySelector('.card');
  if (cardEl){
    cardEl.style.transform = label==='like' ? 'translateX(120%) rotate(8deg)' : 'translateX(-120%) rotate(-8deg)';
    cardEl.style.opacity = '0';
  }
  try {
    const r = await fetch('/train/label', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({user: USER, label, item: card})
    });
    const st = await r.json();
    renderBanner(st);
  } catch(e){ /* keep swiping even if a save blips */ }
  setTimeout(renderTop, 180);
  if (deck.length < 3) loadDeck();
  busy = false;
}

$('pass').onclick = () => swipe('pass');
$('like').onclick = () => swipe('like');
document.addEventListener('keydown', (e) => {
  if (e.key === 'ArrowLeft') swipe('pass');
  if (e.key === 'ArrowRight') swipe('like');
});

loadDeck();
</script>
</body>
</html>"""


def stats(user_id: str) -> dict:
    """Swipe + model status for the trainer UI banner."""
    from .config import get_settings
    from . import reranker

    s = get_settings()
    with connect() as conn:
        rows = conn.execute(
            "SELECT label, COUNT(*) n FROM training_labels WHERE user_id = ? GROUP BY label",
            (user_id,),
        ).fetchall()
    counts = {r["label"]: r["n"] for r in rows}
    likes = counts.get("like", 0)
    passes = counts.get("pass", 0)
    model = reranker.load_model(user_id)
    return {
        "user": user_id,
        "likes": likes,
        "passes": passes,
        "total": likes + passes,
        "need_likes": max(0, s.reranker_min_positive - likes),
        "need_passes": max(0, s.reranker_min_negative - passes),
        "model_trained": model is not None,
        "model_n_labels": (model or {}).get("n_labels"),
        "model_trained_at": (model or {}).get("trained_at"),
    }
