"""Read-only HTML dashboard — the 'see everything' view SMS can't give.

Pure rendering: pulls from the same store/scoring/stats/deadlines/reminders
layers the SMS engine uses and returns a self-contained HTML string (no template
engine, one tiny inline script for the search box). Served at ``GET /``.

Single-user tool: defaults to the user_id with the most applications (so it
works whether you've been on the CLI as ``local`` or over SMS as a phone
number), overridable with ``?user=``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from . import deadlines as deadlines_mod
from . import jobstore, profile
from . import reminders, scoring, stats, store, theme
from .config import get_settings
from .db import connect
from .intents import CANONICAL_STATUSES

# Status → badge color.
_STATUS_COLORS = {
    "Applied": "#6b7280",
    "OA received": "#0891b2",
    "Phone screen": "#7c3aed",
    "Interview": "#2563eb",
    "Onsite": "#c026d3",
    "Offer": "#16a34a",
    "Rejected": "#dc2626",
    "Ghosted": "#9ca3af",
}
_EVENT_ICONS = {"created": "✅", "status": "🔁", "note": "📝"}


def default_user() -> str:
    """The user_id with the most applications, or 'local' if empty."""
    with connect() as conn:
        row = conn.execute(
            "SELECT user_id, COUNT(*) n FROM applications "
            "GROUP BY user_id ORDER BY n DESC LIMIT 1"
        ).fetchone()
        return row["user_id"] if row else "local"


def _all_recruiters(user_id: str) -> list:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM recruiters WHERE user_id = ? ORDER BY company, id",
            (user_id,),
        ).fetchall()


def _known_users() -> list[tuple[str, int]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT user_id, COUNT(*) n FROM applications "
            "GROUP BY user_id ORDER BY n DESC"
        ).fetchall()
        return [(r["user_id"], r["n"]) for r in rows]


def _fmt_date(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%b %d")
    except ValueError:
        return ""


def _badge(status: str) -> str:
    color = _STATUS_COLORS.get(status, "#6b7280")
    return (
        f'<span class="badge" style="background:{color}">{escape(status)}</span>'
    )


def render(user_id: str | None = None, *, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    user_id = user_id or default_user()

    s = stats.compute_stats(user_id, now=now)
    ranked = scoring.rank_followups(user_id, now=now)
    upcoming = deadlines_mod.upcoming(user_id, now=now)
    pending = reminders.list_pending(user_id)
    apps = store.list_applications(user_id, limit=10_000)
    recruiters = _all_recruiters(user_id)

    return (
        "<!doctype html><html lang='en'><head>"
        f"{theme.head('Job Search Dashboard', _CSS)}</head><body>"
        f"{theme.toggle_html()}"
        f"<header><div class='wrap'><h1>📈 Job Search</h1>"
        f"{_user_switch(user_id)}</div></header>"
        "<main>"
        f"{_stats_section(s)}"
        f"{_funnel_section(s)}"
        f"{_followups_section(ranked)}"
        f"{_deadlines_section(upcoming, now)}"
        f"{_reminders_section(pending, now)}"
        f"{_in_flight_section(user_id)}"
        f"{_knowledge_section(user_id)}"
        f"{_discovery_section(user_id)}"
        f"{_applications_section(apps, now)}"
        f"{_recruiters_section(recruiters)}"
        "</main>"
        f"<footer>Updated {now.strftime('%Y-%m-%d %H:%M UTC')}</footer>"
        f"<script>{theme.toggle_js()}{_JS}</script>"
        "</body></html>"
    )


def _user_switch(current: str) -> str:
    users = _known_users()
    if len(users) <= 1:
        return f"<div class='muted'>{escape(current)}</div>"
    links = " · ".join(
        f"<a href='/?user={escape(u)}'>{escape(u)} ({n})</a>" for u, n in users
    )
    return f"<div class='muted'>{links}</div>"


def _stats_section(s: dict) -> str:
    if s["total"] == 0:
        return (
            "<section><p class='empty'>No applications yet. Text "
            "<code>applied &lt;company&gt; &lt;role&gt;</code> or bulk-import "
            "(<code>cli.py import</code>).</p></section>"
        )
    cards = [
        ("Total", s["total"], ""),
        ("Active", s["active"], ""),
        ("Response", f"{s['response_rate']}%", ""),
        ("Interview+", f"{s['interview_rate']}%", ""),
        ("Offers", s["offers"], "good" if s["offers"] else ""),
        ("Going stale", s["stale"], "warn" if s["stale"] else ""),
    ]
    body = "".join(
        f"<div class='card {cls}'><div class='num'>{escape(str(v))}</div>"
        f"<div class='lbl'>{escape(label)}</div></div>"
        for label, v, cls in cards
    )
    return f"<section><div class='cards'>{body}</div></section>"


def _funnel_section(s: dict) -> str:
    total = s["total"]
    if not total:
        return ""
    segs, legend = [], []
    for stage in CANONICAL_STATUSES:
        n = s["by_stage"].get(stage, 0)
        if not n:
            continue
        pct = 100 * n / total
        color = _STATUS_COLORS.get(stage, "#6b7280")
        label = f"{n}" if pct >= 8 else ""
        segs.append(
            f"<div class='seg' style='width:{pct:.1f}%;background:{color}' "
            f"title='{escape(stage)}: {n}'>{label}</div>"
        )
        legend.append(
            f"<span class='lg'><i style='background:{color}'></i>"
            f"{escape(stage)} {n}</span>"
        )
    return (
        "<section><h2>Pipeline</h2>"
        f"<div class='funnel'>{''.join(segs)}</div>"
        f"<div class='legend'>{''.join(legend)}</div></section>"
    )


def _followups_section(ranked: list) -> str:
    if not ranked:
        return ""
    rows = []
    for a, _score, b in ranked[:10]:
        role = f" — {escape(a['role'])}" if a["role"] else ""
        days = int(b["stale_days"])
        quiet = "today" if days == 0 else f"{days}d quiet"
        flag = " 🤝" if b.get("recruiter_component") else ""
        rows.append(
            f"<tr><td>{escape(a['company'])}{role}{flag}</td>"
            f"<td>{_badge(a['status'])}</td><td class='muted'>{quiet}</td></tr>"
        )
    return f"<section><h2>Follow up next</h2><table>{''.join(rows)}</table></section>"


def _deadlines_section(upcoming: list, now: datetime) -> str:
    if not upcoming:
        return ""
    rows = []
    for d in upcoming:
        due = datetime.fromisoformat(d["due_at"])
        when = deadlines_mod._humanize(due, now)
        soon = "warn" if (due.date() - now.date()).days <= 2 else ""
        rows.append(
            f"<tr><td>{escape(d['company'])}</td><td>{escape(d['label'])}</td>"
            f"<td class='muted {soon}'>{escape(when)}</td></tr>"
        )
    return f"<section><h2>📅 Upcoming</h2><table>{''.join(rows)}</table></section>"


def in_flight_rows(user_id: str) -> list[dict]:
    """Every unfinished fill request as ``{id, label, state, awaiting}`` — the same
    data the Slack "in flight" reply is built from, so the phone and the computer
    can never disagree about what's happening."""
    from . import fill_requests, jobstore

    rows = []
    for req in fill_requests.list_active(user_id):
        posting = jobstore.get_posting(user_id, req["posting_id"])
        label = (f"{posting['title'] or 'Role'} @ {posting['company'] or '?'}"
                 if posting is not None else f"posting #{req['posting_id']}")
        rows.append({
            "id": req["posting_id"],
            "label": label,
            "state": _FILL_STATES.get(req["status"], req["status"]),
            "awaiting": req["status"] == "preview",
        })
    return rows


