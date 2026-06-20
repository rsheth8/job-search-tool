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

from . import eligibility, matcher, profile
from .config import get_settings
from .db import connect
from .jobsources import JobPosting
from .jobsources import directory, ghost, quality
from .jobsources import rss as rss_src

# RSS feeds woven into the deck for company variety — these are startup/scale-up
# heavy, balancing the big-name ATS directory.
_DECK_RSS_FEEDS = ("remoteok", "weworkremotely")
# Max roles one company can contribute to a single deck (keeps variety up).
_MAX_PER_COMPANY = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _already_labeled_ids(user_id: str) -> set[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT source || '|' || external_id AS k FROM training_labels WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    return {r["k"] for r in rows}


def _default_sources() -> list[JobPosting]:
    """Real postings for the deck: the big-name ATS directory PLUS startup-heavy
    RSS feeds, so the deck isn't all large companies. Each source is wrapped so a
    single bad feed never empties the deck."""
    posts: list[JobPosting] = []
    try:
        posts += directory.fetch_directory_batch(boards_to_probe=8)
    except Exception:  # noqa: BLE001
        pass
    for feed in _DECK_RSS_FEEDS:
        try:
            posts += rss_src.fetch(feed)
        except Exception:  # noqa: BLE001
            pass
    return posts


def build_deck(user_id: str, *, limit: int = 15, fetch=None, diverse: bool = False,
               uncertain: bool = False) -> list[dict]:
    """A batch of fresh, un-swiped real postings to judge.

    Runs the same gates discovery does — reputability, the eligibility rule tier
    (drop roles above the candidate's level), and (in normal mode) the profile
    pre-filter — so the deck reflects roles that make sense for the user. ``fetch``
    injects the posting source in tests; in production it's ``_default_sources``
    (ATS directory + RSS, the directory advancing a cursor so repeat calls bring
    new companies). Each card carries the matcher's relevance score, populating
    the re-ranker's ``relevance`` feature from the swipe.

    ``diverse=True`` (the UI's "Mix" mode) is for *training*: it skips the profile
    pre-filter and spreads cards across the whole relevance range (strong → weak)
    instead of only the top matches — so the user actually sees roles worth
    rejecting, which is what balances the apply/pass labels the re-ranker needs.

    ``uncertain=True`` (the UI's "Learn" mode) is **active learning**: it scores
    the wide pool with the trained re-ranker and surfaces the postings it's least
    sure about (probability nearest 0.5), so each swipe resolves a genuine
    ambiguity and teaches the model the most. Falls back to the relevance sort
    until a model exists (cold start), so it's safe to pick before training.
    """
    settings = get_settings()
    fetcher = fetch or _default_sources
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
    fresh, _ = quality.filter_reputable(fresh)        # drop spam/placeholder
    fresh = [p for p in fresh if not ghost.is_ghost(p)]  # drop ghost/evergreen reqs
    if settings.eligibility_filter_enabled:           # drop over-qualified roles
        fresh, _ = eligibility.filter_eligible(fresh, prof)
    # Normal mode focuses on profile terms; Mix and Learn modes keep the wider
    # pool — Mix so there are off-target roles to reject, Learn so the model has
    # varied roles to be uncertain about.
    pool = fresh if (diverse or uncertain) else (matcher.prefilter(fresh, prof) or fresh)
    if not pool:
        return []

    # Free heuristic scoring only (no LLM, no embeddings): the deck's score is
    # just a sort key, so we keep it fast with no API latency/rate-limits and
    # spend the LLM budget on the card summaries instead.
    scored = matcher.score(pool, prof, allow_llm=False, allow_embeddings=False)  # never raises
    scored.sort(key=lambda t: t[1], reverse=True)
    if uncertain:
        # Active learning: reorder to the postings the model is least sure about
        # (probability nearest 0.5). No model yet → keep the relevance sort.
        from . import reranker

        preds = reranker.predict(user_id, prof, scored)
        if preds:
            unc = {(p.source, p.external_id): abs(prob - 0.5) for p, prob in preds}
            scored.sort(key=lambda t: unc.get((t[0].source, t[0].external_id), 1.0))
    elif diverse and len(scored) > limit:
        # Spread evenly across the sorted range (strong → weak), then append the
        # full list so the dedup/cap loop below can still backfill to `limit`.
        spread = [scored[round(i * (len(scored) - 1) / (limit - 1))] for i in range(limit)]
        scored = spread + scored

    # Collapse near-duplicates (same company + title, e.g. one role posted for
    # many locations) and cap how many roles any one company contributes, so the
    # deck shows variety rather than 10 listings from a single employer.
    cards: list[dict] = []
    seen_roles: set[tuple[str, str]] = set()
    per_company: dict[str, int] = {}
    for p, sc in scored:
        company = (p.company or "").strip().lower()
        sig = (company, (p.title or "").strip().lower())
        if sig in seen_roles or per_company.get(company, 0) >= _MAX_PER_COMPANY:
            continue
        seen_roles.add(sig)
        per_company[company] = per_company.get(company, 0) + 1
        cards.append(_card(p, sc))
        if len(cards) >= limit:
            break
    return cards


def _clean_title(title: str, company: str) -> str:
    """Strip a redundant 'Company: ' prefix some RSS feeds bake into the title."""
    title = (title or "Role").strip()
    comp = (company or "").strip()
    if comp and ":" in title and title.lower().startswith(comp.lower() + ":"):
        return title[len(comp) + 1:].strip() or title
    return title


def _card(p: JobPosting, score: float) -> dict:
    # Keep the FULL description (up to the source cap): it's stored as the training
    # label and fed to embeddings, where truncating to a snippet badly hurt match
    # quality. The card UI shows it in a scrollable box, so length is fine.
    desc = (p.description or "").strip()
    return {
        "source": p.source,
        "external_id": p.external_id,
        "company": p.company or p.source,
        "title": _clean_title(p.title, p.company),
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
  :root{
    color-scheme: dark;
    --bg:#0b0d12; --card:#171a22; --card2:#1f2330; --line:#2a2f3c;
    --ink:#eef1f6; --muted:#9aa4b4; --dim:#6b7383;
    --blue:#6ea8ff; --green:#39d98a; --greenbg:#0f2b1d; --red:#ff6b6b; --redbg:#2c1417;
    --amber:#ffce6b;
  }
  *{ box-sizing:border-box; }
  html,body{ height:100%; }
  body{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,sans-serif;
        background:radial-gradient(1200px 600px at 50% -10%, #141826 0%, var(--bg) 60%);
        color:var(--ink); display:flex; flex-direction:column; align-items:center;
        min-height:100%; padding:20px 16px 28px; }
  .head{ width:min(620px,96vw); }
  .kicker{ font-size:11px; letter-spacing:.16em; font-weight:700; color:var(--dim); text-transform:uppercase; }
  h1{ font-size:21px; font-weight:700; margin:3px 0 0; }
  h1 span{ color:var(--muted); font-weight:500; }
  /* progress */
  .prog{ width:min(620px,96vw); margin:16px 0 18px; }
  .progbar{ height:8px; border-radius:999px; background:#20242f; overflow:hidden; border:1px solid var(--line); }
  .progfill{ height:100%; width:0%; border-radius:999px;
             background:linear-gradient(90deg,var(--blue),var(--green)); transition:width .35s ease; }
  .progmeta{ display:flex; justify-content:space-between; align-items:center; margin-top:8px;
             font-size:13px; color:var(--muted); }
  .pill{ padding:3px 10px; border-radius:999px; background:#20242f; border:1px solid var(--line); font-weight:600; }
  .pill.ok{ background:var(--greenbg); border-color:#1f7a44; color:var(--green); }
  .modes{ display:inline-flex; gap:4px; background:#171a22; border:1px solid var(--line);
          border-radius:10px; padding:3px; margin-top:12px; }
  .mode{ flex:none; font-size:12.5px; font-weight:600; padding:6px 12px; border-radius:7px;
         border:none; background:transparent; color:var(--muted); cursor:pointer; }
  .mode.active{ background:#26304a; color:var(--ink); }
  .modehint{ font-size:12px; color:var(--dim); margin:6px 0 0; }
  /* deck */
  #stack{ position:relative; width:min(620px,96vw); height:min(580px,70vh); }
  .card{ position:absolute; inset:0; background:var(--card); border:1px solid var(--line);
         border-radius:24px; padding:26px 26px 22px; display:flex; flex-direction:column;
         box-shadow:0 24px 60px rgba(0,0,0,.55); }
  .card.top{ z-index:3; cursor:grab; transition:transform .28s cubic-bezier(.2,.7,.3,1), opacity .28s; }
  .card.top.dragging{ transition:none; cursor:grabbing; }
  .card.behind{ z-index:1; transform:scale(.94) translateY(16px); opacity:.55; filter:saturate(.7); }
  .crow{ display:flex; justify-content:space-between; align-items:center; gap:10px; }
  .src{ font-size:11px; font-weight:700; letter-spacing:.06em; text-transform:uppercase; color:var(--dim);
        background:#20242f; border:1px solid var(--line); border-radius:8px; padding:3px 9px; }
  .match{ font-size:13px; font-weight:800; border-radius:10px; padding:5px 11px; }
  .match.hi{ background:var(--greenbg); color:var(--green); }
  .match.mid{ background:#2a2410; color:var(--amber); }
  .match.lo{ background:#20242f; color:var(--muted); }
  .title{ font-size:27px; font-weight:800; line-height:1.15; margin:16px 0 6px; }
  .company{ font-size:16px; color:var(--ink); font-weight:600; }
  .loc{ font-size:14px; color:var(--muted); margin-top:3px; }
  .chips{ display:flex; gap:8px; flex-wrap:wrap; margin-top:14px; }
  .chip{ font-size:12.5px; color:var(--muted); background:#20242f; border:1px solid var(--line);
         border-radius:9px; padding:6px 11px; }
  .chip b{ color:var(--ink); font-weight:600; }
  .chip .k{ color:var(--dim); font-weight:700; font-size:10.5px; letter-spacing:.06em;
            text-transform:uppercase; margin-right:6px; }
  .fit{ display:inline-flex; align-items:center; gap:8px; margin:14px 0 0; align-self:flex-start;
        font-size:14px; font-weight:600; color:var(--green);
        background:var(--greenbg); border:1px solid #1f7a44; border-radius:999px; padding:7px 14px; }
  .about{ margin-top:14px; font-size:13.5px; line-height:1.5; color:var(--muted); }
  .about .k{ display:block; font-size:10.5px; font-weight:800; letter-spacing:.1em;
             text-transform:uppercase; color:var(--dim); margin-bottom:3px; }
  .tldr{ margin-top:14px; font-size:16px; line-height:1.5; color:var(--ink);
         background:linear-gradient(180deg,#13243f,#101b30); border:1px solid #244674;
         border-radius:14px; padding:14px 16px; }
  .tldr .lbl{ display:block; font-size:11px; font-weight:800; letter-spacing:.1em; color:var(--blue); margin-bottom:4px; }
  .content{ flex:1; display:flex; flex-direction:column; min-height:0; }
  details.more{ margin-top:14px; }
  details.more summary{ font-size:13px; color:var(--blue); cursor:pointer; list-style:none; }
  details.more summary::-webkit-details-marker{ display:none; }
  .desc{ margin-top:10px; font-size:14px; line-height:1.6; color:var(--muted); overflow:auto; }
  .desc.grow{ flex:1; min-height:0; }
  .desc .lbl{ display:block; font-size:11px; font-weight:800; letter-spacing:.1em; color:var(--dim); margin-bottom:6px; }
  .link{ font-size:12px; color:var(--blue); margin-top:auto; padding-top:14px;
         text-decoration:none; word-break:break-all; opacity:.85; }
  /* swipe stamps */
  .stamp{ position:absolute; top:24px; font-size:26px; font-weight:900; letter-spacing:.08em;
          padding:6px 14px; border-radius:12px; border:3px solid; opacity:0; transition:opacity .1s;
          transform:rotate(-12deg); pointer-events:none; }
  .stamp.like{ right:24px; color:var(--green); border-color:var(--green); }
  .stamp.nope{ left:24px; color:var(--red); border-color:var(--red); transform:rotate(12deg); }
  .empty{ display:flex; align-items:center; justify-content:center; text-align:center;
          color:var(--dim); height:100%; font-size:16px; line-height:1.6; }
  /* buttons */
  #buttons{ display:flex; gap:16px; width:min(620px,96vw); margin-top:20px; }
  button{ flex:1; font-size:16px; font-weight:700; padding:16px; border-radius:16px;
          border:1px solid var(--line); cursor:pointer; background:var(--card2); color:var(--ink);
          transition:transform .08s, background .15s, border-color .15s; }
  button:active{ transform:scale(.97); }
  #pass:hover{ background:var(--redbg); border-color:var(--red); color:#ffb3b3; }
  #like:hover{ background:var(--greenbg); border-color:var(--green); color:#9bedc4; }
  .hint{ font-size:12.5px; color:var(--dim); margin-top:16px; text-align:center; }
  kbd{ background:#20242f; border:1px solid var(--line); border-bottom-width:2px; border-radius:6px;
       padding:1px 7px; font-family:inherit; font-size:12px; color:var(--muted); }
</style>
</head>
<body>
  <div class="head">
    <div class="kicker">Train your matcher</div>
    <h1>Would you apply? <span>&middot; __USER__</span></h1>
  </div>
  <div class="prog">
    <div class="progbar"><div class="progfill" id="progfill"></div></div>
    <div class="progmeta">
      <span id="counts">loading&hellip;</span>
      <span class="pill" id="status">&mdash;</span>
    </div>
    <div class="modes">
      <button class="mode active" data-mode="best">&#127919; Best matches</button>
      <button class="mode" data-mode="mix">&#127922; Mix (train)</button>
      <button class="mode" data-mode="learn">&#129504; Learn (smart)</button>
    </div>
    <div class="modehint" id="modehint">Showing your strongest matches.</div>
  </div>
  <div id="stack"></div>
  <div id="buttons">
    <button id="pass">&#128078;&nbsp; Pass</button>
    <button id="like">&#128077;&nbsp; Would apply</button>
  </div>
  <div class="hint">Drag the card, tap the buttons, or use <kbd>&larr;</kbd> pass &nbsp;/&nbsp; <kbd>&rarr;</kbd> apply &mdash; every swipe trains your matcher live</div>

<script>
const USER = "__USER__";
let deck = [];
let busy = false;
let mode = 'best';
const $ = (id) => document.getElementById(id);
function esc(s){ const d=document.createElement('div'); d.textContent = s==null?'':s; return d.innerHTML; }

function matchClass(p){ return p>=0.33 ? 'hi' : (p>=0.22 ? 'mid' : 'lo'); }

function renderProgress(st){
  const need = (st.need_likes||0) + (st.need_passes||0);
  const have = Math.min(5,st.likes||0) + Math.min(5,st.passes||0);
  $('progfill').style.width = (st.model_trained ? 100 : Math.round(have/10*100)) + '%';
  $('counts').innerHTML = `&#128077; ${st.likes||0} would-apply &nbsp;&middot;&nbsp; &#128078; ${st.passes||0} pass`;
  const s = $('status');
  if (st.model_trained){ s.className='pill ok'; s.innerHTML = `&#9989; Trained on ${st.model_n_labels}`; }
  else { s.className='pill'; s.innerHTML = `${need} more to train`; }
}

function cardHTML(c, cls){
  const m = Math.round((c.relevance_score||0)*100);
  return `<div class="card ${cls}">
    <div class="stamp like">APPLY</div><div class="stamp nope">NOPE</div>
    <div class="crow">
      <span class="src">${esc(c.source)}</span>
      <span class="match ${matchClass(c.relevance_score||0)}">${m}% match</span>
    </div>
    <div class="title">${esc(c.title)}</div>
    <div class="company">${esc(c.company)}</div>
    ${c.location ? `<div class="loc">&#128205; ${esc(c.location)}</div>` : ''}
    <div class="content">
      ${c.about ? `<div class="about"><span class="k">About ${esc(c.company)}</span>${esc(c.about)}</div>` : ''}
      ${c.tldr ? `<div class="tldr"><span class="lbl">The role</span>${esc(c.tldr)}</div>` : ''}
      ${(c.level || c.skills) ? `<div class="chips">
        ${c.level ? `<span class="chip"><span class="k">Level</span><b>${esc(c.level)}</b></span>` : ''}
        ${c.skills ? `<span class="chip"><span class="k">Skills</span>${esc(c.skills)}</span>` : ''}
      </div>` : ''}
      ${c.fit ? `<div class="fit">&#127919; ${esc(c.fit)}</div>` : ''}
      ${c.tldr
        ? `<details class="more"><summary>Full description</summary><div class="desc">${esc(c.description) || 'No description.'}</div></details>`
        : `<div class="desc grow"><span class="lbl">About this role</span>${esc(c.description) || 'No description provided.'}</div>`}
    </div>
    ${c.url ? `<a class="link" href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.url)} &#8599;</a>` : ''}
  </div>`;
}

function renderTop(){
  const stack = $('stack');
  if (!deck.length){
    stack.innerHTML = '<div class="card"><div class="empty">No more postings right now.<br>Come back later &mdash; the deck refreshes from new boards each time.</div></div>';
    return;
  }
  const behind = deck[1] ? cardHTML(deck[1], 'behind') : '';
  stack.innerHTML = behind + cardHTML(deck[0], 'top');
  attachDrag($('stack').querySelector('.card.top'));
}

function attachDrag(el){
  if (!el) return;
  let startX=0, dx=0, down=false;
  const like = el.querySelector('.stamp.like'), nope = el.querySelector('.stamp.nope');
  const move = (x)=>{ dx=x-startX; el.style.transform=`translateX(${dx}px) rotate(${dx/22}deg)`;
    like.style.opacity = dx>0 ? Math.min(1,dx/90) : 0;
    nope.style.opacity = dx<0 ? Math.min(1,-dx/90) : 0; };
  el.addEventListener('pointerdown', (e)=>{ if(e.target.closest('a,summary')) return;
    down=true; startX=e.clientX; el.classList.add('dragging'); el.setPointerCapture(e.pointerId); });
  el.addEventListener('pointermove', (e)=>{ if(down) move(e.clientX); });
  const end = ()=>{ if(!down) return; down=false; el.classList.remove('dragging');
    if (Math.abs(dx)>120) swipe(dx>0?'like':'pass');
    else { el.style.transform=''; like.style.opacity=0; nope.style.opacity=0; } };
  el.addEventListener('pointerup', end);
  el.addEventListener('pointercancel', end);
}

function renderLoading(){
  $('stack').innerHTML = '<div class="card"><div class="empty">Loading roles&hellip;'
    + '<br><span style="font-size:13px;color:var(--dim)">writing quick summaries &mdash; a few seconds</span></div></div>';
}

async function loadDeck(){
  if (!deck.length) renderLoading();
  try{
    const r = await fetch(`/train/deck?user=${encodeURIComponent(USER)}&n=8&mode=${mode}`);
    const data = await r.json();
    deck = deck.concat(data.cards || []);
    renderProgress(data.stats);
  }catch(e){}
  renderTop();         // show cards immediately (raw description)
  ensureSummaries();   // fill in AI summaries a beat later
}

async function ensureSummaries(){
  const need = deck.filter(c => !c._sumReq).slice(0, 6);
  if (!need.length) return;
  need.forEach(c => c._sumReq = true);  // don't re-request the same card
  try{
    const r = await fetch('/train/summaries', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({user: USER, items: need})
    });
    const map = (await r.json()).summaries || {};
    let changed = false;
    deck.forEach(c => {
      const s = map[`${c.source}:${c.external_id}`];
      if (s){ Object.assign(c, s); changed = true; }
    });
    if (changed) renderTop();  // refresh the visible card with its summary
  }catch(e){ need.forEach(c => c._sumReq = false); }  // allow a retry on failure
}

async function swipe(label){
  if (busy || !deck.length) return;
  busy = true;
  const card = deck.shift();
  const el = $('stack').querySelector('.card.top');
  if (el){ el.style.transform = label==='like' ? 'translateX(130%) rotate(12deg)' : 'translateX(-130%) rotate(-12deg)'; el.style.opacity='0'; }
  try{
    const r = await fetch('/train/label', { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({user: USER, label, item: card}) });
    renderProgress(await r.json());
  }catch(e){}
  setTimeout(renderTop, 200);
  if (deck.length < 3) loadDeck();
  ensureSummaries();   // keep upcoming cards' summaries warm
  busy = false;
}

$('pass').onclick = () => swipe('pass');
$('like').onclick = () => swipe('like');
document.addEventListener('keydown', (e)=>{ if(e.key==='ArrowLeft') swipe('pass'); if(e.key==='ArrowRight') swipe('like'); });

const MODE_HINT = {
  best: 'Showing your strongest matches.',
  mix: 'Showing a wide mix — reject the off-target ones to balance your training.',
  learn: 'Showing the roles your matcher is least sure about — your swipes here teach it the most.',
};
document.querySelectorAll('.mode').forEach(b => b.onclick = () => {
  const want = b.dataset.mode;
  if (want === mode) return;
  mode = want;
  document.querySelectorAll('.mode').forEach(x => x.classList.toggle('active', x === b));
  $('modehint').textContent = MODE_HINT[mode] || MODE_HINT.best;
  deck = []; $('stack').innerHTML = ''; loadDeck();
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
