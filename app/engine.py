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
from . import horizon
from . import discovery as discovery_mod
from . import jobs_review
from . import jobstore
from . import outreach
from . import profile as profile_mod
from . import reminders
from . import scoring
from . import stats as stats_mod
from . import store
from . import voice
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
    Intent.APPLY, Intent.UPDATE, Intent.NOTE, Intent.REMIND,
    Intent.DELETE,
}

# Required slots, asked in order. Role is always optional for APPLY.
REQUIRED: dict[Intent, list[str]] = {
    Intent.APPLY: ["company"],
    Intent.UPDATE: ["company", "status"],
    Intent.NOTE: ["company", "note"],
    Intent.REMIND: ["company", "time_reference"],
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
    "▸ NOTES\n"
    "  \"note stripe recruiter was great\" — jot a note\n"
    "\n"
    "▸ DATES\n"
    "  \"stripe oa due friday\" — set a deadline\n"
    "  \"remind me about google in 3 days\" — set a reminder\n"
    "\n"
    "▸ DISCOVER JOBS\n"
    "  \"I'm looking for new grad SWE roles, remote or NYC\" — set what to match\n"
    "  \"track openings at stripe\" — watch one company's board\n"
    "  \"https://…/jobs/…\" — paste any job link (LinkedIn, Amazon, Workday…)\n"
    "  \"track feed hn-hiring\" — optional extra RSS feed\n"
    "  \"what am I tracking\" · \"stop tracking stripe\" — manage tracked boards\n"
    "  (Set your profile once — I'll also scan Amazon, Netflix, Workday, RSS, and ATS boards)\n"
    "  \"any new jobs\" — quick list of queued matches\n"
    "  \"review jobs\" — go through new matches one by one (skip / apply / stop)\n"
    "  \"apply 2\" — get the link + a drafted blurb for posting #2 (I log it)\n"
    "  \"queue 2\" · \"queue top 3\" — stage matches to your apply queue (prepared at /apply)\n"
    "  \"dismiss 2\" · \"snooze 2 for a week\" — clear a posting you're not into\n"
    "  \"only show 80%+ matches\" · \"be less picky\" — tune match strictness\n"
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
    "▸ THE APP\n"
    "  \"how do I autofill\" — Autofill, Submit, résumé attach\n"
    "  \"change my phone to 555…\" — update form details\n"
    "  \"take me to settings\" — jump to a tab\n"
    "\n"
    "💡 Combine them: \"applied to notion and airtable, both PM\". "
    "Text \"help\" anytime to see this again."
)

HELP = MENU

GREETING = f"Hey — {voice.HORIZON_BLURB}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def handle_sms(user_id: str, text: str) -> str:
    from . import llm_budget

    with llm_budget.for_user(user_id):
        return _handle_sms(user_id, text, pre_parsed=None)


def handle_action(user_id: str, parsed: ParsedMessage, text: str) -> str:
    """Dispatch a pre-parsed agent action through the same engine as chat."""
    from . import llm_budget

    with llm_budget.for_user(user_id):
        return _handle_sms(user_id, text, pre_parsed=parsed)


