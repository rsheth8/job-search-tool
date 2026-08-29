"""In-app assistant helpers: structured actions, identity fields, how-to copy.

The on-device classifier (and POST /agent) emit an action + slots. This module
maps that onto ParsedMessage, answers HELP_APP from a fixed FAQ (never invented
job lists), and attaches chips / deep links for the iOS Assistant UI.

Paid models are not used here.
"""
from __future__ import annotations

import re

from .intents import Intent, ParsedMessage
from .router import normalize_status

# Human phrases -> applicant.FIELDS keys. Longer aliases first when matching.
_IDENTITY_ALIASES: tuple[tuple[str, str], ...] = (
    ("first name", "first_name"),
    ("last name", "last_name"),
    ("full name", "full_name"),
    ("preferred name", "preferred_name"),
    ("graduation year", "grad_year"),
    ("grad year", "grad_year"),
    ("years of experience", "years_experience"),
    ("years experience", "years_experience"),
    ("current company", "current_company"),
    ("current title", "current_title"),
    ("work authorization", "work_authorized"),
    ("work authorized", "work_authorized"),
    ("needs sponsorship", "needs_sponsorship"),
    ("sponsorship", "needs_sponsorship"),
    ("willing to relocate", "willing_to_relocate"),
    ("work arrangement", "work_arrangement"),
    ("start date", "start_date"),
    ("salary expectation", "salary_expectation"),
    ("linkedin", "linkedin"),
    ("github", "github"),
    ("portfolio", "portfolio"),
    ("website", "portfolio"),
    ("phone", "phone"),
    ("cell", "phone"),
    ("mobile", "phone"),
    ("email", "email"),
    ("e-mail", "email"),
    ("location", "location"),
    ("address", "address"),
    ("city", "city"),
    ("state", "state"),
    ("country", "country"),
    ("zip code", "zip"),
    ("zipcode", "zip"),
    ("postal", "zip"),
    ("zip", "zip"),
    ("school", "school"),
    ("university", "school"),
    ("college", "school"),
    ("degree", "degree"),
    ("discipline", "discipline"),
    ("major", "discipline"),
    ("gpa", "gpa"),
    ("pronouns", "pronouns"),
    ("firstname", "first_name"),
    ("lastname", "last_name"),
    ("name", "full_name"),
)

_ALIAS_BY_LEN = tuple(sorted(_IDENTITY_ALIASES, key=lambda kv: -len(kv[0])))

# Canonical deep links the iOS client understands after a confirmed hop.
_DEST_ALIASES = {
    "apply": "apply",
    "queue": "apply",
    "matches": "apply",
    "filed": "apply:filed",
    "applications": "apply:filed",
    "you": "you",
    "about": "you",
    "profile": "you",
    "knowledge": "you",
    "identity": "you:identity",
    "forms": "you:identity",
    "details": "you:identity",
    "search": "you:search",
    "looking": "you:search",
    "roles": "you:search",
    "criteria": "you:search",
    "add": "you:add",
    "fact": "you:add",
    "projects": "you:projects",
    "experience": "you:experience",
    "import": "you:import",
    "github": "you:import",
    "linkedin": "you:import",
    "settings": "settings",
    "notifications": "settings:notifications",
    "feedback": "settings:feedback",
    "preview": "settings:quiz",
    "quiz": "setup",
    "setup": "setup",
    "chat": "chat",
    "ask": "chat",
    "assistant": "chat",
}

_MULTIWORD_DEST = {
    "form details": "you:identity",
    "form detail": "you:identity",
    "my details": "you:identity",
    "looking for": "you:search",
    "what im looking for": "you:search",
    "what i'm looking for": "you:search",
    "add a fact": "you:add",
    "quiz preview": "settings:quiz",
    "profile quiz": "setup",
}

_DEST_NAMES = {
    "apply": "Apply",
    "apply:filed": "Filed on Apply",
    "you": "You",
    "you:identity": "form details on You",
    "you:search": "what you're looking for on You",
    "you:add": "add a fact on You",
    "you:projects": "projects on You",
    "you:experience": "experience on You",
    "you:import": "import on You",
    "settings": "Settings",
    "settings:notifications": "notifications in Settings",
    "settings:feedback": "feedback in Settings",
    "settings:quiz": "the quiz preview",
    "setup": "the profile quiz",
    "chat": "Ask",
}

