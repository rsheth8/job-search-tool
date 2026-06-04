"""Conversational application engine.

A message no longer maps to a single isolated action. Instead the engine runs a
small multi-turn dialogue:

  * Intents that need information ("update", "note", "remind") collect their
    missing pieces one question at a time (slot filling).
  * The bot remembers what it just asked (`conversation.Pending`), so a bare
    reply ("spotify", "interview", "yes") is threaded back into that question.
  * yes/no/cancel and corrections ("no, figma") are understood.
  * A new, confident command can interrupt a half-finished one.

Repair contract from the spec still holds: never hard-fail; always store
something useful or ask exactly one short question.
"""
from __future__ import annotations

import re

from . import context as ctx
from . import conversation as convo
from . import deadlines as deadlines_mod
from . import discovery as discovery_mod
from . import jobstore
from . import outreach
from . import profile as profile_mod
from . import reminders
from . import scoring
from . import stats as stats_mod
from . import store
from .config import get_settings
from .conversation import Pending
from .intents import Intent, ParsedMessage
from .router import (
    clean_company,
    clean_role,
    get_router,
    normalize_status,
    parse_edit_change,
)

# Intents that collect slots before they can run.
SLOT_INTENTS = {
    Intent.APPLY, Intent.UPDATE, Intent.NOTE, Intent.REMIND, Intent.OUTREACH,
    Intent.DELETE,
}

# Required slots, asked in order. Role is always optional for APPLY.
REQUIRED: dict[Intent, list[str]] = {
    Intent.APPLY: ["company"],
    Intent.UPDATE: ["company", "status"],
    Intent.NOTE: ["company", "note"],
    Intent.REMIND: ["company", "time_reference"],
    Intent.OUTREACH: ["company"],
}

# The menu — shown on "help"/"menu"/"?" and (with a welcome line) on a greeting.
# Grouped so it scans fast on a phone; every line is a copy-pasteable example.
MENU = (
    "📋 Text me naturally — here's what I can do:\n"
    "\n"
    "▸ LOG & UPDATE\n"
    "  \"applied stripe swe\" — log an application\n"
    "  \"stripe oa received\" — update the stage\n"
    "  \"google rejected\" · \"ramp offer!\" — any stage\n"
    "\n"
    "▸ NOTES & RECRUITERS\n"
    "  \"note stripe recruiter was great\" — jot a note\n"
    "  \"reach out to a recruiter at stripe\" — find contacts + draft an intro\n"
    "\n"
    "▸ DATES\n"
    "  \"stripe oa due friday\" — set a deadline\n"
    "  \"remind me about google in 3 days\" — set a reminder\n"
    "\n"
    "▸ DISCOVER JOBS\n"
    "  \"I'm looking for new grad SWE roles, remote or NYC\" — set what to match\n"
    "  \"track openings at stripe\" — watch a company's board (I'll alert on new fits)\n"
    "  \"what am I tracking\" · \"stop tracking stripe\" — manage tracked boards\n"
    "  \"any new jobs\" — browse the latest matches I've found\n"
    "\n"
    "▸ SEE YOUR SEARCH\n"
    "  \"list\" — all applications\n"
    "  \"what did I apply to this week\" — recent applications\n"
    "  \"what's the status of stripe\" — check one application\n"
    "  \"what should I follow up on\" — top priorities\n"
    "  \"how am I doing\" — pipeline stats\n"
    "  \"what's coming up\" — your calendar\n"
    "\n"
    "▸ FIX THINGS\n"
    "  \"change the stripe role to SWE II\" — correct a saved entry\n"
    "  \"reject everything still in Applied\" — bulk update (I'll confirm)\n"
    "  \"delete stripe\" — remove an application (I'll confirm first)\n"
    "  \"undo\" — reverse my last change\n"
    "  \"no, I meant figma\" — correct me mid-answer · \"cancel\" — scrap it\n"
    "\n"
    "💡 Combine them: \"applied to notion and airtable, both PM\". "
    "Text \"help\" anytime to see this again."
)

HELP = MENU