_FILL_STATES = {
    "pending": "queued for the worker",
    "filling": "filling the form",
    "preview": "waiting on your approval",
    "approved": "approved — submitting",
    "submitting": "submitting",
}


def _in_flight_section(user_id: str) -> str:
    """Applications the submit worker is currently handling. Read-only: approving
    stays an explicit action on /apply or in Slack, never a dashboard click."""
    rows = in_flight_rows(user_id)
    if not rows:
        return ""
    body = []
    for r in rows:
        cta = ("<a href='/apply'>review &amp; approve</a>" if r["awaiting"]
               else f"<span class='muted'>{escape(r['state'])}</span>")
        flag = "warn" if r["awaiting"] else ""
        body.append(
            f"<tr><td>#{r['id']}</td><td>{escape(r['label'])}</td>"
            f"<td class='muted {flag}'>{escape(r['state'])}</td><td>{cta}</td></tr>"
        )
    return ("<section><h2>🤖 In flight</h2>"
            f"<table>{''.join(body)}</table></section>")


def _knowledge_section(user_id: str) -> str:
    """How completely the assistant knows you — the lever on how much it can fill
    without asking. Shown only while something's still missing."""
    from . import knowledge

    report = knowledge.audit(user_id)
    if not report["identity_missing"] and not report["suggestions"]:
        return ""
    pct = int(report["score"] * 100)
    flag = " warn" if pct < 70 else ""
    missing = escape(", ".join(report["identity_missing"][:8])) or "—"
    rows = [f"<tr><td>Identity</td><td class='muted'>{pct}% complete</td>"
            f"<td class='muted{flag}'>{missing}</td></tr>"]
    counts = report["knowledge_counts"]
    known = ", ".join(f"{n} {c}{'s' if n != 1 else ''}"
                      for c, n in counts.items() if n) or "nothing yet"
    rows.append(f"<tr><td>Background</td><td class='muted'>{escape(known)}</td>"
                f"<td class='muted'>{escape(report['suggestions'][0]) if report['suggestions'] else '—'}</td></tr>")
    return ("<section><h2>🧠 What I know about you</h2>"
            f"<table>{''.join(rows)}</table></section>")