_DEST_REPLIES = {
    "apply": (
        "Apply is your match queue. Open a job, tap Autofill, then Submit "
        "yourself."
    ),
    "apply:filed": (
        "Filed on Apply is every application you've marked done after Submit."
    ),
    "you": (
        "You is your profile — looking-for, form details, facts, and import."
    ),
    "you:identity": (
        "Form details live on You — name, email, phone, location, links, work "
        "auth. Or say “change my phone to …” here."
    ),
    "you:search": (
        "What you're looking for lives on You. Or tell me here: “looking for "
        "new-grad SWE, remote or NYC”."
    ),
    "you:add": (
        "Add a project, experience, or fact on You — or say “remember "
        "project: …” here."
    ),
    "you:projects": "Projects you've stored live on You.",
    "you:experience": "Experience you've stored lives on You.",
    "you:import": (
        "On You you can import a résumé, GitHub, or LinkedIn. I merge what I "
        "find into form details and facts."
    ),
    "settings": "Settings is account, notifications, feedback, and tester notes.",
    "settings:notifications": (
        "Notification permission lives in Settings. I'll ping for new matches "
        "and follow-ups once you turn it on."
    ),
    "settings:feedback": (
        "Feedback lives in Settings — a bug, a confusing screen, or a form "
        "that didn't fill."
    ),
    "settings:quiz": (
        "Quiz preview walks the profile quiz with sample answers. Nothing is saved."
    ),
    "setup": (
        "The profile quiz is how JobPilot learns roles and form details. You "
        "can retake it any time."
    ),
    "chat": (
        "You're on Ask — that's me. I can find jobs, edit details, and open "
        "any screen in the app."
    ),
}

_HELP_TOPICS = {
    "autofill": (
        "Autofill lives on Apply. Open a match, wait for the real form, then tap "
        "Fill. It fills identity and drafted answers on public application forms "
        "(Greenhouse, Lever, and Ashby are the smoothest). Login walls and "
        "CAPTCHAs pause for you — sign-in stays in the app. You always tap "
        "Submit yourself — I never submit."
    ),
    "submit": (
        "I never submit an application. After Autofill, attach your résumé yourself "
        "(the in-app browser can't set file inputs), review the form, then tap "
        "Submit on the site. Mark Filed on Apply when you're done."
    ),
    "resume": (
        "Résumé and cover letter attach is manual — WKWebView can't set file "
        "inputs. On Apply, open the documents menu: Resume is pre-downloaded, "
        "Cover letter is built when you tap it. Share into Files, then attach "
        "on the form yourself before you Submit."
    ),
    "identity": (
        "Form details (name, email, phone, location, links, work auth) live on You. "
        "Say “change my phone to …” or “I live in Chicago” and I'll update them. "
        "Ask “what's missing?” to see coverage."
    ),
    "queue": (
        "Apply is your queue. Say “show new jobs”, “queue top 3”, or “queue 2” to "
        "stage a match. Open Apply to Autofill and submit."
    ),
    "jobs": (
        "Tell me what you're after (“looking for new-grad SWE, remote or NYC”), "
        "then “show new jobs” or “review jobs”. I score and rank; you choose."
    ),
    "filed": (
        "After you Submit, mark Filed on Apply. Filed applications live on the "
        "Filed pane."
    ),
    "search": (
        "Roles and places I match on live under Looking for on You. Say "
        "“looking for …” here to change them."
    ),
    "add": (
        "Say “remember project: …” or add a fact on You. I use it when drafting "
        "answers."
    ),
    "import": (
        "Import a résumé, GitHub, or LinkedIn on You. I fill form details and "
        "facts from what I find."
    ),
    "notifications": (
        "Turn on notifications in Settings. I'll ping when new matches land and "
        "when a follow-up is due."
    ),
    "feedback": (
        "Send feedback from Settings — a bug, a confusing screen, or a form "
        "that didn't fill."
    ),
    "quiz": (
        "The profile quiz on You / first launch is how I learn roles and form "
        "details. Settings has a preview that saves nothing."
    ),
    "settings": (
        "Settings is the last tab — account, notifications, feedback, and tester notes."
    ),
    "overview": (
        "I'm Horizon — JobPilot from Ask. I find and rank jobs, queue a match, "
        "walk you through them, change form details, store facts, and Autofill "
        "on Apply. You always tap Submit.\n\n"
        "I can take you to Apply (matches or Filed), You (details, looking-for, "
        "add a fact, import), Settings (notifications, feedback), or the profile "
        "quiz. Say “show new jobs”, “what's missing?”, or “open form details”."
    ),
}

