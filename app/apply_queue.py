"""Semi-auto application queue (Track C).

Stage postings you want to apply to, assemble a review-ready package for each
(apply link + drafted "why I'm a fit" answers + tailored resume), and track each
item through ``staged -> ready -> submitted``.

We **never submit a form on the user's behalf** — submission stays a human
action. This layer removes the busywork *before* the click: it pre-builds the
application materials so the user just reviews, tweaks, and sends. (The optional
browser form-fill that drives an ATS page is a separate, opt-in step that always
pauses for a final confirmation.)

Package assembly reuses the existing apply-flow builders and inherits their
fail-open behaviour: ``outreach.draft_application_answers`` falls back to a clean
template with no API key, and ``resume_tailor.tailor_for_posting`` returns nothing
when tailoring is disabled. Built answers/resume are cached on the row so
re-opening an item never re-bills the LLM.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from . import ats, jobstore, outreach, profile as profile_mod
from .db import connect

logger = logging.getLogger("apply_queue")

STATUSES = ("staged", "ready", "submitted")

# The free-text questions most applications ask, pre-answered so they're ready to
# review/paste. Tailored per posting (company/title interpolated; answers grounded
# in the JD). The browser extension still answers a form's *actual* questions live;
# this gives the phone preview strong answers without the form in front of you.
COMMON_QUESTIONS = (
    "Why do you want to work at {company}?",
    "Why are you a strong fit for the {title} role?",
    "Tell us about a relevant project or accomplishment.",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stage(user_id: str, posting_id: int) -> bool:
    """Add a posting to the apply queue. Idempotent — returns False if it was
    already staged or the posting doesn't belong to the user."""
    if jobstore.get_posting(user_id, posting_id) is None:
        return False
    now = _now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO apply_queue "
            "(user_id, posting_id, status, created_at, updated_at) "
            "VALUES (?, ?, 'staged', ?, ?)",
            (user_id, posting_id, now, now),
        )
        return cur.rowcount > 0


def remove(user_id: str, posting_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM apply_queue WHERE user_id = ? AND posting_id = ?",
            (user_id, posting_id),
        )
        return cur.rowcount > 0


def mark(user_id: str, posting_id: int, status: str) -> bool:
    """Advance an item to 'ready' or 'submitted'. Returns False for an unknown
    status or a missing item. 'submitted' only ever reflects the user confirming
    they sent the application — it never triggers a submission."""
    if status not in STATUSES:
        return False
    with connect() as conn:
        cur = conn.execute(
            "UPDATE apply_queue SET status = ?, updated_at = ? "
            "WHERE user_id = ? AND posting_id = ?",
            (status, _now(), user_id, posting_id),
        )
        return cur.rowcount > 0