def _reminders_section(pending: list, now: datetime) -> str:
    if not pending:
        return ""
    rows = []
    for r in pending:
        body = (r["body"] or "").split("\n")[0]
        rows.append(
            f"<tr><td>{escape(body)}</td>"
            f"<td class='muted'>{_fmt_date(r['remind_at'])}</td></tr>"
        )
    return f"<section><h2>⏰ Reminders</h2><table>{''.join(rows)}</table></section>"


def _applications_section(apps: list, now: datetime) -> str:
    if not apps:
        return ""
    items = []
    for a in apps:
        role = f" — {escape(a['role'])}" if a["role"] else ""
        days = int(scoring.days_since(a["last_updated_at"], now))
        quiet = "today" if days == 0 else f"{days}d"
        search = escape(f"{a['company']} {a['role'] or ''}".lower())
        history = "".join(
            f"<li><span class='ic'>{_EVENT_ICONS.get(e['type'], '•')}</span>"
            f"{escape(e['content'] or e['type'])} "
            f"<span class='muted'>{_fmt_date(e['timestamp'])}</span></li>"
            for e in store.list_events(a["id"])
        )
        items.append(
            f"<details data-search='{search}'>"
            f"<summary>{_badge(a['status'])} <b>{escape(a['company'])}</b>"
            f"{role} <span class='muted'>· {quiet}</span></summary>"
            f"<ul class='timeline'>{history or '<li class=muted>No history.</li>'}</ul>"
            "</details>"
        )
    return (
        "<section><h2>Applications</h2>"
        "<input id='q' class='search' placeholder='Filter by company or role…'>"
        f"<div class='apps'>{''.join(items)}</div></section>"
    )


def _discovery_section(user_id: str) -> str:
    """Tracked boards + the freshest matched postings discovery has surfaced."""
    board_stats = jobstore.board_stats(user_id)
    posts = jobstore.list_postings(
        user_id, statuses=("alerted", "new", "applied"), limit=15
    )
    if not board_stats and not posts:
        return ""

    parts = ["<section><h2>🔎 Job discovery</h2>"]

    if board_stats:
        default = get_settings().job_relevance_threshold
        thresh = profile.effective_threshold(profile.get_profile(user_id), default)
        rows = []
        for s in board_stats:
            b = s["board"]
            name = escape(b["company_name"] or b["board_token"])
            fresh = (f"<b>{s['fresh']}</b> new" if s["fresh"]
                     else "<span class='muted'>0 new</span>")
            rows.append(
                f"<tr><td>{name} <span class='muted'>({escape(b['source'])})</span></td>"
                f"<td>{fresh}</td><td class='muted'>{s['total']} seen</td></tr>"
            )
        parts.append(
            f"<p class='muted'>Matching ≥ {round(thresh * 100)}% relevance</p>"
            f"<table>{''.join(rows)}</table>"
        )

    if posts:
        rows = []
        for p in posts:
            score = p["relevance_score"]
            pct = f"{round(score * 100)}%" if score is not None else "—"
            loc = f" · {escape(p['location'])}" if p["location"] else ""
            title = escape(p["title"] or "role")
            company = escape(p["company"] or "")
            link = (f"<a href='{escape(p['url'])}' target='_blank' rel='noopener'>{title} ↗</a>"
                    if p["url"] else title)
            tag = " <span class='muted'>(applied)</span>" if p["status"] == "applied" else ""
            rows.append(
                f"<tr><td class='muted'>#{p['id']}</td>"
                f"<td>{link}{tag}<div class='muted'>{company}{loc}</div></td>"
                f"<td><b>{pct}</b></td></tr>"
            )
        parts.append(f"<h3 class='sub'>Latest matches</h3><table>{''.join(rows)}</table>")

    parts.append("</section>")
    return "".join(parts)