_TOPIC_HOPS = {
    "autofill": "apply",
    "submit": "apply",
    "resume": "apply",
    "identity": "you:identity",
    "queue": "apply",
    "jobs": "apply",
    "filed": "apply:filed",
    "search": "you:search",
    "add": "you:add",
    "import": "you:import",
    "notifications": "settings:notifications",
    "feedback": "settings:feedback",
    "quiz": "setup",
    "settings": "settings",
}

_NAVIGATE_PHRASES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(
        r"\b(?:take me to|open|go to|switch to|show me)\s+"
        r"(?:my\s+)?filed(?:\s+applications?)?\b", re.I,
    ), "apply:filed"),
    (re.compile(
        r"\b(?:take me to|open|go to|switch to|show me)\s+"
        r"(?:the\s+)?matches?\b", re.I,
    ), "apply"),
    (re.compile(
        r"\b(?:take me to|open|go to|switch to|show me|edit)\s+"
        r"(?:my\s+)?form details?\b", re.I,
    ), "you:identity"),
    (re.compile(
        r"\b(?:take me to|open|go to|switch to|show me|edit)\s+"
        r"(?:my\s+)?identity\b", re.I,
    ), "you:identity"),
    (re.compile(
        r"\b(?:take me to|open|go to|show me|edit|change)\s+"
        r"(?:what i'?m |my )?looking for\b", re.I,
    ), "you:search"),
    (re.compile(
        r"^\s*(?:add|open)\s+(?:a\s+)?(?:fact|project|experience)\s*[?.!]?\s*$",
        re.I,
    ), "you:add"),
    (re.compile(
        r"\b(?:take me to|open|go to|show me)\s+(?:my\s+)?projects\b", re.I,
    ), "you:projects"),
    (re.compile(
        r"\b(?:take me to|open|go to|show me)\s+(?:my\s+)?experience\b", re.I,
    ), "you:experience"),
    (re.compile(
        r"\b(?:take me to|open|go to|show me)\s+"
        r"(?:the\s+)?(?:import|r[eé]sum[eé] import|github|linkedin)\b", re.I,
    ), "you:import"),
    (re.compile(
        r"\b(?:take me to|open|go to|show me)\s+notifications\b"
        r"|\bturn on notifications\b", re.I,
    ), "settings:notifications"),
    (re.compile(
        r"\b(?:take me to|open|go to|show me)\s+feedback\b"
        r"|\bsend feedback\b", re.I,
    ), "settings:feedback"),
    (re.compile(
        r"\b(?:open|show|preview)\s+(?:the\s+)?quiz preview\b", re.I,
    ), "settings:quiz"),
    (re.compile(
        r"\b(?:retake|restart)\s+(?:the\s+)?(?:profile\s+)?quiz\b"
        r"|\b(?:open|go to|take me to)\s+(?:the\s+)?(?:profile\s+)?"
        r"(?:quiz|setup)\b", re.I,
    ), "setup"),
)

_NAVIGATE_SIMPLE = re.compile(
    r"\b(?:take me to|open|go to|switch to|show me)\s+(?:the\s+)?"
    r"(apply|queue|you|about|identity|profile|settings|chat|ask|assistant|"
    r"matches|filed|notifications|feedback|setup|import)"
    r"(?:\s+tab)?\b",
    re.I,
)