GREETING = (
    "👋 Hey! I'm your job-search assistant — I track applications, deadlines, "
    "and follow-ups right here over text.\n\n" + MENU
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def handle_sms(user_id: str, text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "Text me something like 'applied stripe swe' — or 'help'."
    if convo.is_help(text):
        return HELP

    pending = convo.get_pending(user_id)
    actions = get_router().parse_actions(text)
    primary = actions[0] if actions else None

    if pending.active:
        if convo.is_cancel(text):
            convo.clear_pending(user_id)
            return "Okay, scrapped that. What's next?"
        if primary and not _looks_like_new_command(primary, text, pending):
            return _continue(user_id, pending, primary, text)
        # User pivoted to a clearly different command — drop the stale exchange.
        convo.clear_pending(user_id)

    if len(actions) > 1:
        return _start_multi(user_id, actions, text)
    return _start(user_id, primary, text)


# ---------------------------------------------------------------------------
# Routing helpers
# ---------------------------------------------------------------------------

def _slots_from_parsed(p: ParsedMessage) -> dict:
    return {
        "company": p.company,
        "role": p.role,
        "status": p.status,
        "note": p.message,
        "time_reference": p.time_reference,
    }


def _raw_slot_value(slot: str, p: ParsedMessage, raw: str) -> str:
    raw = raw.strip()
    if slot == "company":
        return p.company or clean_company(raw)
    if slot == "role":
        return p.role or clean_role(raw)
    if slot == "status":
        return p.status or normalize_status(raw) or raw
    if slot == "note":
        return p.message or raw
    if slot == "time_reference":
        return p.time_reference or raw
    return raw


def _looks_like_new_command(p: ParsedMessage, text: str, pending: Pending) -> bool:
    """Should an incoming message abandon the in-flight exchange?

    Conservative: answers (yes/no/corrections) never count. Navigation
    (list/query) always interrupts. A fully-specified command of a *different*
    intent interrupts; anything else is treated as an answer to the question.
    """
    if convo.is_affirmation(text) or convo.is_negation(text) or convo.is_correction(text):
        return False
    if (
        p.intent in (Intent.LIST, Intent.QUERY, Intent.STATS, Intent.DEADLINE,
                     Intent.CHECK, Intent.UNDO, Intent.JOBS, Intent.TRACK,
                     Intent.PROFILE)
        and p.confidence >= 0.7
    ):
        return True
    pend = Intent(pending.intent)
    if p.intent != pend:
        if p.intent == Intent.APPLY and p.company and p.confidence >= 0.85:
            return True
        if p.intent == Intent.UPDATE and p.company and p.status:
            return True
        if p.intent == Intent.DELETE and p.company and p.confidence >= 0.8:
            return True
        if p.intent in (Intent.EDIT, Intent.BULK) and p.confidence >= 0.8:
            return True
    return False


def _start(user_id: str, p: ParsedMessage, raw: str) -> str:
    memory = ctx.get_context(user_id)
    if p.intent in SLOT_INTENTS:
        slots = {k: v for k, v in _slots_from_parsed(p).items() if v}
        return _advance(user_id, p.intent, slots, p, raw, memory)
    if p.intent == Intent.LIST:
        return _do_list(user_id, p, memory)
    if p.intent == Intent.QUERY:
        return _do_query(user_id, p, memory)
    if p.intent == Intent.STATS:
        return stats_mod.render(stats_mod.compute_stats(user_id))
    if p.intent == Intent.DEADLINE:
        return _do_deadline(user_id, p, raw, memory)
    if p.intent == Intent.CHECK:
        return _do_check(user_id, p, memory)
    if p.intent == Intent.UNDO:
        return _do_undo(user_id)
    if p.intent == Intent.EDIT:
        return _start_edit(user_id, p, raw, memory)
    if p.intent == Intent.BULK:
        slots = {
            "new_status": p.status,
            "filter_stage": p.message,
            "age_ref": p.time_reference,
        }
        return _advance_bulk(user_id, slots, memory)
    if p.intent == Intent.TRACK:
        return _do_track(user_id, p, raw)
    if p.intent == Intent.JOBS:
        return _do_jobs(user_id)
    if p.intent == Intent.PROFILE:
        return _do_profile(user_id, p, raw)
    # UNKNOWN
    if convo.is_greeting(raw):
        return GREETING
    smalltalk = convo.smalltalk_reply(raw)
    if smalltalk:
        return smalltalk
    return _do_unknown(p, memory)


def _start_multi(user_id: str, actions: list[ParsedMessage], raw: str) -> str:
    """Run several actions from one combined SMS, in order.

    Each action flows through the normal single-action path, so context updates
    from earlier actions are visible to later ones (e.g. "applied to acme and
    add a note" resolves the note's company from the just-logged application).
    If an action needs a clarifying question it sets pending and we stop there,
    so we never stack multiple questions; any remaining actions are deferred.
    """
    replies: list[str] = []
    for action in actions:
        replies.append(_start(user_id, action, raw))
        if convo.get_pending(user_id).active:
            break  # this action asked a question — defer the rest
    return "\n\n".join(r for r in replies if r)


def _continue(user_id: str, pending: Pending, p: ParsedMessage, raw: str) -> str:
    intent = Intent(pending.intent)
    slots = dict(pending.slots)
    awaiting = pending.awaiting
    memory = ctx.get_context(user_id)

    # EDIT has bespoke slots ("edit_change") that don't fit the generic value
    # machinery, so it's resolved here in full (including any correction prefix).
    if intent == Intent.EDIT:
        return _continue_edit(user_id, slots, awaiting, raw)

    # "no, figma" / "actually google" — re-parse the remainder as the new value.
    if convo.is_correction(raw):
        corrected = convo.strip_correction_prefix(raw)
        cp = get_router().parse(corrected)
        for k, v in _slots_from_parsed(cp).items():
            if v:
                slots[k] = v  # corrections overwrite
        if awaiting and awaiting != "confirm" and not slots.get(awaiting):
            slots[awaiting] = _raw_slot_value(awaiting, cp, corrected)
        slots.pop("_needs_confirm", None)
        slots.pop("_confirmed", None)
        return _advance(user_id, intent, slots, cp, corrected, memory)

    if awaiting == "confirm":
        if convo.is_affirmation(raw):
            slots["_confirmed"] = True
            return _advance(user_id, intent, slots, p, raw, memory)
        if convo.is_negation(raw):
            convo.clear_pending(user_id)
            return "Got it — not logging that. Anything else?"
        if p.company:  # they answered with the actual company instead of yes/no
            slots["company"] = p.company
            if p.role:
                slots["role"] = p.role
            slots.pop("_needs_confirm", None)
            return _advance(user_id, intent, slots, p, raw, memory)
        return "Sorry — is that a yes or no?"

    # Awaiting a value slot.
    if convo.is_affirmation(raw) or convo.is_negation(raw):
        return _ask(intent, awaiting, slots)  # meaningless answer, re-ask
    for k, v in _slots_from_parsed(p).items():
        if v and not slots.get(k):  # fill empty slots; never clobber known ones
            slots[k] = v
    if awaiting and not slots.get(awaiting):
        slots[awaiting] = _raw_slot_value(awaiting, p, raw)
    return _advance(user_id, intent, slots, p, raw, memory)


def _advance(
    user_id: str, intent: Intent, slots: dict, p: ParsedMessage, raw: str, memory: dict
) -> str:
    """Fill what we can, ask for the next missing slot, or execute."""
    if intent == Intent.APPLY:
        return _advance_apply(user_id, slots, p, raw, memory)
    if intent == Intent.DELETE:
        return _advance_delete(user_id, slots, memory)
    if intent == Intent.BULK:
        return _advance_bulk(user_id, slots, memory)

    # For non-APPLY intents the company defaults silently to context.
    if not slots.get("company") and memory.get("last_company"):
        slots["company"] = memory["last_company"]

    for slot in REQUIRED[intent]:
        if not slots.get(slot):
            convo.set_pending(user_id, Pending(intent.value, slots, slot))
            return _ask(intent, slot, slots)

    return _execute(user_id, intent, slots, raw)


def _advance_apply(
    user_id: str, slots: dict, p: ParsedMessage, raw: str, memory: dict
) -> str:
    if not slots.get("company"):
        if memory.get("last_company"):
            # Don't silently log against context — confirm first (avoids dupes).
            slots["company"] = memory["last_company"]
            slots["_needs_confirm"] = True
        else:
            convo.set_pending(user_id, Pending(Intent.APPLY.value, slots, "company"))
            return "Which company did you apply to?"

    if not slots.get("_confirmed"):
        existing = store.find_application(
            user_id, slots["company"], role=slots.get("role")
        ) or store.find_application(user_id, slots["company"])
        if existing or slots.get("_needs_confirm"):
            convo.set_pending(user_id, Pending(Intent.APPLY.value, slots, "confirm"))
            role = f" — {slots['role']}" if slots.get("role") else ""
            if existing:
                return (
                    f"You already have {existing['company']} "
                    f"[{existing['status']}]. Log another application? (yes/no)"
                )
            return f"Apply to {slots['company']}{role}? (yes/no)"

    return _execute(user_id, Intent.APPLY, slots, raw)


def _advance_delete(user_id: str, slots: dict, memory: dict) -> str:
    """Delete needs a company and an explicit yes before anything is removed."""
    company = slots.get("company") or memory.get("last_company")
    if not company:
        convo.set_pending(user_id, Pending(Intent.DELETE.value, slots, "company"))
        return "Which application should I delete?"
    slots["company"] = company
    app = store.find_application(user_id, company)
    if app is None:
        convo.clear_pending(user_id)
        return f"I don't have {company} on file, so there's nothing to delete."
    if not slots.get("_confirmed"):
        convo.set_pending(user_id, Pending(Intent.DELETE.value, slots, "confirm"))
        role = f" — {app['role']}" if app["role"] else ""
        return (
            f"Delete {app['company']}{role} [{app['status']}] and its history? "
            "This can't be undone. (yes/no)"
        )
    return _execute(user_id, Intent.DELETE, slots, "")


def _advance_bulk(user_id: str, slots: dict, memory: dict) -> str:
    """Mass stage change. Always two-step: show the exact count + sample and
    require an explicit yes before touching anything (it's destructive)."""
    new_status = normalize_status(slots.get("new_status")) or slots.get("new_status")
    if not new_status:
        convo.clear_pending(user_id)
        return (
            "What status should I set them to? "
            "e.g. \"reject everything still in Applied\"."
        )
    slots["new_status"] = new_status
    matches = _bulk_matches(
        user_id,
        normalize_status(slots.get("filter_stage")) if slots.get("filter_stage") else None,
        slots.get("age_ref"),
    )
    if not matches:
        convo.clear_pending(user_id)
        return "Nothing matches that, so I haven't changed anything."

    if not slots.get("_confirmed"):
        convo.set_pending(user_id, Pending(Intent.BULK.value, slots, "confirm"))
        preview = "\n".join(
            f"• {a['company']} [{a['status']}]" for a in matches[:8]
        )
        more = f"\n…and {len(matches) - 8} more" if len(matches) > 8 else ""
        return (
            f"⚠️ Heads up — this changes {len(matches)} application(s) to "
            f"{new_status} and can't be undone:\n{preview}{more}\n\n"
            "Are you sure? (yes/no)"
        )
    return _execute(user_id, Intent.BULK, slots, "")


def _ask(intent: Intent, slot: str, slots: dict) -> str:
    company = slots.get("company")
    table = {
        (Intent.APPLY, "company"): "Which company did you apply to?",
        (Intent.UPDATE, "company"): "Which application should I update?",
        (Intent.UPDATE, "status"):
            f"What should I update {company} to?\n(e.g. OA received, interview, rejected)",
        (Intent.NOTE, "company"): "Which application is this note for?",
        (Intent.NOTE, "note"): f"What's the note for {company}?",
        (Intent.REMIND, "company"): "Remind you about which company?",
        (Intent.REMIND, "time_reference"):
            f"When should I remind you about {company}? (e.g. in 3 days)",
        (Intent.OUTREACH, "company"): "Reach out to a recruiter at which company?",
    }
    return table.get((intent, slot), f"What's the {slot}?")


# ---------------------------------------------------------------------------
# Terminal actions (all clear pending)
# ---------------------------------------------------------------------------

def _execute(user_id: str, intent: Intent, slots: dict, raw: str) -> str:
    convo.clear_pending(user_id)
    intent = intent if isinstance(intent, Intent) else Intent(intent)
    return {
        Intent.APPLY: _do_apply,
        Intent.UPDATE: _do_update,
        Intent.NOTE: _do_note,
        Intent.REMIND: _do_remind,
        Intent.OUTREACH: _do_outreach,
        Intent.DELETE: _do_delete,
        Intent.BULK: _do_bulk,
    }[intent](user_id, slots, raw)


def _do_apply(user_id: str, slots: dict, raw: str) -> str:
    company = slots["company"]
    role = slots.get("role")
    app = store.create_application(user_id, company, role, status="Applied", raw_sms=raw)
    ctx.set_context(user_id, company=company, role=role, application_id=app["id"])
    role_line = f"{company} — {role}" if role else company
    store.record_undo(
        user_id, "apply", {"app_id": app["id"], "company": company},
        f"logging {role_line}",
    )
    days = get_settings().default_followup_days
    return (
        f"Logged:\n{role_line}\nStatus: Applied\nNext follow-up: {days} days"
    )


def _do_update(user_id: str, slots: dict, raw: str) -> str:
    company = slots["company"]
    role = slots.get("role")
    status = normalize_status(slots["status"]) or slots["status"]
    app = store.find_application(user_id, company, role=role) or store.find_application(
        user_id, company
    )
    if app is None:
        new_app = store.create_application(
            user_id, company, role, status=status, raw_sms=raw
        )
        ctx.set_context(user_id, company=company, role=role, application_id=new_app["id"])
        store.record_undo(
            user_id, "apply", {"app_id": new_app["id"], "company": company},
            f"creating {company}",
        )
        return f"I didn't have {company} yet — created it.\n{company} → {status}"

    prev_status = app["status"]
    prev_lua = app["last_updated_at"]
    updated = store.update_status(app["id"], status, raw_sms=raw)
    store.record_undo(
        user_id, "status",
        {"app_id": app["id"], "prev_status": prev_status,
         "prev_last_updated_at": prev_lua,
         "event_id": store.last_event_id(app["id"], "status")},
        f"{app['company']} → {status} (it was {prev_status})",
    )
    ctx.set_context(
        user_id, company=app["company"], role=app["role"], application_id=app["id"]
    )
    extra = _maybe_deadline_from_update(
        user_id, app["company"], status, slots.get("time_reference"), app["id"]
    )
    role_line = (
        f"{updated['company']} {updated['role']}" if updated["role"] else updated["company"]
    )
    return f"Updated:\n{role_line} → {status}{extra}"


# Stages that are scheduled events worth putting on the calendar.
_SCHEDULED_STAGES = {"Phone screen", "Interview", "Onsite"}


def _maybe_deadline_from_update(
    user_id: str, company: str, status: str, time_reference, app_id: int
) -> str:
    """If an UPDATE named a date for a scheduled stage, also log a deadline.

    Lets "google onsite next tuesday" both advance the stage *and* land on the
    calendar, without the user issuing a separate deadline command.
    """
    if not time_reference or status not in _SCHEDULED_STAGES:
        return ""
    when = reminders.parse_time_reference(time_reference)
    if when is None:
        return ""
    deadlines_mod.create_deadline(
        user_id, company, status, when, application_id=app_id
    )
    return f"\n📅 {status} {_humanize_when(when)} — added to your calendar."


def _do_note(user_id: str, slots: dict, raw: str) -> str:
    company = slots["company"]
    note = slots["note"]
    memory = ctx.get_context(user_id)
    app = store.find_application(user_id, company)
    if app is None and memory.get("last_application_id"):
        app = store.get_application(memory["last_application_id"])
    if app is None:
        return f"I don't have {company} on file yet. Log it first with 'applied {company}'."
    prev_lua = app["last_updated_at"]
    store.add_note(app["id"], note, raw_sms=raw)
    store.record_undo(
        user_id, "note",
        {"app_id": app["id"], "event_id": store.last_event_id(app["id"], "note"),
         "prev_last_updated_at": prev_lua},
        f"the note on {app['company']}",
    )
    ctx.set_context(
        user_id, company=app["company"], role=app["role"], application_id=app["id"]
    )
    return f"Noted on {app['company']}:\n“{note}”"


def _do_remind(user_id: str, slots: dict, raw: str) -> str:
    company = slots["company"]
    time_ref = slots.get("time_reference")
    _row, when, parsed = reminders.schedule_for_company(
        user_id, company, time_ref,
        fallback_days=get_settings().default_followup_days,
    )
    pretty = _humanize_when(when)
    if parsed:
        return f"Got it — I'll remind you to follow up with {company} {pretty}."
    return (
        f"I couldn't pin down \"{time_ref or 'when'}\", so I set a reminder for "
        f"{company} {pretty}. Reply with a time to change it."
    )


def _humanize_when(when) -> str:
    from datetime import datetime, timezone

    delta = when - datetime.now(timezone.utc)
    days = round(delta.total_seconds() / 86400)
    if days <= 0:
        return "later today"
    if days == 1:
        return "tomorrow"
    if days < 7:
        return f"in {days} days"
    if days < 14:
        return "in about a week"
    return f"on {when.date().isoformat()}"


def _do_outreach(user_id: str, slots: dict, raw: str) -> str:
    company = slots["company"]
    app = store.find_application(user_id, company)
    app_id = app["id"] if app else None
    discovery = outreach.discover_for_company(user_id, company, application_id=app_id)
    recruiters = discovery.recruiters

    if not recruiters:
        from . import apollo as apollo_mod

        issue = apollo_mod.discovery_issue()
        if issue:
            return issue
        if not get_settings().apollo_enabled:
            return (
                f"To find recruiters at {company} I need an Apollo API key "
                "(set APOLLO_API_KEY in .env). Once it's set, text this again "
                "and I'll pull contacts and draft an intro."
            )
        return (
            f"I couldn't find recruiters at {company} right now. "
            "Try again later, or add a contact name and I'll draft something."
        )

    top = recruiters[0]
    role = app["role"] if app else None
    draft = outreach.draft_outreach(company, top, role=role)

    contact = top["name"] + (f" — {top['title']}" if top["title"] else "")
    link = f"\n{top['linkedin_url']}" if top["linkedin_url"] else ""
    more = f"\n(+{len(recruiters) - 1} more at {company})" if len(recruiters) > 1 else ""
    footnote = outreach.apollo_footnote(discovery)
    footnote_line = f"\n\n({footnote})" if footnote else ""
    return (
        f"Found {len(recruiters)} contact(s) at {company}. Top pick:\n"
        f"{contact}{link}{more}\n\n"
        f"Draft:\n{draft}\n\n"
        "Copy/paste to send — I won't send anything automatically."
        f"{footnote_line}"
    )


# ---------------------------------------------------------------------------
# Non-slot intents
# ---------------------------------------------------------------------------

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _apply_window(ref: str | None, now):
    """Map a window phrase to a [start, end) datetime range over applied_at.

    Returns None if the phrase isn't a recognizable past window (so LIST falls
    back to its normal all-applications behavior)."""
    from datetime import timedelta

    ref = (ref or "").strip().lower()
    if not ref:
        return None
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day - timedelta(days=now.weekday())
    if ref == "today":
        return (day, None)
    if ref == "yesterday":
        return (day - timedelta(days=1), day)
    if ref == "this week":
        return (week_start, None)
    if ref == "last week":
        return (week_start - timedelta(days=7), week_start)
    if ref == "this month":
        return (day.replace(day=1), None)
    if ref == "last month":
        first = day.replace(day=1)
        return ((first - timedelta(days=1)).replace(day=1), first)
    if ref == "this year":
        return (day.replace(month=1, day=1), None)
    if ref in ("recently", "lately"):
        return (day - timedelta(days=7), None)
    m = re.search(r"(\d+)\s*days?", ref)
    if m:
        return (now - timedelta(days=int(m.group(1))), None)
    m = re.search(r"since (\w+)", ref)
    if m and m.group(1) in _WEEKDAYS:
        delta = (now.weekday() - _WEEKDAYS[m.group(1)]) % 7
        return (day - timedelta(days=delta), None)
    return None


def _window_label(ref: str) -> str:
    label = ref.strip().lower()
    if re.match(r"(past|last)\s+\d+", label):
        return "in the " + label
    return label


def _do_recent(user_id: str, ref: str, window) -> str:
    start, end = window
    apps = store.applications_in_window(user_id, start, end)
    label = _window_label(ref)
    if not apps:
        return f"Nothing logged {label}. Text 'applied <company>' to add one."
    lines = [f"{len(apps)} application(s) {label}:"]
    for a in apps[:15]:
        role = f" — {a['role']}" if a["role"] else ""
        lines.append(f"• {a['company']}{role} [{a['status']}]")
    return "\n".join(lines)


def _do_list(user_id: str, p: ParsedMessage, memory: dict) -> str:
    if p.time_reference:
        window = _apply_window(p.time_reference, _now_utc())
        if window:
            return _do_recent(user_id, p.time_reference, window)
    apps = store.list_applications(user_id)
    target = normalize_status(p.status) if p.status else None
    # "list applied" == "list applications" (all); only filter on a later stage.
    if target and target != "Applied":
        apps = [a for a in apps if a["status"].lower() == target.lower()]
    if not apps:
        return "No applications yet. Text 'applied <company> <role>' to start."
    lines = ["Your applications:"]
    for a in apps[:15]:
        role = f" — {a['role']}" if a["role"] else ""
        lines.append(f"• {a['company']}{role} [{a['status']}]")
    return "\n".join(lines)


def _do_query(user_id: str, p: ParsedMessage, memory: dict) -> str:
    ranked = scoring.rank_followups(user_id)
    if not ranked:
        return "Nothing open to follow up on right now. \U0001f389"
    lines = ["Top to follow up on:"]
    for a, _score, b in ranked[:5]:
        role = f" — {a['role']}" if a["role"] else ""
        days = int(b["stale_days"])
        quiet = "today" if days == 0 else f"{days}d quiet"
        lines.append(f"• {a['company']}{role} [{a['status']}] — {quiet}")
    return "\n".join(lines)


def _do_jobs(user_id: str) -> str:
    posts = jobstore.list_postings(user_id, statuses=("alerted", "new"), limit=10)
    if not posts:
        if not jobstore.list_tracked(user_id):
            return ("You're not tracking any boards yet. Try "
                    "'track openings at <company>', then 'show my profile' to tune matches.")
        return "No new matching jobs yet — I'll ping you the moment one drops."
    lines = ["🆕 Newest matching jobs:"]
    for p in posts:
        score = p["relevance_score"]
        pct = f" · {round(score * 100)}%" if score is not None else ""
        loc = f" — {p['location']}" if p["location"] else ""
        lines.append(f"• #{p['id']} {p['title']} @ {p['company']}{loc}{pct}")
    return "\n".join(lines)


def _do_track(user_id: str, p: ParsedMessage, raw: str) -> str:
    action = (p.message or "").strip().lower()

    if action == "list":
        boards = jobstore.list_tracked(user_id)
        if not boards:
            return "Not tracking any companies yet. Try 'track openings at <company>'."
        lines = ["👀 Tracking:"]
        for b in boards:
            lines.append(f"• {b['company_name'] or b['board_token']} ({b['source']})")
        return "\n".join(lines)

    company = p.company
    if not company:
        return "Which company should I track? e.g. 'track openings at Stripe'."

    if action == "remove":
        n = jobstore.remove_tracked(user_id, company)
        return (f"Stopped tracking {company}." if n
                else f"You weren't tracking {company}.")

    board = discovery_mod.resolve_board(company)
    if board is None:
        return (f"Couldn't find a public job board for {company} on "
                f"{', '.join(get_settings().job_sources)}. It may use a different ATS.")
    row = jobstore.add_tracked_company(
        user_id, board["source"], board["board_token"], board["company_name"]
    )
    if row is None:
        return f"Already tracking {board['company_name']}."
    seeded = discovery_mod.seed_board(
        user_id, board["source"], board["board_token"], board["company_name"]
    )
    return (f"✅ Tracking {board['company_name']} ({board['source']}). Baselined "
            f"{seeded} current roles — I'll alert you on NEW matches from here.")


def _do_profile(user_id: str, p: ParsedMessage, raw: str) -> str:
    criteria = (p.message or "").strip()
    if not criteria:
        text = profile_mod.profile_text(profile_mod.get_profile(user_id))
        if not text:
            return ("No profile yet. Tell me what you're after, e.g. "
                    "\"I'm looking for new grad SWE roles, remote or NYC\".")
        return "🧭 Your job-search profile:\n" + text

    roles, locations = _split_profile_criteria(criteria)
    if not roles:
        return ("Tell me the roles you want, e.g. "
                "\"looking for new grad SWE roles, remote or NYC\".")
    profile_mod.set_profile(user_id, roles=roles, keywords=roles, locations=locations or None)
    saved = profile_mod.profile_text(profile_mod.get_profile(user_id))
    return ("Got it — I'll match new jobs against:\n" + saved +
            "\nNow track companies with 'track openings at <company>'.")


_PROFILE_LEADIN = re.compile(
    r"^\s*(i'?m looking for|i am looking for|looking for|i want|interested in|"
    r"set (?:my )?profile(?: to)?|update (?:my )?profile(?: to)?|find me)\s*",
    re.I,
)


def _split_profile_criteria(text: str) -> tuple[str, str]:
    """Best-effort split of freeform criteria into roles + locations.

    The full criteria is kept as the role/keyword string (so every term feeds
    matching); locations are *also* pulled out for the location bonus.
    """
    cleaned = _PROFILE_LEADIN.sub("", text.strip()).strip()
    locations: list[str] = []
    if re.search(r"\bremote\b", cleaned, re.I):
        locations.append("remote")
    m = re.search(r"\bin\s+(.+)$", cleaned, re.I)
    loc_clause = m.group(1) if m else (cleaned.rsplit(",", 1)[1] if "," in cleaned else "")
    for part in re.split(r"\s+or\s+|,|/", loc_clause):
        part = part.strip().strip(".")
        if part and part.lower() != "remote" and len(part) <= 30:
            locations.append(part)
    locs = ", ".join(dict.fromkeys(loc.lower() for loc in locations))
    return cleaned.rstrip(" ,.").strip(), locs


def _do_delete(user_id: str, slots: dict, raw: str) -> str:
    company = slots["company"]
    app = store.find_application(user_id, company)
    if app is None:
        return f"Nothing to delete for {company}."
    deleted_company = app["company"]
    store.delete_application(app["id"])
    # Tombstone, not a reversible record — so "undo" is honest about a delete
    # being gone rather than silently reversing the action before it.
    store.record_undo(
        user_id, "delete", {"company": deleted_company}, f"deleting {deleted_company}"
    )
    return f"🗑️ Deleted {deleted_company} and its history. Anything else?"


def _do_undo(user_id: str) -> str:
    """Reverse the most recent reversible action (single-level undo)."""
    u = store.get_undo(user_id)
    if not u:
        return "Nothing to undo yet — make a change and I can roll it back."
    kind, p, summary = u["kind"], u["payload"], u["summary"]

    if kind == "delete":
        store.clear_undo(user_id)
        company = p.get("company", "it")
        return (
            f"I can't undo a delete — {company} and its history are gone. "
            f"Re-add it with 'applied {company}'."
        )
    if kind == "apply":
        store.delete_application(p["app_id"])
    elif kind == "status":
        store.restore_application(p["app_id"], {
            "status": p["prev_status"],
            "last_updated_at": p["prev_last_updated_at"],
        })
        if p.get("event_id"):
            store.delete_event(p["event_id"])
    elif kind == "note":
        if p.get("event_id"):
            store.delete_event(p["event_id"])
        store.restore_application(
            p["app_id"], {"last_updated_at": p["prev_last_updated_at"]}
        )
    elif kind == "edit":
        prev = p["prev"]
        store.restore_application(p["app_id"], {
            "company": prev["company"], "role": prev["role"],
            "applied_at": prev["applied_at"],
            "last_updated_at": prev["last_updated_at"],
        })
        if p.get("event_id"):
            store.delete_event(p["event_id"])
    elif kind == "bulk":
        for c in p["changes"]:
            store.restore_application(c["app_id"], {
                "status": c["prev_status"],
                "last_updated_at": c["prev_last_updated_at"],
            })
            if c.get("event_id"):
                store.delete_event(c["event_id"])

    store.clear_undo(user_id)
    return f"↩️ Undone — reversed {summary}."


def _start_edit(user_id: str, p: ParsedMessage, raw: str, memory: dict) -> str:
    """Correct a stored application's role / name / applied date (not its stage).

    Multi-turn: if the company is missing we ask which app; if the company is
    known but *what* to change isn't, we ask that and remember (so a bare
    follow-up like "role to SWE II" is threaded back in)."""
    company = p.company or memory.get("last_company")
    new_name = p.message  # rename target (router convention)
    new_role = p.role
    new_date = reminders.parse_time_reference(p.time_reference) if p.time_reference else None
    if not company:
        convo.set_pending(
            user_id,
            Pending(Intent.EDIT.value,
                    {"role": new_role, "name": new_name, "date": p.time_reference},
                    "company"),
        )
        return (
            "Which application should I fix? "
            "e.g. \"change the stripe role to SWE II\"."
        )
    return _apply_edit(user_id, company, new_role, new_name, new_date, raw)


def _continue_edit(user_id: str, slots: dict, awaiting: str | None, raw: str) -> str:
    text = convo.strip_correction_prefix(raw) if convo.is_correction(raw) else raw
    if awaiting == "company":
        company = clean_company(text)
        new_date = (
            reminders.parse_time_reference(slots["date"]) if slots.get("date") else None
        )
        return _apply_edit(
            user_id, company, slots.get("role"), slots.get("name"), new_date, raw
        )
    # awaiting the change description ("role to SWE II", "call it Acme Inc", ...)
    company = slots.get("company")
    new_role, new_name, date_phrase = parse_edit_change(text)
    new_date = reminders.parse_time_reference(date_phrase) if date_phrase else None
    return _apply_edit(user_id, company, new_role, new_name, new_date, raw)


def _apply_edit(
    user_id: str, company: str | None, new_role, new_name, new_date, raw: str
) -> str:
    if not company:
        return "Which application should I fix?"
    app = store.find_application(user_id, company)
    if app is None:
        convo.clear_pending(user_id)
        return f"I don't have {company} on file to edit."
    if not (new_role or new_name or new_date):
        convo.set_pending(
            user_id, Pending(Intent.EDIT.value, {"company": app["company"]}, "edit_change")
        )
        return (
            f"What should I change about {app['company']} — "
            "its role, name, or applied date?"
        )

    # Snapshot everything the edit can touch so "undo" fully reverts it.
    prev = {
        "company": app["company"], "role": app["role"],
        "applied_at": app["applied_at"], "last_updated_at": app["last_updated_at"],
    }
    store.edit_application(
        app["id"], company=new_name, role=new_role,
        applied_at=new_date, raw_sms=raw,
    )
    event_id = store.last_event_id(app["id"], "edit")
    changes = []
    if new_name:
        changes.append(f"name → {new_name}")
    if new_role:
        changes.append(f"role → {new_role}")
    if new_date:
        changes.append(f"applied date → {new_date.date().isoformat()}")
    final = new_name or app["company"]
    store.record_undo(
        user_id, "edit",
        {"app_id": app["id"], "prev": prev, "event_id": event_id},
        f"the edit to {final}",
    )
    convo.clear_pending(user_id)
    ctx.set_context(user_id, company=final, role=new_role or app["role"],
                    application_id=app["id"])
    return f"✏️ Updated {final}: " + ", ".join(changes) + "."


# --- bulk helpers -----------------------------------------------------------

def _age_to_days(ref: str | None) -> float | None:
    """Turn an age phrase ('30 days', '1 month', '2 weeks') into a day count."""
    if not ref:
        return None
    ref = ref.lower()
    m = re.search(r"(\d+)", ref)
    n = int(m.group(1)) if m else 1
    if "month" in ref:
        return n * 30.0
    if "week" in ref:
        return n * 7.0
    if "day" in ref:
        return float(n)
    return None


def _bulk_matches(user_id: str, filter_stage: str | None, age_ref: str | None) -> list:
    """Open applications matching the bulk filter (current stage + min staleness).

    Terminal apps (Offer/Rejected/Ghosted) are never swept — they're already
    closed and shouldn't be silently re-stamped.
    """
    now = _now_utc()
    min_days = _age_to_days(age_ref)
    out = []
    for a in store.list_applications(user_id, limit=10_000):
        if a["status"] in scoring.TERMINAL_STATUSES:
            continue
        if filter_stage and a["status"].lower() != filter_stage.lower():
            continue
        if min_days is not None and scoring.days_since(a["last_updated_at"], now) < min_days:
            continue
        out.append(a)
    return out


def _do_bulk(user_id: str, slots: dict, raw: str) -> str:
    new_status = normalize_status(slots.get("new_status")) or slots.get("new_status")
    filter_stage = (
        normalize_status(slots.get("filter_stage")) if slots.get("filter_stage") else None
    )
    matches = _bulk_matches(user_id, filter_stage, slots.get("age_ref"))
    changes = []
    for a in matches:
        changes.append({
            "app_id": a["id"], "prev_status": a["status"],
            "prev_last_updated_at": a["last_updated_at"],
        })
        store.update_status(a["id"], new_status, raw_sms=raw or "bulk update")
    for c in changes:
        c["event_id"] = store.last_event_id(c["app_id"], "status")
    store.record_undo(
        user_id, "bulk", {"changes": changes},
        f"the bulk change of {len(changes)} application(s) to {new_status}",
    )
    return f"✅ Done — set {len(matches)} application(s) to {new_status}."


def _do_check(user_id: str, p: ParsedMessage, memory: dict) -> str:
    company = p.company or memory.get("last_company")
    if not company:
        return (
            "Which company? e.g. \"what's the status of stripe\". "
            "Or text \"list\" to see everything."
        )
    app = store.find_application(user_id, company, role=p.role) or \
        store.find_application(user_id, company)
    if app is None:
        return (
            f"I don't have {company} on file yet. "
            f"Want to add it? Text 'applied {company}'."
        )
    ctx.set_context(
        user_id, company=app["company"], role=app["role"], application_id=app["id"]
    )
    role_line = f"{app['company']} — {app['role']}" if app["role"] else app["company"]
    now = _now_utc()
    days = int(scoring.days_since(app["last_updated_at"], now))
    last = "today" if days == 0 else f"{days}d ago"
    lines = [f"📄 {role_line}", f"Status: {app['status']} (last update {last})"]

    # Most recent note, if any.
    notes = [e for e in store.list_events(app["id"]) if e["type"] == "note"]
    if notes:
        lines.append(f"Last note: “{notes[-1]['content']}”")

    # Next deadline for this company, if any.
    up = [
        d for d in deadlines_mod.upcoming(user_id)
        if d["company"].lower() == company.lower()
    ]
    if up:
        from datetime import datetime as _dt
        when = deadlines_mod._humanize(_dt.fromisoformat(up[0]["due_at"]), now)
        lines.append(f"📅 Next: {up[0]['label']} ({when})")
    elif app["status"] not in scoring.TERMINAL_STATUSES and app["next_follow_up_at"]:
        # No concrete deadline — show when the follow-up nudge is due instead.
        from datetime import datetime as _dt
        lines.append(
            f"⏰ Follow-up due {_humanize_when(_dt.fromisoformat(app['next_follow_up_at']))}"
        )

    return "\n".join(lines)


def _now_utc():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _do_deadline(user_id: str, p: ParsedMessage, raw: str, memory: dict) -> str:
    company = p.company or memory.get("last_company")
    when = reminders.parse_time_reference(p.time_reference) if p.time_reference else None

    if company and when:
        app = store.find_application(user_id, company)
        label = deadlines_mod.label_from(raw, p.status)
        deadlines_mod.create_deadline(
            user_id, company, label, when,
            application_id=app["id"] if app else None,
        )
        return (
            f"📅 Got it — {company} {label} due {_humanize_when(when)}. "
            "I'll nudge you a day before."
        )
    if company:  # named a company but no usable date
        return f"When's the {company} deadline? e.g. '{company} oa due friday'."
    # No company → show what's on the calendar.
    return deadlines_mod.render_upcoming(user_id)


def _do_unknown(p: ParsedMessage, memory: dict) -> str:
    if p.company:
        last = memory.get("last_company")
        anchor = f"\nLast mentioned: {last}." if last else ""
        return (
            f"Got '{p.company}' but I'm not sure what to do.\n"
            f"Did you mean to:\n1) apply  2) update status  3) add a note{anchor}"
        )
    return (
        "I didn't fully understand that.\n"
        "Did you mean:\n1) apply to a job\n2) update an application\n3) add a note\n"
        "(or text 'help')"
    )