def _recruiters_section(recruiters: list) -> str:
    if not recruiters:
        return ""
    rows = []
    for r in recruiters:
        title = escape(r["title"]) if r["title"] else ""
        link = (
            f"<a href='{escape(r['linkedin_url'])}' target='_blank' rel='noopener'>profile ↗</a>"
            if r["linkedin_url"] else ""
        )
        rows.append(
            f"<tr><td>{escape(r['company'])}</td><td>{escape(r['name'])}</td>"
            f"<td class='muted'>{title}</td><td>{link}</td></tr>"
        )
    return f"<section><h2>🤝 Recruiters</h2><table>{''.join(rows)}</table></section>"


# Layout only — every colour comes from app/theme.py so this page follows the
# light/dark toggle instead of being permanently light like it used to be.
_CSS = (
    "header{background:var(--panel);border-bottom:1px solid var(--line);padding:18px 0}"
    "header .wrap{display:flex;align-items:baseline;justify-content:space-between;"
    "flex-wrap:wrap;gap:8px}"
    "header h1{margin:0}"
    "header a{text-decoration:none}"
    "main{max-width:960px;margin:0 auto;padding:8px 16px 40px}"
    "section{margin:22px 0}"
    "h2{border-bottom:1px solid var(--line);padding-bottom:6px}"
    "h3.sub{font-size:13px;color:var(--dim);margin:14px 0 6px;font-weight:600}"
    # The stage badge keeps its per-status colour (set inline from the funnel
    # palette); white text is legible on all of them in both modes.
    ".badge{color:#fff}"
    ".cards{display:flex;gap:12px;flex-wrap:wrap}"
    ".card{padding:14px 18px;min-width:96px;text-align:center;flex:1}"
    ".card .num{font-size:26px;font-weight:700;letter-spacing:-.02em}"
    ".card .lbl{font-size:12px;color:var(--dim)}"
    ".card.good .num{color:var(--ok)}.card.warn .num{color:var(--warn)}"
    ".funnel{display:flex;height:30px;border-radius:var(--radius-sm);overflow:hidden;"
    "box-shadow:var(--shadow)}"
    ".funnel .seg{display:flex;align-items:center;justify-content:center;color:#fff;"
    "font-size:12px;font-weight:600;min-width:2px}"
    ".legend{margin-top:10px;display:flex;flex-wrap:wrap;gap:12px;font-size:12px;"
    "color:var(--dim)}"
    ".legend .lg{display:inline-flex;align-items:center;gap:5px}"
    ".legend i{width:10px;height:10px;border-radius:3px;display:inline-block}"
    ".warn{color:var(--warn);font-weight:600}"
    ".empty{background:var(--panel);border:1px solid var(--line);"
    "border-radius:var(--radius);padding:18px;color:var(--dim)}"
    ".search{margin-bottom:10px}"
    ".apps details{background:var(--panel);border:1px solid var(--line);"
    "border-radius:var(--radius-sm);margin-bottom:6px}"
    ".apps summary{padding:10px 13px;cursor:pointer;list-style:none;display:flex;"
    "align-items:center;gap:8px}"
    ".apps summary::-webkit-details-marker{display:none}"
    ".timeline{margin:0;padding:4px 16px 12px 40px;font-size:14px}"
    ".timeline li{margin:3px 0}.timeline .ic{margin-left:-22px;margin-right:8px}"
    "footer{text-align:center;color:var(--dim);font-size:12px;padding:24px}"
)

_JS = (
    "var q=document.getElementById('q');"
    "if(q){q.addEventListener('input',function(){"
    "var v=q.value.toLowerCase();"
    "document.querySelectorAll('.apps [data-search]').forEach(function(el){"
    "el.style.display=el.getAttribute('data-search').indexOf(v)>-1?'':'none';});"
    "});}"
)