def list_queue(user_id: str, *, status: str | None = None) -> list[dict]:
    """Queue items joined with their posting (company/title/url/score/source),
    newest first. Optional ``status`` filter. Items whose posting was deleted are
    skipped."""
    sql = (
        "SELECT q.posting_id, q.status, q.questions_json, q.resume_path, q.updated_at, "
        "       p.company, p.title, p.url, p.source, p.relevance_score "
        "FROM apply_queue q JOIN job_postings p ON p.id = q.posting_id "
        "WHERE q.user_id = ? "
    )
    params: list = [user_id]
    if status is not None:
        sql += "AND q.status = ? "
        params.append(status)
    sql += "ORDER BY q.created_at DESC"
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {
            "posting_id": r["posting_id"],
            "status": r["status"],
            "company": r["company"],
            "title": r["title"],
            "url": r["url"],
            "source": r["source"],
            "score": r["relevance_score"],
            "auto_fillable": ats.is_fillable_form(r["url"]),
            "has_answers": bool(r["questions_json"]),
            "has_resume": bool(r["resume_path"]) and r["resume_path"] != _RESUME_NONE,
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def get_package(user_id: str, posting_id: int, *, prof=None) -> dict | None:
    """Assemble (and cache) the full application package: apply link, a tailored
    answer for each common question, the applicant identity, and a tailored resume.
    Best-effort — answers fall back to templates, resume to None. Returns None if
    the item or its posting is gone."""
    with connect() as conn:
        row = conn.execute(
            "SELECT status, resume_path FROM apply_queue "
            "WHERE user_id = ? AND posting_id = ?",
            (user_id, posting_id),
        ).fetchone()
    if row is None:
        return None
    posting = jobstore.get_posting(user_id, posting_id)
    if posting is None:
        return None
    if prof is None:
        prof = profile_mod.get_profile(user_id)

    company = posting["company"] or "the company"
    title = posting["title"] or "Role"
    questions = get_questions(user_id, posting_id, prof=prof)
    resume = _ensure_resume(user_id, posting_id, company, title, posting,
                            row["resume_path"])

    from . import applicant

    return {
        "posting_id": posting_id,
        "status": row["status"],
        "company": company,
        "title": title,
        "url": posting["url"] or "",
        "source": posting["source"],
        "score": posting["relevance_score"],
        "questions": questions,  # [{question, answer}] — one tailored answer each
        "resume": resume,        # {filename, variant, pages} or None
        # The facts that will fill the form's simple fields — shown so the user can
        # confirm them at a glance before applying.
        "identity": applicant.autofill_map(user_id),
    }


def get_questions(user_id: str, posting_id: int, *, prof=None) -> list[dict]:
    """The application's common questions with a tailored answer each — drafted
    (one batched LLM call) on first request, then cached on the row. Returns
    ``[{question, answer}]``; [] if the item/posting is gone."""
    with connect() as conn:
        row = conn.execute(
            "SELECT questions_json FROM apply_queue WHERE user_id = ? AND posting_id = ?",
            (user_id, posting_id),
        ).fetchone()
    if row is None:
        return []
    cached = _decode_json(row["questions_json"])
    if cached:
        return cached
    posting = jobstore.get_posting(user_id, posting_id)
    if posting is None:
        return []
    if prof is None:
        prof = profile_mod.get_profile(user_id)
    from . import applicant

    from . import knowledge

    company = posting["company"] or "the company"
    title = posting["title"] or "Role"
    prompts = [q.format(company=company, title=title) for q in COMMON_QUESTIONS]

    # Deterministic first: a question you've already answered well is reused
    # verbatim — no model call, no cost, no variance. Only the rest get drafted.
    answers: list[str | None] = [knowledge.canned_answer(user_id, q) for q in prompts]
    todo = [i for i, a in enumerate(answers) if a is None]
    if todo:
        drafted = outreach.draft_question_answers(
            [prompts[i] for i in todo], company, title, posting["description"], prof,
            identity_block=applicant.identity_block(user_id),
            knowledge_block=knowledge.knowledge_block(user_id),
        )
        for i, a in zip(todo, drafted):
            answers[i] = a

    qs = [{"question": q, "answer": a or ""} for q, a in zip(prompts, answers)]
    _save_questions(user_id, posting_id, qs)
    return qs


def save_answer(user_id: str, posting_id: int, index: int, answer: str) -> bool:
    """Persist a user-edited answer to question ``index``. False if the item or
    index is out of range."""
    qs = get_questions(user_id, posting_id)
    if not (0 <= index < len(qs)):
        return False
    qs[index]["answer"] = answer
    return _save_questions(user_id, posting_id, qs)


def redraft_answer(user_id: str, posting_id: int, index: int, *, prof=None) -> str | None:
    """Regenerate a fresh answer for question ``index`` only. None if the item or
    index is gone."""
    qs = get_questions(user_id, posting_id, prof=prof)
    if not (0 <= index < len(qs)):
        return None
    posting = jobstore.get_posting(user_id, posting_id)
    if posting is None:
        return None
    if prof is None:
        prof = profile_mod.get_profile(user_id)
    from . import applicant, knowledge

    answer = outreach.answer_application_question(
        qs[index]["question"], posting["company"] or "the company",
        posting["title"] or "Role", posting["description"], prof,
        identity_block=applicant.identity_block(user_id),
        knowledge_block=knowledge.knowledge_block(user_id),
    )
    qs[index]["answer"] = answer
    _save_questions(user_id, posting_id, qs)
    return answer


def _save_questions(user_id: str, posting_id: int, qs: list[dict]) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE apply_queue SET questions_json = ?, updated_at = ? "
            "WHERE user_id = ? AND posting_id = ?",
            (json.dumps(qs), _now(), user_id, posting_id),
        )
        return cur.rowcount > 0