_OPEN_JOB_RE = re.compile(
    r"\b(?:open|take me to|go to)\s+(?:job\s+)?#(\d+)\b"
    r"|\b(?:open|take me to|go to)\s+job\s+(\d+)\b"
    r"|\b(?:open|take me to|go to)\s+(?:the\s+)?(.+?)\s+"
    r"(?:job|posting|form|one)\b",
    re.I,
)

_HOP_CHIPS = ["Take me there", "Not now"]

_DEFAULT_SUGGESTIONS = [
    "Show new jobs",
    "How do I autofill?",
    "What's missing?",
]

_OVERVIEW_SUGGESTIONS = [
    "Show new jobs",
    "What's missing?",
    "Open form details",
]


def canonical_identity_field(name: str | None) -> str | None:
    """Map a user-facing field name onto an applicant identity key."""
    if not name:
        return None
    raw = name.strip().lower().replace("-", " ")
    raw = " ".join(raw.split())
    if not raw:
        return None
    from . import applicant

    if raw in applicant.FIELDS:
        return raw
    for alias, key in _ALIAS_BY_LEN:
        if raw == alias or raw.replace(" ", "_") == key:
            return key
    # "my phone number" etc.
    for alias, key in _ALIAS_BY_LEN:
        if alias in raw:
            return key
    return None


def identity_alias_pattern() -> str:
    """Alternation of aliases for heuristic regex (longest first)."""
    import re

    return "|".join(re.escape(a) for a, _ in _ALIAS_BY_LEN)


def navigate_dest(text: str | None) -> str | None:
    """Return a canonical deep link if ``text`` names a destination."""
    if not text:
        return None
    low = text.strip().lower()
    if low.startswith("tab:"):
        low = low[4:].strip()
    if low.startswith("dest:"):
        low = low[5:].strip()
    if low.startswith("job:") and low[4:].strip():
        return f"job:{low[4:].strip()}"
    compact = " ".join(low.replace("\u2019", "'").split())
    if compact in _MULTIWORD_DEST:
        return _MULTIWORD_DEST[compact]
    return _DEST_ALIASES.get(low)


def navigate_tab(text: str | None) -> str | None:
    """Alias of ``navigate_dest`` — hops can be screens, not only tabs."""
    return navigate_dest(text)


def parse_navigate(low: str) -> str | None:
    """Match a go-there utterance. None if this isn't navigation."""
    for pat, dest in _NAVIGATE_PHRASES:
        if pat.search(low):
            return dest
    m = _OPEN_JOB_RE.search(low)
    if m:
        pid = m.group(1) or m.group(2)
        if pid:
            return f"job:{pid}"
        name = (m.group(3) or "").strip().lower()
        name = re.sub(r"^(the|my)\s+", "", name)
        if not name or name in {
            "the", "a", "an", "my", "this", "that", "apply", "autofill",
            "queue", "you", "profile", "form", "details",
        }:
            if name in ("apply", "autofill", "queue"):
                return "apply"
            if name in ("you", "profile"):
                return "you"
            # fall through to the simple tab matcher
        elif name in ("identity", "details", "form details"):
            return "you:identity"
        else:
            return f"job:{name}"
    m = _NAVIGATE_SIMPLE.search(low)
    if m:
        return navigate_dest(m.group(1))
    return None