def _handle_sms(user_id: str, text: str, pre_parsed: ParsedMessage | None) -> str:
    text = (text or "").strip()
    if not text:
        return "Ask me to find jobs, edit your details, or how Autofill works — or say help."

    pending = convo.get_pending(user_id)
    actions = [pre_parsed] if pre_parsed is not None else get_router().parse_actions(text)
    primary = actions[0] if actions else None

    if (not pending.active and primary is not None
            and primary.intent == Intent.HELP_APP and primary.confidence >= 0.7):
        return _start(user_id, primary, text)

    # Bare "commands" / "menu" still dump the long list. Don't let "help" inside
    # a real command ("help me log stripe") steal the turn.
    if (pre_parsed is None and convo.is_help(text)
            and (primary is None or primary.intent == Intent.UNKNOWN
                 or primary.confidence < 0.5)):
        return HELP

    if pending.active and pending.intent == Intent.JOBS_REVIEW.value:
        # skip / apply / apply N / stop / dismiss all stay in the walkthrough;
        # a confident, clearly-different command (tune, dismiss N, track, …)
        # breaks out so the user isn't trapped mid-review.
        breakout = (
            primary is not None
            and primary.intent != Intent.APPLY_JOB  # "apply N" is a review control
            and _looks_like_new_command(primary, text, pending)
        )
        if not breakout:
            return jobs_review.continue_review(user_id, pending, text)
        convo.clear_pending(user_id)
        pending = convo.get_pending(user_id)  # now inactive — fall through to dispatch

    if pending.active and pending.awaiting == "go":
        breakout = (
            primary is not None
            and _looks_like_new_command(primary, text, pending)
        )
        if not breakout:
            return _continue(user_id, pending, primary or ParsedMessage(intent=Intent.UNKNOWN, confidence=0.1), text)
        convo.clear_pending(user_id)
        pending = convo.get_pending(user_id)

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
    if primary is None:
        primary = ParsedMessage(intent=Intent.UNKNOWN, confidence=0.1)
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
    if pending.awaiting == "go":
        if convo.is_hop_go(text) or convo.is_hop_stay(text):
            return False
        if p.intent == Intent.HELP_APP:
            from . import agent
            tab = agent.navigate_tab(p.message)
            if not tab or tab == pending.slots.get("tab"):
                return False
    if (
        p.intent in (Intent.LIST, Intent.QUERY, Intent.STATS, Intent.DEADLINE,
                     Intent.CHECK, Intent.UNDO, Intent.JOBS, Intent.JOBS_REVIEW,
                     Intent.TRACK, Intent.PROFILE, Intent.APPLY_JOB,
                     Intent.QUEUE_JOB, Intent.DISMISS_JOB, Intent.SNOOZE_JOB,
                     Intent.TUNE, Intent.HELP_APP, Intent.SET_IDENTITY)
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
    from .jobsources import ingest as ingest_mod

    # A pasted job URL is the easy path for LinkedIn / Indeed / random boards
    # we can't index. Don't steal "applied … https://…" (that's logging).
    if ingest_mod.is_job_link_message(raw) and p.intent not in (
        Intent.APPLY, Intent.UPDATE, Intent.NOTE, Intent.DELETE,
        Intent.EDIT, Intent.BULK, Intent.TRACK,
    ):
        return _do_save_link(user_id, raw)

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
    if p.intent == Intent.JOBS_REVIEW:
        return jobs_review.start_review(user_id)
    if p.intent == Intent.PROFILE:
        return _do_profile(user_id, p, raw)
    if p.intent == Intent.APPLY_JOB:
        return _do_apply_job(user_id, p, raw)
    if p.intent == Intent.QUEUE_JOB:
        return _do_queue_job(user_id, p)
    if p.intent == Intent.DISMISS_JOB:
        return _do_dismiss_job(user_id, p)
    if p.intent == Intent.SNOOZE_JOB:
        return _do_snooze_job(user_id, p)
    if p.intent == Intent.TUNE:
        return _do_tune(user_id, p)
    if p.intent == Intent.REMEMBER:
        return _do_remember(user_id, p)
    if p.intent == Intent.KNOWLEDGE:
        return _do_knowledge(user_id)
    if p.intent == Intent.HELP_APP:
        return _do_help_app(user_id, p)
    if p.intent == Intent.SET_IDENTITY:
        return _do_set_identity(user_id, p)
    # UNKNOWN
    if convo.is_greeting(raw):
        return voice.greeting_reply(user_id, raw)
    smalltalk = convo.smalltalk_reply(raw, name=voice.first_name(user_id))
    if smalltalk:
        return smalltalk
    return _do_unknown(user_id, p, raw, memory)


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
    if intent == Intent.SET_IDENTITY:
        return _continue_identity(user_id, slots, awaiting, p, raw)
    if intent == Intent.HELP_APP:
        return _continue_go(user_id, slots, awaiting, raw)

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
    updated = store.update_status(user_id, app["id"], status, raw_sms=raw)
    store.record_undo(
        user_id, "status",
        {"app_id": app["id"], "prev_status": prev_status,
         "prev_last_updated_at": prev_lua,
         "event_id": store.last_event_id(user_id, app["id"], "status")},
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
        app = store.get_application(user_id, memory["last_application_id"])
    if app is None:
        return f"I don't have {company} on file yet. Log it first with 'applied {company}'."
    prev_lua = app["last_updated_at"]
    store.add_note(user_id, app["id"], note, raw_sms=raw)
    store.record_undo(
        user_id, "note",
        {"app_id": app["id"], "event_id": store.last_event_id(user_id, app["id"], "note"),
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


def _row_get(row, key, default=None):
    """sqlite3.Row, dict, or JobPosting."""
    try:
        val = row[key]
        return default if val is None else val
    except Exception:
        return getattr(row, key, default)


def _speak_place(location: str | None) -> str:
    loc = (location or "").strip()
    if not loc:
        return ""
    if loc.lower() in ("remote", "hybrid", "anywhere", "worldwide"):
        return f", {loc.lower()}"
    return f" in {loc}"


def _speak_posting(p, score=None) -> str:
    """One role in a spoken sentence — never a Slack-style dump line."""
    title = str(_row_get(p, "title") or "a role").strip()
    company = str(_row_get(p, "company") or "a company").strip()
    loc = _row_get(p, "location") or ""
    sc = score if score is not None else _row_get(p, "relevance_score")
    pct = f" — {round(sc * 100)}% match" if sc is not None else ""
    return f"{title} at {company}{_speak_place(loc)}{pct}"


def _speak_app(a) -> str:
    role = a["role"]
    suffix = f" as {role}" if role else ""
    return f"{a['company']}{suffix} ({a['status']})"


def _do_recent(user_id: str, ref: str, window) -> str:
    start, end = window
    apps = store.applications_in_window(user_id, start, end)
    label = _window_label(ref)
    if not apps:
        return f"Nothing logged {label}. Say “applied Stripe SWE” to add one."
    named = [_speak_app(a) for a in apps[:3]]
    lead = f"{len(apps)} application{'s' if len(apps) != 1 else ''} {label}."
    if len(apps) == 1:
        return f"{lead} {named[0]}."
    extra = f" and {len(apps) - 3} more" if len(apps) > 3 else ""
    return f"{lead} {', '.join(named[:-1])}, and {named[-1]}{extra}."


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
        return "No applications yet. Mark Filed on Apply, or say “applied Stripe SWE”."
    named = [_speak_app(a) for a in apps[:3]]
    if len(apps) == 1:
        return f"You've logged one application: {named[0]}."
    extra = f" and {len(apps) - 3} more" if len(apps) > 3 else ""
    return (
        f"You've logged {len(apps)} applications. "
        f"{', '.join(named[:-1])}, and {named[-1]}{extra}."
    )


def _do_query(user_id: str, p: ParsedMessage, memory: dict) -> str:
    ranked = scoring.rank_followups(user_id)
    if not ranked:
        return "Nothing open to follow up on right now."
    a, _score, b = ranked[0]
    days = int(b["stale_days"])
    quiet = "today" if days == 0 else f"{days} day{'s' if days != 1 else ''} quiet"
    lead = f"I'd follow up on {_speak_app(a)} — {quiet}."
    if len(ranked) == 1:
        return lead
    rest = [_speak_app(row[0]) for row in ranked[1:3]]
    return lead + " Also " + ", then ".join(rest) + "."


def _do_jobs(user_id: str) -> str:
    jobstore.wake_snoozed(user_id, _now_utc().isoformat())  # resurface expired snoozes
    posts = jobstore.list_postings(
        user_id,
        statuses=("queued", "alerted"),
        limit=10,
        exclude_already_applied=True,
    )
    counts = jobstore.counts_by_status(user_id)
    total = counts.get("queued", 0) + counts.get("alerted", 0)
    if not posts:
        if not jobstore.list_tracked(user_id) and not profile_mod.has_profile(user_id):
            return (
                "You're not tracking anything yet. Tell me what you want — "
                "new-grad SWE, remote or NYC — or a company to watch."
            )
        return voice.with_name(
            user_id,
            "No jobs in your queue yet, {name} — I'll ping you when new matches land.",
            "No jobs in your queue yet — I'll ping you when new matches land.",
        )
    top = _speak_posting(posts[0])
    if total == 1:
        return (
            f"You've got one match: {top}.\n\n"
            "I can walk you through it, or you can open Apply."
        )
    bits = [f"You have {total} matches. The strongest is {top}."]
    if len(posts) > 1:
        bits.append(f"Next is {_speak_posting(posts[1])}.")
    rest = total - min(2, len(posts))
    if rest > 0:
        bits.append(f"{rest} more {'is' if rest == 1 else 'are'} waiting on Apply.")
    return (
        " ".join(bits)
        + "\n\nWant me to walk you through them, or open Apply?"
    )


def _do_track(user_id: str, p: ParsedMessage, raw: str) -> str:
    action = (p.message or "").strip().lower()

    if action == "list":
        stats = jobstore.board_stats(user_id)
        if not stats:
            return "You're not watching any companies yet. Name one and I'll track its board."
        n = len(stats)
        lead = f"You're watching {n} board{'s' if n != 1 else ''}."
        bits = []
        for s in stats[:4]:
            b = s["board"]
            name = b["company_name"] or b["board_token"]
            if s["fresh"]:
                bits.append(f"{name} has {s['fresh']} new")
            else:
                bits.append(f"{name} is quiet")
        extra = f", and {n - 4} more" if n > 4 else ""
        body = lead + " " + ", ".join(bits) + extra + "."
        counts = jobstore.counts_by_status(user_id)
        alerted = counts.get("queued", 0) + counts.get("alerted", 0)
        default = get_settings().job_relevance_threshold
        thresh = profile_mod.effective_threshold(profile_mod.get_profile(user_id), default)
        body += f" Matching at {round(thresh * 100)}%+."
        if alerted:
            body += f" {alerted} to review on Apply."
        return body

    company = p.company
    if action == "feed":
        if not company:
            from .jobsources import rss as rss_mod
            ids = ", ".join(rss_mod.list_feed_ids())
            return f"Which feed? e.g. 'track feed hn-hiring'. Available: {ids}"
        meta = discovery_mod.resolve_feed(company)
        if meta is None:
            return f"Unknown feed '{company}'. Try hn-hiring or remoteok."
        row = jobstore.add_tracked_company(
            user_id, "rss", meta["feed_id"], meta["label"]
        )
        if row is None:
            return f"Already tracking feed {meta['label']}."
        return (f"✅ Tracking RSS feed {meta['label']}. "
                "Wide discovery also runs default feeds when your profile is set.")

    if not company:
        return "Which company should I track? e.g. 'track openings at Stripe'."

    if action == "remove":
        n = jobstore.remove_tracked(user_id, company)
        return (f"Stopped tracking {company}." if n
                else f"You weren't tracking {company}.")

    from .jobsources import workday as workday_src

    wd_board = workday_src.parse_board(company) or workday_src.parse_board(raw)
    if wd_board and "workday" in get_settings().job_sources:
        known = workday_src.lookup_token(wd_board.token)
        board = {
            "source": "workday",
            "board_token": wd_board.token,
            "company_name": (known or {}).get("name") or wd_board.tenant.replace("-", " ").title(),
            "count": 0,
        }
    else:
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


def _do_save_link(user_id: str, raw: str) -> str:
    from . import ats
    from .jobsources import ingest as ingest_mod

    result = ingest_mod.save_pasted_job(user_id, raw)
    if not result.get("ok"):
        return result.get("error") or "Couldn't save that link."
    title = result.get("title") or "Saved job"
    company = result.get("company") or "that company"
    kind = result.get("apply_kind") or ats.apply_kind(result.get("url"), result.get("source"))
    already = "" if result.get("created") else " (already in your list)"
    extra = ""
    if kind == "browser":
        extra = (
            " Autofill won't drive LinkedIn/Indeed — open it in Apply, attach "
            "your resume, and you Submit."
        )
    elif kind == "direct":
        extra = " Open it in Apply; you still tap Submit."
    verb = "Saved" if result.get("created") else "That's"
    return (
        f"{verb} {title} @ {company}{already}. It's in Apply.{extra}"
    )


def _do_profile(user_id: str, p: ParsedMessage, raw: str) -> str:
    criteria = (p.message or "").strip()
    if not criteria:
        text = profile_mod.profile_text(profile_mod.get_profile(user_id))
        if not text:
            return ("No profile yet. Tell me what you're after, e.g. "
                    "\"I'm looking for new grad SWE roles, remote or NYC\".")
        return "Here's your search profile — what I'm matching on:\n\n" + text

    roles, locations = _split_profile_criteria(criteria)
    if not roles:
        return ("Tell me the roles you want, e.g. "
                "\"looking for new grad SWE roles, remote or NYC\".")
    profile_mod.set_profile(user_id, roles=roles, keywords=roles, locations=locations or None)
    from . import wide_discovery

    saved_prof = profile_mod.get_profile(user_id)
    wide_discovery.ensure_default_feeds_tracked(user_id, saved_prof)
    saved = profile_mod.profile_text(saved_prof)
    wide = wide_discovery.describe_wide_status(saved_prof)
    msg = "Got it — I'll match new jobs against:\n\n" + saved
    if wide:
        msg += f"\n\nWide discovery is on: {wide}"
    msg += (
        "\n\nYou don't need a company list — I'll poll feeds and rotate through "
        "job boards. Say “track openings at Stripe” for a favorite."
    )
    return msg


def _resolve_posting(user_id: str, p: ParsedMessage):
    """Find the posting an APPLY_JOB refers to — by '#<id>' or by company name."""
    pid = (p.message or "").strip()
    if pid.isdigit():
        return jobstore.get_posting(user_id, int(pid))
    if p.company:
        # Most relevant un-applied posting for that company (alerted/new only).
        for row in jobstore.list_postings(
            user_id,
            statuses=("alerted", "queued", "new"),
            limit=25,
            exclude_already_applied=True,
        ):
            if (row["company"] or "").lower() == p.company.lower():
                return row
    return None


def _do_apply_job(user_id: str, p: ParsedMessage, raw: str) -> str:
    """Assisted apply: hand back the apply link + a drafted blurb, log it as
    Applied, and mark the posting applied. We never auto-submit anything."""
    posting = _resolve_posting(user_id, p)
    if posting is None:
        pid = (p.message or "").strip()
        if pid.isdigit():
            return (
                f"I don't have job #{pid} on file. Say “show new jobs” to see "
                "the latest matches."
            )
        if p.company:
            return (
                f"I don't have an open posting from {p.company} to apply to. "
                "Say “show new jobs” to see what I've found."
            )
        return (
            "Which posting? Ask me to walk through your matches, "
            "or say “show new jobs”."
        )

    if posting["status"] == "applied":
        return (
            f"You've already applied to #{posting['id']} "
            f"({posting['title']} @ {posting['company']}).\n{posting['url'] or ''}".strip()
        )

    company = posting["company"] or "the company"
    title = posting["title"] or None
    prof = profile_mod.get_profile(user_id)
    draft = outreach.draft_application_answers(
        company, title or "", posting["description"], prof
    )

    app = store.create_application(
        user_id, company, title, status="Applied", source="discovery", raw_sms=raw
    )
    jobstore.mark_posting_status(user_id, posting["id"], "applied")
    ctx.set_context(user_id, company=company, role=title, application_id=app["id"])
    store.record_undo(
        user_id, "apply", {"app_id": app["id"], "company": company},
        f"applying to {title or company}",
    )

    role_line = f"{title} @ {company}" if title else company
    link_line = f"\n🔗 Apply here: {posting['url']}" if posting["url"] else ""

    resume_line = ""
    try:
        from . import resume_tailor

        tailored = resume_tailor.tailor_for_posting(
            user_id,
            company,
            title or "",
            posting["description"],
            posting_id=posting["id"],
        )
        if tailored:
            assert tailored.pages == 1
            if tailored.from_cache:
                resume_line = (
                    f"\n📎 Reusing saved resume ({tailored.variant.upper()}, "
                    f"1 page — already tailored for this role)."
                )
            else:
                resume_line = (
                    f"\n📎 Tailored resume ready ({tailored.variant.upper()}, "
                    "1 page — saved for next time). Open Apply to download it."
                )
    except Exception:
        import logging
        logging.getLogger("engine").exception("resume tailor failed; continuing without PDF")

    return (
        f"📝 {role_line}{link_line}\n\n"
        f"Draft \"why I'm a fit\" — tweak before you send:\n“{draft}”\n\n"
        f"Logged as Applied. I won't submit anything for you — paste the draft "
        f"into the application yourself.{resume_line}"
    )


def _posting_label(posting) -> str:
    title = posting["title"] or "role"
    company = posting["company"] or "?"
    return f"{title} @ {company}"


def _posting_not_found(p: ParsedMessage, verb: str) -> str:
    pid = (p.message or "").strip()
    if pid.isdigit():
        return f"I don't have job #{pid}. Say “show new jobs” to see the current matches."
    if p.company:
        return f"I don't have an open posting from {p.company} to {verb}."
    return (f"Which posting should I {verb}? Reply '{verb} <#>' with a number "
            "from a job alert.")


def _do_queue_job(user_id: str, p: ParsedMessage) -> str:
    """Stage a surfaced posting into the apply queue. We pre-assemble the
    application package (draft answers + tailored resume) for review on Apply —
    nothing is applied or submitted here."""
    spec = (p.message or "").strip()
    if spec == "all" or spec.startswith("top:"):
        n = None if spec == "all" else int(spec.split(":", 1)[1])
        return _do_queue_bulk(user_id, n)

    posting = _resolve_posting(user_id, p)
    if posting is None:
        return _posting_not_found(p, "queue")
    from . import apply_queue

    label = _posting_label(posting)
    if not apply_queue.stage(user_id, posting["id"]):
        from . import agent
        return agent.offer_tab_hop(
            user_id, f"job:{posting['id']}",
            f"#{posting['id']} ({label}) is already in your apply queue — "
            "Autofill, then you tap Submit. I never submit.",
        )
    from . import agent
    return agent.offer_tab_hop(
        user_id, f"job:{posting['id']}",
        f"Staged {label} to your apply queue. I'll have the application package "
        "ready — Autofill, then you tap Submit.",
    )


def _do_queue_bulk(user_id: str, n: int | None) -> str:
    """Stage the top ``n`` un-staged queued matches (or all of them), best score
    first — one-shot triage. Returns a count summary."""
    from . import apply_queue

    staged = {it["posting_id"] for it in apply_queue.list_queue(user_id)}
    fresh = [r for r in jobstore.list_review_queue(user_id) if r["id"] not in staged]
    if n is not None:
        fresh = fresh[:n]
    count = sum(1 for r in fresh if apply_queue.stage(user_id, r["id"]))
    if not count:
        from . import agent
        return agent.offer_tab_hop(
            user_id, "apply",
            "Nothing new to queue — your top matches are already staged.",
        )
    from . import agent
    return agent.offer_tab_hop(
        user_id, "apply",
        f"Staged {count} match{'es' if count != 1 else ''} to your apply "
        "queue. Autofill, then you tap Submit.",
    )


def _do_dismiss_job(user_id: str, p: ParsedMessage) -> str:
    posting = _resolve_posting(user_id, p)
    if posting is None:
        return _posting_not_found(p, "dismiss")
    jobstore.mark_posting_status(user_id, posting["id"], "dismissed")
    return f"👍 Dismissed #{posting['id']} ({_posting_label(posting)}) — won't surface it again."


def _do_snooze_job(user_id: str, p: ParsedMessage) -> str:
    posting = _resolve_posting(user_id, p)
    if posting is None:
        return _posting_not_found(p, "snooze")
    when = reminders.parse_time_reference(p.time_reference) if p.time_reference else None
    if when is None:
        from datetime import timedelta
        when = _now_utc() + timedelta(days=7)
    jobstore.snooze_posting(user_id, posting["id"], when.isoformat())
    return (f"😴 Snoozed #{posting['id']} ({_posting_label(posting)}) until "
            f"{when.date().isoformat()} — it'll resurface in 'any new jobs' then.")


# Words that hint at which kind of fact an un-categorised "remember …" is.
_CATEGORY_HINTS = (
    ("experience", re.compile(
        r"\bintern(?:ship)?\b|\bteaching assistant\b|\bambassador\b|"
        r"\bworked at\b|\bemployed\b|\bsoftware developer at\b|"
        r"\b[A-Z][a-zA-Z .]+,\s*[A-Z]{2}\b",
        re.I,
    )),
    ("project", re.compile(r"\bi (?:built|made|created|wrote|shipped|designed)\b|"
                           r"\bproject\b|\bapp\b|\bsystem\b", re.I)),
    ("achievement", re.compile(r"\bi (?:led|won|grew|cut|reduced|improved|increased|"
                               r"scaled|saved|launched)\b|\baward\b|\b\d+%", re.I)),
    ("preference", re.compile(r"\bi (?:want|prefer|like|need|enjoy)\b|"
                              r"\blooking for\b|\bideally\b", re.I)),
)


def _infer_category(text: str) -> str:
    """Guess what kind of fact this is when the user didn't label it. Wrong guesses
    are cheap — everything lands in the same grounding block either way."""
    for category, rx in _CATEGORY_HINTS:
        if rx.search(text):
            return category
    return "strength"


def _do_remember(user_id: str, p: ParsedMessage) -> str:
    """Store a durable fact about the user, so drafted answers get more specific
    every time they tell it something."""
    from . import knowledge

    parts = (p.message or "").split("|", 2)
    if parts[0] == "answer" and len(parts) == 3:
        _, question, text = parts
        if not knowledge.add(user_id, "answer", text, label=question):
            return "I need both a question and an answer to save — try again?"
        return (f"🧠 Saved your answer to “{question}”. I'll reuse it verbatim when "
                "that question comes up — no redraft, no cost.")

    category, text = (parts + [""])[:2] if len(parts) > 1 else ("", parts[0])
    text = text.strip()
    if not text:
        return ("Tell me what to remember — e.g. \"remember project: I built a "
                "real-time pricing service\" or \"remember I cut p99 latency 40%\".")
    category = category or _infer_category(text)
    if not knowledge.add(user_id, category, text):
        return "I couldn't store that — try 'remember project: …'."
    return (f"🧠 Got it ({category}). I'll use that when drafting your application "
            "answers. Say 'what do you know about me' to see everything.")


def _do_knowledge(user_id: str) -> str:
    """What it knows, and what it still needs — spoken, not a dump."""
    from . import knowledge

    items = knowledge.list_all(user_id)
    report = knowledge.audit(user_id)
    score = int(report["score"] * 100)
    parts = [f"Your form details are {score}% complete."]
    if items:
        sample = items[0]["text"]
        more = f", plus {len(items) - 1} more" if len(items) > 1 else ""
        parts.append(f"I've stored “{sample[:90]}”{more} to use on forms.")
    else:
        parts.append("Nothing stored yet — that's why drafted answers read generic.")
    missing = report.get("identity_missing") or []
    if missing:
        parts.append("Still missing " + ", ".join(missing[:6]) + ".")
    tips = report.get("suggestions") or []
    if tips:
        parts.append(str(tips[0]).rstrip(".") + ".")
    from . import agent

    if missing:
        dest = "you:identity"
    elif not items:
        dest = "you:add"
    else:
        dest = "you"
    return agent.offer_tab_hop(user_id, dest, " ".join(parts))


def _do_tune(user_id: str, p: ParsedMessage) -> str:
    spec = (p.message or "").strip().lower()
    default = get_settings().job_relevance_threshold
    prof = profile_mod.get_profile(user_id)
    current = profile_mod.effective_threshold(prof, default)

    if spec == "reset":
        profile_mod.set_min_relevance(user_id, None)
        return f"🎚️ Match threshold reset to the default ({round(default * 100)}%)."
    if spec == "all":
        new = 0.0
    elif spec.startswith("set:"):
        try:
            new = max(0.0, min(0.95, float(spec.split(":", 1)[1])))
        except ValueError:
            new = current
    elif spec == "loosen":
        new = round(max(0.0, current - 0.1), 2)
    elif spec == "tighten":
        new = round(min(0.95, current + 0.1), 2)
    else:
        return ("Tell me how to tune matching — e.g. \"only show 80%+ matches\", "
                "\"be less picky\", or \"reset matching\".")

    profile_mod.set_min_relevance(user_id, new)
    if new <= 0:
        return "🎚️ Showing every match now — I'll alert on all new postings."
    return f"🎚️ Match threshold set to {round(new * 100)}%. I'll alert on jobs scoring ≥ {new:.2f}."


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
    store.delete_application(user_id, app["id"])
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
        store.delete_application(user_id, p["app_id"])
    elif kind == "status":
        store.restore_application(user_id, p["app_id"], {
            "status": p["prev_status"],
            "last_updated_at": p["prev_last_updated_at"],
        })
        if p.get("event_id"):
            store.delete_event(user_id, p["event_id"])
    elif kind == "note":
        if p.get("event_id"):
            store.delete_event(user_id, p["event_id"])
        store.restore_application(
            user_id,
            p["app_id"], {"last_updated_at": p["prev_last_updated_at"]}
        )
    elif kind == "edit":
        prev = p["prev"]
        store.restore_application(user_id, p["app_id"], {
            "company": prev["company"], "role": prev["role"],
            "applied_at": prev["applied_at"],
            "last_updated_at": prev["last_updated_at"],
        })
        if p.get("event_id"):
            store.delete_event(user_id, p["event_id"])
    elif kind == "bulk":
        for c in p["changes"]:
            store.restore_application(user_id, c["app_id"], {
                "status": c["prev_status"],
                "last_updated_at": c["prev_last_updated_at"],
            })
            if c.get("event_id"):
                store.delete_event(user_id, c["event_id"])

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
        user_id, app["id"], company=new_name, role=new_role,
        applied_at=new_date, raw_sms=raw,
    )
    event_id = store.last_event_id(user_id, app["id"], "edit")
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
        store.update_status(user_id, a["id"], new_status, raw_sms=raw or "bulk update")
    for c in changes:
        c["event_id"] = store.last_event_id(user_id, c["app_id"], "status")
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
    notes = [e for e in store.list_events(user_id, app["id"]) if e["type"] == "note"]
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


def _do_help_app(user_id: str, p: ParsedMessage) -> str:
    from . import agent

    dest = agent.navigate_dest(p.message)
    if dest and dest.startswith("job:"):
        spec = dest.split(":", 1)[1]
        posting = _resolve_posting(
            user_id,
            ParsedMessage(
                intent=Intent.APPLY_JOB,
                message=spec if spec.isdigit() else None,
                company=None if spec.isdigit() else spec,
            ),
        )
        if posting is None:
            fake = ParsedMessage(
                intent=Intent.APPLY_JOB,
                message=spec if spec.isdigit() else None,
                company=None if spec.isdigit() else spec,
            )
            return _posting_not_found(fake, "open")
        label = _posting_label(posting)
        return agent.offer_tab_hop(
            user_id,
            f"job:{posting['id']}",
            f"{label} is on Apply. Autofill, then you tap Submit.",
        )
    reply = agent.help_reply(p.message)
    hop = dest or agent.help_hop_dest(p.message)
    if hop and hop != "chat":
        return agent.offer_tab_hop(user_id, hop, reply)
    return reply


def _continue_go(user_id: str, slots: dict, awaiting: str | None, raw: str) -> str:
    from . import agent

    tab = slots.get("tab") or "apply"
    name = agent.tab_label(tab)
    if awaiting != "go":
        return agent.offer_tab_hop(user_id, tab, agent.help_reply(f"tab:{tab}"))
    if convo.is_hop_stay(raw):
        slots["_declined_go"] = True
        slots.pop("_confirmed_go", None)
        convo.set_pending(
            user_id, Pending(Intent.HELP_APP.value, slots, "go"),
        )
        return f"No problem — I'll stay here. Ask when you're ready to open {name}."
    if convo.is_hop_go(raw):
        slots["_confirmed_go"] = True
        slots.pop("_declined_go", None)
        convo.set_pending(
            user_id, Pending(Intent.HELP_APP.value, slots, "go"),
        )
        return f"Okay — heading to {name}."
    return f"Want me to take you to {name}, or stay here?"


def _do_set_identity(user_id: str, p: ParsedMessage) -> str:
    slots = {"field": p.role, "value": p.message}
    return _finish_identity(user_id, slots)


def _continue_identity(
    user_id: str, slots: dict, awaiting: str | None, p: ParsedMessage, raw: str,
) -> str:
    from . import agent

    if awaiting == "field":
        field = agent.canonical_identity_field(p.role or raw)
        if not field:
            return ("Which detail should I change — phone, email, location, "
                    "LinkedIn, or GitHub?")
        slots["field"] = field
        if p.message:
            slots["value"] = p.message
    elif awaiting == "value":
        slots["value"] = (p.message or raw).strip()
    return _finish_identity(user_id, slots)


def _finish_identity(user_id: str, slots: dict) -> str:
    from . import agent, applicant

    field = agent.canonical_identity_field(slots.get("field"))
    value = slots.get("value")
    if isinstance(value, str):
        value = value.strip()
    if not field:
        convo.set_pending(
            user_id, Pending(Intent.SET_IDENTITY.value, slots, "field"),
        )
        return ("Which detail should I change — phone, email, location, "
                "LinkedIn, or GitHub?")
    slots["field"] = field
    if value in (None, ""):
        convo.set_pending(
            user_id, Pending(Intent.SET_IDENTITY.value, slots, "value"),
        )
        label = field.replace("_", " ")
        return f"What's the new {label}?"
    saved = applicant.set_identity(user_id, {field: value})
    convo.clear_pending(user_id)
    shown = saved.get(field)
    if isinstance(shown, bool):
        shown = "yes" if shown else "no"
    label = field.replace("_", " ")
    return f"Updated {label} to {shown}. I'll use that on forms."


#: Sentence openers the router's company extractor mistakes for company names.
#: It takes the first content token as the company when no role hint follows, so
#: "Which of these should I do first?" parsed as company="Which" and answered
#: 'Got "Which" but I'm not sure what to do' -- the same non-answer for every
#: real question anyone would type. A leading interrogative or filler is never a
#: company, and these turns are exactly the ones Horizon should get.
_NOT_A_COMPANY = frozenset({
    "actually", "also", "am", "and", "any", "anything", "anyway", "are", "but",
    "can", "could", "did", "do", "does", "hello", "hey", "hi", "hmm", "honestly",
    "how", "i", "is", "it", "just", "maybe", "me", "my", "ok", "okay", "please",
    "should", "so", "tell", "that", "the", "then", "there", "these", "they",
    "this", "those", "was", "were", "what", "when", "where", "which", "who",
    "why", "would", "you", "your",
})


def _looks_like_a_company(name: str | None) -> bool:
    head = (name or "").strip().split(" ")[0].lower()
    return bool(head) and head not in _NOT_A_COMPANY


def _do_unknown(user_id: str, p: ParsedMessage, raw: str, memory: dict) -> str:
    if p.company and _looks_like_a_company(p.company):
        last = memory.get("last_company")
        anchor = f"\nLast mentioned: {last}." if last else ""
        return (
            f"Got '{p.company}' but I'm not sure what to do.\n"
            f"Did you mean to:\n1) apply  2) update status  3) add a note{anchor}"
        )
    # Last stop before giving up. Horizon gets the turn with the person's real
    # profile, matches and applications attached; questions this app has the
    # facts to answer ("which should I do first?", "why this one?") used to die
    # here. Returns None with no API key or once the daily chat slice is spent,
    # which leaves the reply below exactly as it was.
    grounded = horizon.answer(user_id, raw)
    if grounded:
        return grounded
    return (
        "I didn't fully understand that. Try “show new jobs”, "
        "“how do I autofill”, or “change my phone to …”. "
        "Say “commands” if you want the full list."
    )