def _decode_json(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def build_resume_bytes(user_id: str, posting_id: int) -> tuple[bytes, str] | None:
    """(pdf_bytes, filename) for the item's tailored resume, or None. Backed by the
    resume_store cache, so serving a download doesn't rebuild the PDF."""
    posting = jobstore.get_posting(user_id, posting_id)
    if posting is None:
        return None
    result = _build_resume(user_id, posting["company"] or "the company",
                           posting["title"] or "Role", posting)
    return (result.pdf_bytes, result.filename) if result else None


def _build_resume(user_id: str, company: str, title: str, posting):
    """Best-effort one-page tailored resume (a ``TailorResult``), or None when
    tailoring is disabled/unavailable. Cached by resume_store, so repeat calls for
    the same posting are cheap."""
    try:
        from . import resume_tailor

        return resume_tailor.tailor_for_posting(
            user_id, company, title, posting["description"], posting_id=posting["id"]
        )
    except Exception:  # noqa: BLE001 — packaging never hard-fails on the resume
        logger.warning("resume tailoring failed for posting %s", posting["id"],
                       exc_info=True)
        return None


_RESUME_NONE = "__none__"  # sentinel: tailoring was attempted and yielded nothing


def _ensure_resume(user_id: str, posting_id: int, company: str, title: str,
                   posting, cached: str | None) -> dict | None:
    """Resume metadata for the item — build + cache it on first request, then reuse.
    A sentinel records 'no resume' so we don't retry a disabled build every load."""
    if cached == _RESUME_NONE:
        return None
    meta = _decode_json(cached)
    if meta is not None:
        return meta
    result = _build_resume(user_id, company, title, posting)
    meta = ({"filename": result.filename, "variant": result.variant,
             "pages": result.pages} if result else None)
    with connect() as conn:
        conn.execute(
            "UPDATE apply_queue SET resume_path = ?, updated_at = ? "
            "WHERE user_id = ? AND posting_id = ?",
            (json.dumps(meta) if meta else _RESUME_NONE, _now(), user_id, posting_id),
        )
    return meta


# ---------------------------------------------------------------------------
# Web review surface
# ---------------------------------------------------------------------------

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Apply queue</title>
<style>
  :root{ --bg:#0f1117; --panel:#171a22; --line:#262b38; --ink:#e8ecf5;
         --dim:#9aa3b5; --acc:#6ea8fe; --ok:#16a34a; }
  *{ box-sizing:border-box; }
  body{ margin:0; background:var(--bg); color:var(--ink);
        font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; }
  .wrap{ max-width:640px; margin:0 auto; padding:20px 14px 80px; }
  h1{ font-size:20px; margin:0 0 2px; } .sub{ color:var(--dim); font-size:13px; margin:0 0 18px; }
  h2{ font-size:12px; text-transform:uppercase; letter-spacing:.5px; color:var(--dim);
      margin:24px 0 10px; }
  .card{ background:var(--panel); border:1px solid var(--line); border-radius:14px;
         padding:14px 15px; margin:0 0 11px; }
  .row{ display:flex; justify-content:space-between; gap:10px; align-items:baseline; }
  .title{ font-weight:600; font-size:16px; } .co{ color:var(--dim); font-size:13px; }
  .score{ color:var(--acc); font-size:12px; font-weight:700; white-space:nowrap; }
  .pill{ font-size:10px; padding:2px 8px; border-radius:999px; border:1px solid var(--line);
         color:var(--dim); text-transform:uppercase; letter-spacing:.4px; vertical-align:middle; }
  .pill.ready{ color:#fbbf24; border-color:#5b4a1f; } .pill.submitted{ color:var(--ok); border-color:#1f4a2f; }
  .actions{ margin-top:11px; display:flex; flex-wrap:wrap; gap:8px; }
  button,a.btn{ font:inherit; font-size:14px; font-weight:600; cursor:pointer; border-radius:9px;
          border:1px solid var(--line); background:#222838; color:var(--ink);
          padding:9px 13px; text-decoration:none; display:inline-block; }
  button.primary,a.primary{ background:#26406b; border-color:#34507f; }
  button.ghost{ background:transparent; color:var(--dim); }
  a.submit{ display:block; text-align:center; background:#1f6f43; border-color:#2f7d52;
            color:#fff; font-size:15px; padding:13px; margin-top:14px; border-radius:11px; }
  .pkg{ margin-top:12px; border-top:1px solid var(--line); padding-top:12px; display:none; }
  .pkg.show{ display:block; }
  .pkg h4{ margin:14px 0 7px; font-size:11px; color:var(--dim); text-transform:uppercase; letter-spacing:.4px; }
  .pkg h4:first-child{ margin-top:0; }
  .ident{ display:flex; flex-wrap:wrap; gap:6px; }
  .chip{ background:#10131b; border:1px solid var(--line); border-radius:8px;
         padding:5px 9px; font-size:12.5px; }
  .chip b{ color:var(--dim); font-weight:600; }
  textarea.ans{ width:100%; min-height:130px; background:#10131b; color:var(--ink);
                border:1px solid var(--line); border-radius:9px; padding:11px; font:inherit;
                font-size:14px; resize:vertical; }
  .abtns{ margin-top:8px; display:flex; gap:8px; }
  .note{ color:var(--dim); font-size:12px; margin-top:10px; }
  .empty{ color:var(--dim); font-style:italic; padding:8px 0; }
  .ok{ color:var(--ok); }
  .req{ margin-top:6px; }
  .reqbar{ display:flex; justify-content:space-between; align-items:center; gap:8px;
           font-size:13px; color:var(--ink); margin-top:8px; }
  a.submit{ cursor:pointer; }
</style></head><body><div class="wrap">
  <h1>Apply queue <span class="co">· __USER__</span></h1>
  <p class="sub">Each job is pre-assembled — your details, a drafted answer, and a
    tailored resume. Review, tweak, then open &amp; submit. Nothing is ever
    submitted for you.</p>

  <h2>Ready to apply</h2>
  <div id="queue"><div class="empty">loading…</div></div>

  <h2>Top matches — stage to apply</h2>
  <div id="matches"><div class="empty">loading…</div></div>

<script>
const USER = "__USER__";
const $ = (id)=>document.getElementById(id);
const esc=(s)=>{const d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML;};
const human=(k)=>k.replace(/_/g,' ').replace(/\\b\\w/g,c=>c.toUpperCase());
async function jpost(url, body){ const r=await fetch(url,{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); return r.json(); }

async function load(){
  const r = await fetch(`/apply/data?user=${encodeURIComponent(USER)}`);
  const d = await r.json();
  renderQueue(d.queue||[]); renderMatches(d.queued||[]);
}
function renderMatches(items){
  const el=$('matches');
  if(!items.length){ el.innerHTML='<div class="empty">No queued matches right now. New ones land here as discovery finds them.</div>'; return; }
  el.innerHTML = items.map(it=>`<div class="card"><div class="row">
      <div><div class="title">${esc(it.title)}</div><div class="co">${esc(it.company)} · ${esc(it.source)}</div></div>
      <div class="score">${Math.round((it.score||0)*100)}%</div></div>
      <div class="actions"><button class="primary" onclick="stage(${it.posting_id})">＋ Stage to apply</button>
      ${it.url?`<a class="btn" href="${esc(it.url)}" target="_blank" rel="noopener">Open posting ↗</a>`:''}</div></div>`).join('');
}
function renderQueue(items){
  const el=$('queue');
  if(!items.length){ el.innerHTML='<div class="empty">Nothing staged yet — stage a match below to prepare its application.</div>'; return; }
  el.innerHTML = items.map(it=>`<div class="card" id="c${it.posting_id}"><div class="row">
      <div><div class="title">${esc(it.title)} <span class="pill ${it.status}">${esc(it.status)}</span></div>
        <div class="co">${esc(it.company)} · ${esc(it.source)}</div></div>
      <div class="score">${Math.round((it.score||0)*100)}%</div></div>
      <div class="actions">
        <button class="primary" onclick="openPkg(${it.posting_id})">Review &amp; apply</button>
        ${it.auto_fillable
          ? `<button onclick="autoSubmit(${it.posting_id})">🤖 Auto-fill &amp; submit</button>`
          : (it.url ? `<a class="btn" href="${esc(it.url)}" target="_blank" rel="noopener"
               title="Not a directly fillable form (aggregator / login / captcha) — finish it on your computer with the browser extension"
               >↗ Open &amp; apply on desktop</a>` : '')}
        <button onclick="mark(${it.posting_id},'submitted')">✓ Submitted</button>
        <button class="ghost" onclick="remove(${it.posting_id})">Remove</button>
      </div>
      <div class="req" id="r${it.posting_id}"></div>
      <div class="pkg" id="p${it.posting_id}"></div></div>`).join('');
  items.forEach(it=>pollRequest(it.posting_id));
}
const REQ_LABEL = {pending:'⏳ Queued for the auto-filler…', filling:'⚙️ Filling the form…',
  preview:'🖼️ Filled — review &amp; approve below', approved:'✅ Approved — submitting…',
  submitting:'📤 Submitting…', submitted:'✅ Submitted', failed:'⚠️ Stopped'};
async function stage(pid){ await jpost('/apply/stage',{user:USER,posting_id:pid}); load(); }
async function autoSubmit(pid){
  $('r'+pid).innerHTML='<div class="note">starting the auto-filler…</div>';
  const res = await jpost('/apply/autosubmit',{user:USER,posting_id:pid});
  if(res && res.fillable===false){
    $('r'+pid).innerHTML = `<div class="note">This isn't a directly fillable form `
      + `(aggregator / login / captcha). `
      + (res.url?`<a href="${esc(res.url)}" target="_blank" rel="noopener">Open it on your computer ↗</a> `
                +`and finish with the browser extension.`
               :`Open it on your computer and finish with the browser extension.`)
      + `</div>`;
    return;
  }
  pollRequest(pid, true);
}
async function pollRequest(pid){
  let req;
  try{ req=(await (await fetch(`/apply/request?user=${encodeURIComponent(USER)}&posting_id=${pid}`)).json()).request; }
  catch(e){ return; }
  renderRequest(pid, req);
  if(req && ['pending','filling','approved','submitting'].includes(req.status))
    setTimeout(()=>pollRequest(pid), 3000);
}
function renderRequest(pid, req){
  const box=$('r'+pid); if(!box) return;
  if(!req || (req.status==='failed' && req.error==='cancelled')){ box.innerHTML=''; return; }
  let html = `<div class="reqbar"><span>${REQ_LABEL[req.status]||req.status}</span>`;
  if(['pending','filling','preview','approved','submitting'].includes(req.status))
    html += ` <button class="ghost" onclick="cancelReq(${pid},${req.id})">Cancel</button>`;
  html += `</div>`;
  if(req.status==='failed' && req.error && req.error!=='cancelled')
    html += `<div class="note">${esc(req.error)}</div>`;
  const pv = req.preview;
  if(req.status==='preview' && pv){
    const filled=(pv.filled||[]).map(f=>`<span class="chip"><b>${esc(f.label)}:</b> ${esc(f.value)}</span>`).join('');
    html += `<div class="note" style="margin-top:8px">It filled these — check them, then approve:</div>
      <div class="ident">${filled||'<span class="note">no fields filled</span>'}</div>`;
    if(pv.skipped && pv.skipped.length)
      html += `<div class="note">Left for you: ${esc(pv.skipped.join(', '))}</div>`;
    if(pv.screenshot_url)
      html += `<div style="margin-top:8px"><img src="${esc(pv.screenshot_url)}" style="max-width:100%;border-radius:8px;border:1px solid var(--line)"></div>`;
    html += `<a class="submit" onclick="approveReq(${pid},${req.id})">✅ Approve &amp; submit</a>
      <div class="note" style="text-align:center">It submits the form for you. Nothing is sent until you tap this.</div>`;
  }
  box.innerHTML = html;
}
async function approveReq(pid, rid){
  $('r'+pid).innerHTML='<div class="note">submitting…</div>';
  await jpost('/apply/request/approve',{user:USER,request_id:rid});
  pollRequest(pid);
}
async function cancelReq(pid, rid){
  await jpost('/apply/request/cancel',{user:USER,request_id:rid});
  pollRequest(pid);
}
async function mark(pid,st){ await jpost('/apply/mark',{user:USER,posting_id:pid,status:st}); load(); }
async function remove(pid){ await jpost('/apply/remove',{user:USER,posting_id:pid}); load(); }

async function openPkg(pid){
  const box=$('p'+pid);
  if(box.classList.contains('show')){ box.classList.remove('show'); box.innerHTML=''; return; }
  box.innerHTML='<div class="empty">assembling your application…</div>'; box.classList.add('show');
  const pkg=await jpost('/apply/package',{user:USER,posting_id:pid});
  if(pkg.error){ box.innerHTML='<div class="empty">Could not assemble.</div>'; return; }

  const ident = pkg.identity || {};
  const chips = Object.keys(ident).map(k=>`<span class="chip"><b>${esc(human(k))}:</b> ${esc(ident[k])}</span>`).join('');
  const identHtml = chips
    ? `<h4>What will fill the form</h4><div class="ident">${chips}</div>`
    : `<h4>Your details</h4><div class="note">No details saved yet — add them in the extension settings so they fill in here.</div>`;

  const resumeHtml = pkg.resume
    ? `<div class="note">📄 Tailored resume ready (${esc(pkg.resume.variant)}, ${pkg.resume.pages}p) —
        <a class="btn" href="/apply/resume?user=${encodeURIComponent(USER)}&id=${pid}">Download PDF</a></div>`
    : `<div class="note">No tailored resume (resume tailoring is off).</div>`;

  const qs = pkg.questions || [];
  const qHtml = qs.map((q,i)=>`
    <h4>${esc(q.question)}</h4>
    <textarea class="ans" id="ans${pid}_${i}">${esc(q.answer||'')}</textarea>
    <div class="abtns">
      <button onclick="saveAns(${pid},${i})">Save</button>
      <button onclick="copyAns(${pid},${i})">Copy</button>
      <button class="ghost" onclick="redraft(${pid},${i})">↻ Redraft</button>
      <span id="msg${pid}_${i}" class="note" style="margin:0;align-self:center"></span>
    </div>`).join('');

  box.innerHTML = identHtml
    + (qHtml || '<div class="note">No drafted answers yet.</div>')
    + resumeHtml
    + (pkg.url ? `<a class="submit" href="${esc(pkg.url)}" target="_blank" rel="noopener"
          onclick="setTimeout(()=>mark(${pid},'submitted'),500)">Open &amp; submit ↗</a>
          <div class="note" style="text-align:center">Opens the real form — you click submit there. We'll mark it submitted.</div>`
        : '');
}
async function saveAns(pid,i){
  await jpost('/apply/answer/save',{user:USER,posting_id:pid,index:i,answer:$('ans'+pid+'_'+i).value});
  flash(pid,i,'Saved ✓');
}
function copyAns(pid,i){ navigator.clipboard.writeText($('ans'+pid+'_'+i).value).then(()=>flash(pid,i,'Copied ✓')); }
async function redraft(pid,i){
  flash(pid,i,'Redrafting…');
  const r=await jpost('/apply/answer/redraft',{user:USER,posting_id:pid,index:i});
  if(r.answer){ $('ans'+pid+'_'+i).value=r.answer; flash(pid,i,'Fresh draft ✓'); }
  else flash(pid,i,'Could not redraft');
}
function flash(pid,i,t){ const m=$('msg'+pid+'_'+i); if(!m)return; m.textContent=t; m.className='note ok';
  setTimeout(()=>{ if(m) m.textContent=''; }, 2500); }
load();
</script></div></body></html>"""


def render_page(user_id: str) -> str:
    return _PAGE.replace("__USER__", user_id)