def help_topic_key(text: str | None) -> str:
    low = (text or "").strip().lower()
    dest = navigate_dest(low)
    if dest:
        if dest.startswith("job:") or dest == "apply":
            return "queue"
        mapping = {
            "apply:filed": "filed",
            "you": "identity",
            "you:identity": "identity",
            "you:search": "search",
            "you:add": "add",
            "you:projects": "add",
            "you:experience": "add",
            "you:import": "import",
            "settings": "settings",
            "settings:notifications": "notifications",
            "settings:feedback": "feedback",
            "settings:quiz": "quiz",
            "setup": "quiz",
            "chat": "overview",
        }
        return mapping.get(dest, "overview")
    if "autofill" in low or "auto fill" in low or "auto-fill" in low:
        return "autofill"
    if "submit" in low:
        return "submit"
    if "cover letter" in low or "cover-letter" in low or "coverletter" in low:
        return "resume"
    if "resume" in low or "résumé" in low or "attach" in low:
        return "resume"
    if "import" in low or "github" in low:
        return "import"
    if "notification" in low:
        return "notifications"
    if "feedback" in low:
        return "feedback"
    if "quiz" in low or "setup" in low:
        return "quiz"
    if "filed" in low:
        return "filed"
    if "looking for" in low:
        return "search"
    if any(w in low for w in ("identity", "form detail", "my details", "phone", "email")):
        return "identity"
    if any(w in low for w in ("queue", "apply tab", "stage")):
        return "queue"
    if any(w in low for w in ("find job", "new job", "discover", "match")):
        return "jobs"
    if "add a fact" in low or "add a project" in low:
        return "add"
    if "settings" in low:
        return "settings"
    return "overview"


def help_hop_dest(topic: str | None) -> str | None:
    """Screen to offer after a how-to, if the topic isn't already a dest."""
    dest = navigate_dest(topic)
    if dest:
        return dest
    return _TOPIC_HOPS.get(help_topic_key(topic))


def help_reply(topic: str | None) -> str:
    dest = navigate_dest(topic)
    if dest and dest.startswith("job:"):
        return _DEST_REPLIES["apply"]
    if dest and dest in _DEST_REPLIES:
        return _DEST_REPLIES[dest]
    return _HELP_TOPICS[help_topic_key(topic)]


def dest_label(dest: str | None) -> str:
    if not dest:
        return "that screen"
    if dest.startswith("job:"):
        return "that form on Apply"
    return _DEST_NAMES.get(dest, dest)


def tab_label(tab: str | None) -> str:
    return dest_label(tab)


def offer_tab_hop(user_id: str, tab: str | None, lead: str) -> str:
    """Explain a destination and ask before moving. Never switches screens itself."""
    from . import conversation as convo
    from .conversation import Pending
    from .intents import Intent

    if not tab or tab == "chat":
        return lead
    convo.set_pending(
        user_id, Pending(Intent.HELP_APP.value, {"tab": tab}, "go"),
    )
    name = dest_label(tab)
    body = (lead or "").rstrip()
    if "Want me to take you" in body:
        return body
    return f"{body} Want me to take you to {name}?"


def _str(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value).strip()
    return text or None


def parsed_from_action(action: str, slots: dict | None, raw_text: str) -> ParsedMessage:
    """Turn a device-classified action + slots into an engine ParsedMessage."""
    slots = dict(slots or {})
    try:
        intent = Intent((action or "UNKNOWN").strip().upper())
    except ValueError:
        intent = Intent.UNKNOWN

    try:
        confidence = float(slots.get("confidence") if slots.get("confidence") is not None else 0.9)
    except (TypeError, ValueError):
        confidence = 0.9
    confidence = max(0.0, min(1.0, confidence))

    company = _str(slots.get("company"))
    role = _str(slots.get("role"))
    status = normalize_status(_str(slots.get("status"))) if slots.get("status") else None
    message = _str(slots.get("message"))
    time_reference = _str(slots.get("time_reference") or slots.get("timeReference"))

    if intent == Intent.SET_IDENTITY:
        field = canonical_identity_field(
            _str(slots.get("field") or slots.get("key") or role)
        )
        value = _str(slots["value"] if "value" in slots else message)
        role = field
        message = value
        if not field:
            confidence = min(confidence, 0.45)

    elif intent == Intent.HELP_APP:
        raw_dest = _str(slots.get("tab") or slots.get("dest") or slots.get("topic"))
        tab = navigate_dest(raw_dest) or parse_navigate(raw_dest or "")
        if tab:
            message = f"tab:{tab}"
        else:
            message = _str(slots.get("topic") or slots.get("tab") or message) or raw_text

    elif intent in (
        Intent.QUEUE_JOB, Intent.APPLY_JOB, Intent.DISMISS_JOB, Intent.SNOOZE_JOB,
    ):
        if slots.get("job_id") is not None or slots.get("jobId") is not None:
            jid = slots.get("job_id", slots.get("jobId"))
            message = str(jid).strip()
        elif slots.get("spec"):
            message = str(slots["spec"]).strip()
        elif slots.get("count") is not None:
            try:
                message = f"top:{int(slots['count'])}"
            except (TypeError, ValueError):
                pass

    elif intent == Intent.TUNE:
        message = message or _str(slots.get("tune"))

    elif intent == Intent.REMEMBER:
        cat = _str(slots.get("category"))
        text = _str(slots.get("text") or message)
        if cat and text:
            message = f"{cat}|{text}"
        elif text:
            message = text

    elif intent == Intent.PROFILE:
        message = message or _str(slots.get("criteria") or raw_text)

    elif intent == Intent.UNKNOWN:
        confidence = min(confidence, 0.2)

    return ParsedMessage(
        intent=intent,
        company=company,
        role=role,
        status=status,
        message=message,
        time_reference=time_reference,
        confidence=confidence,
    )


def suggestions_for(
    parsed: ParsedMessage | None, *, confirm: bool, pending=None,
) -> list[str]:
    if confirm:
        return ["Yes", "Cancel"]
    if pending is not None and pending.active and pending.awaiting == "go":
        return list(_HOP_CHIPS)
    if pending is not None and pending.active and pending.intent == Intent.JOBS_REVIEW.value:
        return ["Skip", "Queue this", "Stop"]
    intent = parsed.intent if parsed else Intent.UNKNOWN
    topic = (parsed.message or "").lower() if parsed else ""
    if intent == Intent.HELP_APP:
        if topic.startswith("tab:"):
            return list(_HOP_CHIPS)
        hop = help_hop_dest(topic)
        if hop and hop != "chat":
            return list(_HOP_CHIPS)
        if topic in ("overview", "help", ""):
            return list(_OVERVIEW_SUGGESTIONS)
        if "autofill" in topic or "submit" in topic or "resume" in topic:
            return ["Open Apply", "What's missing?", "Show new jobs"]
        if "identity" in topic or "you" in topic:
            return ["Open You", "What's missing?"]
        return _DEFAULT_SUGGESTIONS
    if intent == Intent.SET_IDENTITY:
        return ["What's missing?", "Open You", "Show new jobs"]
    if intent == Intent.JOBS:
        return ["Walk me through them", "Open Apply", "Be less picky"]
    if intent == Intent.JOBS_REVIEW:
        return ["Skip", "Queue this", "Stop"]
    if intent == Intent.QUEUE_JOB:
        return ["Open Apply", "Show new jobs"]
    if intent == Intent.PROFILE:
        return ["Show new jobs", "Track a company"]
    if intent == Intent.KNOWLEDGE:
        return list(_HOP_CHIPS)
    if intent == Intent.STATS:
        return ["What should I follow up on", "What's coming up"]
    return list(_DEFAULT_SUGGESTIONS)


def decorate(user_id: str, reply: str, parsed: ParsedMessage | None) -> dict:
    """Chips / deep link / confirm flag for the Assistant UI.

    ``deep_link`` is only set after the user accepts a hop. Offers never move
    the tab on their own.
    """
    from . import conversation as convo

    pending = convo.get_pending(user_id)
    deep_link = None
    if pending.active and pending.awaiting == "go":
        tab = pending.slots.get("tab")
        if pending.slots.get("_confirmed_go"):
            convo.clear_pending(user_id)
            deep_link = tab if tab and tab != "chat" else None
            suggestions = list(_DEFAULT_SUGGESTIONS)
        elif pending.slots.get("_declined_go"):
            convo.clear_pending(user_id)
            suggestions = list(_DEFAULT_SUGGESTIONS)
        else:
            suggestions = list(_HOP_CHIPS)
        confirm = False
    else:
        confirm = bool(pending.active and pending.awaiting == "confirm")
        suggestions = suggestions_for(parsed, confirm=confirm, pending=pending)
    return {
        "suggestions": suggestions,
        "deep_link": deep_link if not confirm else None,
        "confirm": {"pending": True} if confirm else None,
        "intent": parsed.intent.value if parsed else None,
    }
