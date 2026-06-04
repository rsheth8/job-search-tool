"""Apollo.io recruiter discovery — the only file that talks to Apollo's API.

Mirrors the isolation pattern used elsewhere (``scheduler.py`` owns apscheduler,
``TwilioSender`` owns the Twilio SDK): nothing else imports httpx-for-Apollo, so
the rest of the app stays offline-testable and the network surface is one file.

Contract, same as the router's: **never block the user**. ``discover_recruiters``
returns a list of plain dicts on success and an empty list on *anything* else —
no API key, network error, rate limit, bad payload. The caller treats "no key"
and "found nobody" the same way (gracefully), so this layer never raises.

Credit safety:
  * ``mixed_people/api_search`` (people discovery) does **not** consume credits.
  * ``mixed_companies/search`` (org → domain lookup) **does** consume credits —
    disabled by default via ``APOLLO_ORG_LOOKUP_ENABLED=false``. Without it we
    use ``q_keywords`` for company names (still free).
  * Daily discovery cap + per-minute rate limit prevent accidental API spam.
  * ``discover_for_company`` caches by company so repeat OUTREACH is DB-only.

Use ``discovery_issue()`` after an empty result when Apollo is configured to
distinguish plan/auth errors from a genuine empty search.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import get_settings
from .db import connect
from .ratelimit import TokenBucket

logger = logging.getLogger("apollo")

# People at a company worth reaching out to about a job. Apollo matches these as
# substrings against job titles, so the list is deliberately broad.
DEFAULT_TITLES = [
    "recruiter",
    "technical recruiter",
    "talent acquisition",
    "talent partner",
    "sourcer",
    "head of talent",
    "engineering manager",
]

# Documented People API Search endpoint (replaces legacy mixed_people/search).
_PEOPLE_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
_ORG_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_companies/search"
_TIMEOUT_SECONDS = 10.0

_last_discovery_issue: str | None = None
_limiter: TokenBucket | None = None
_usage = {
    "people_searches": 0,
    "org_searches": 0,
    "skipped_rate_limit": 0,
    "skipped_daily_cap": 0,
    "skipped_org_cap": 0,
}


@dataclass
class DiscoveryMeta:
    """What Apollo did on the last ``discover_recruiters`` call."""

    people_search: bool = False
    org_credits: int = 0
    domain_from_cache: bool = False


_discovery_meta = DiscoveryMeta()


def discovery_issue() -> str | None:
    """Human-readable reason the last discovery returned no people, if known."""
    return _last_discovery_issue


def usage() -> dict:
    """Counters for /health — includes today's discovery count vs daily cap."""
    s = get_settings()
    today = _discoveries_today()
    return {
        **_usage,
        "discoveries_today": today,
        "discoveries_daily_cap": s.apollo_max_discoveries_per_day,
        "org_searches_today": _org_searches_today(),
        "org_searches_daily_cap": s.apollo_max_org_searches_per_day,
        "org_lookup_enabled": s.apollo_org_lookup_enabled,
    }


def discovery_meta() -> DiscoveryMeta:
    return DiscoveryMeta(
        people_search=_discovery_meta.people_search,
        org_credits=_discovery_meta.org_credits,
        domain_from_cache=_discovery_meta.domain_from_cache,
    )


def reset_for_tests() -> None:
    """Clear cached client, limiter, and in-memory counters (tests only)."""
    global _client_singleton, _last_discovery_issue, _limiter, _discovery_meta
    _client_singleton = None
    _last_discovery_issue = None
    _limiter = None
    _discovery_meta = DiscoveryMeta()
    for k in _usage:
        _usage[k] = 0


def _clear_discovery_issue() -> None:
    global _last_discovery_issue
    _last_discovery_issue = None


def _set_discovery_issue(message: str) -> None:
    global _last_discovery_issue
    _last_discovery_issue = message


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_start_iso() -> str:
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat()


def _discoveries_today() -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM apollo_api_calls "
            "WHERE call_type = 'people_search' AND called_at >= ?",
            (_today_start_iso(),),
        ).fetchone()
        return int(row["n"]) if row else 0


def _org_searches_today() -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM apollo_api_calls "
            "WHERE call_type = 'org_search' AND called_at >= ?",
            (_today_start_iso(),),
        ).fetchone()
        return int(row["n"]) if row else 0


def _company_key(name: str) -> str:
    return (name or "").strip().lower()


def _clean_domain(raw: str | None) -> str | None:
    if not raw:
        return None
    domain = str(raw).strip().lower()
    domain = domain.removeprefix("https://").removeprefix("http://")
    domain = domain.removeprefix("www.").split("/")[0]
    return domain or None


def _get_cached_domain(company: str) -> str | None:
    key = _company_key(company)
    if not key:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT domain FROM company_domains WHERE company_key = ?",
            (key,),
        ).fetchone()
        return row["domain"] if row else None


def _org_miss_is_fresh(company: str) -> bool:
    """True when a recent org lookup found no domain — skip spending credits again."""
    key = _company_key(company)
    if not key:
        return False
    days = get_settings().apollo_org_miss_cache_days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM company_domain_misses WHERE company_key = ? AND tried_at >= ?",
            (key, cutoff),
        ).fetchone()
        return row is not None


def _record_org_miss(company: str) -> None:
    key = _company_key(company)
    if not key:
        return
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO company_domain_misses (company_key, tried_at) "
            "VALUES (?, ?)",
            (key, _utc_now_iso()),
        )


def _save_company_domain(
    company: str, domain: str, *, apollo_org_id: str | None = None
) -> None:
    key = _company_key(company)
    if not key or not domain:
        return
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO company_domains "
            "(company_key, domain, apollo_org_id, resolved_at) VALUES (?, ?, ?, ?)",
            (key, domain, apollo_org_id, _utc_now_iso()),
        )
        conn.execute(
            "DELETE FROM company_domain_misses WHERE company_key = ?", (key,)
        )


def _cache_domain_from_org_blob(company: str, org: dict) -> None:
    """Persist domain from any Apollo org-shaped blob (free when piggybacking people search)."""
    if not org:
        return
    domain = _clean_domain(
        org.get("primary_domain") or org.get("website_url") or org.get("domain")
    )
    if domain:
        _save_company_domain(
            company, domain, apollo_org_id=(org.get("id") or org.get("organization_id"))
        )


def _record_call(call_type: str, company: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO apollo_api_calls (call_type, company, called_at) VALUES (?, ?, ?)",
            (call_type, company, _utc_now_iso()),
        )
    if call_type == "people_search":
        _usage["people_searches"] += 1
    elif call_type == "org_search":
        _usage["org_searches"] += 1


def _get_limiter() -> TokenBucket:
    global _limiter
    if _limiter is None:
        _limiter = TokenBucket(get_settings().apollo_rate_limit_per_min)
    return _limiter


def _check_limits() -> str | None:
    """Return a user-facing message if this discovery should not call Apollo."""
    s = get_settings()
    today = _discoveries_today()
    if today >= s.apollo_max_discoveries_per_day:
        _usage["skipped_daily_cap"] += 1
        return (
            f"Apollo discovery limit for today ({s.apollo_max_discoveries_per_day}/day). "
            "Cached contacts still work — try again tomorrow or raise "
            "APOLLO_MAX_DISCOVERIES_PER_DAY in .env."
        )
    if not _get_limiter().allow():
        _usage["skipped_rate_limit"] += 1
        return (
            "Apollo rate limit — too many discovery requests in a short window. "
            "Wait a minute and try again."
        )
    return None


def _issue_from_response(status_code: int, body: dict) -> str | None:
    err = (body.get("error") or "").strip()
    code = body.get("error_code") or ""
    if status_code == 403 and (
        "not accessible" in err.lower()
        or code == "API_INACCESSIBLE"
    ):
        if "free plan" in err.lower():
            return (
                "Apollo's People Search API still reports this account as free-plan "
                "(common on a Basic *trial* — the UI may show People access before the "
                "API unlocks). Try: add a payment method and start paid Basic, or wait "
                "for trial API access to sync; then create a new master key in "
                "Settings → Integrations → Apollo API and update APOLLO_API_KEY."
            )
        return (
            "Apollo people search isn't available for this API key (paid plan + "
            "master API key with People Search access required). Check "
            "https://app.apollo.io/ → API keys, update APOLLO_API_KEY, restart."
        )
    if status_code in (401, 403):
        return "Apollo rejected the API key. Check APOLLO_API_KEY in .env."
    return None


def _issue_from_exception(exc: Exception) -> str | None:
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        try:
            body = exc.response.json()
            if isinstance(body, dict):
                return _issue_from_response(exc.response.status_code, body)
        except Exception:
            pass
    return None


class ApolloClient:
    """Thin wrapper over Apollo's people-search endpoint.

    Constructed only when an API key is present. The httpx client is built once
    and reused (cached by ``get_apollo``).
    """

    def __init__(self, api_key: str) -> None:
        import httpx  # lazy: offline/test paths never import it

        self._key = api_key
        self._http = httpx.Client(timeout=_TIMEOUT_SECONDS)

    def find_people(self, company: str, *, titles: list[str], limit: int) -> list[dict]:
        """Raw people search. Raises on transport/HTTP error (caller catches)."""
        domain = self._resolve_domain(company)
        payload: dict = {
            "person_titles": titles,
            "page": 1,
            "per_page": limit,
        }
        if domain:
            payload["q_organization_domains_list"] = [domain]
        else:
            # Free fallback when org lookup is off or fails — no credits consumed.
            payload["q_keywords"] = company

        resp = self._http.post(
            _PEOPLE_SEARCH_URL,
            headers={
                "Content-Type": "application/json",
                "Cache-Control": "no-cache",
                "X-Api-Key": self._key,
            },
            json=payload,
        )
        if resp.status_code >= 400:
            try:
                body = resp.json()
                if isinstance(body, dict):
                    issue = _issue_from_response(resp.status_code, body)
                    if issue:
                        _set_discovery_issue(issue)
            except Exception:
                pass
        resp.raise_for_status()
        people = resp.json().get("people") or []
        normalized = [_normalize_person(p, company) for p in people[:limit]]
        for p in people[:limit]:
            _cache_domain_from_org_blob(company, p.get("organization") or {})
        return normalized

    def _resolve_domain(self, company: str) -> str | None:
        """Map a company name to an employer domain when possible.

        Org search consumes Apollo credits — only runs when
        ``apollo_org_lookup_enabled`` is true. Results are cached in SQLite.
        """
        raw = (company or "").strip()
        if not raw:
            return None
        # Already looks like a domain (stripe.com, www.stripe.com).
        if re.match(r"^[\w.-]+\.[a-z]{2,}$", raw, re.I):
            return raw.lower().removeprefix("www.")

        s = get_settings()
        if not s.apollo_org_lookup_enabled:
            return None

        key = _company_key(raw)
        cached = _get_cached_domain(raw)
        if cached:
            global _discovery_meta
            _discovery_meta.domain_from_cache = True
            return cached
        if _org_miss_is_fresh(raw):
            logger.debug("Skipping org search for %r — recent miss cached", raw)
            return None
        if _org_searches_today() >= s.apollo_max_org_searches_per_day:
            _usage["skipped_org_cap"] += 1
            logger.info(
                "Apollo org search cap reached (%s/day); using keyword fallback for %r",
                s.apollo_max_org_searches_per_day,
                raw,
            )
            return None

        try:
            resp = self._http.post(
                _ORG_SEARCH_URL,
                headers={
                    "Content-Type": "application/json",
                    "Cache-Control": "no-cache",
                    "X-Api-Key": self._key,
                },
                json={"q_organization_name": raw, "page": 1, "per_page": 1},
            )
            if resp.status_code >= 400:
                _record_org_miss(raw)
                return None
            _record_call("org_search", raw)
            _discovery_meta.org_credits += 1
            data = resp.json()
            orgs = data.get("organizations") or data.get("accounts") or []
            if not orgs:
                _record_org_miss(raw)
                return None
            org = orgs[0]
            domain = _clean_domain(
                org.get("primary_domain")
                or org.get("website_url")
                or org.get("domain")
            )
            if not domain:
                _record_org_miss(raw)
                return None
            _save_company_domain(
                raw,
                domain,
                apollo_org_id=str(org.get("id") or org.get("organization_id") or ""),
            )
            return domain
        except Exception:
            logger.debug("Apollo org lookup failed for %r", company, exc_info=True)
            return None


def _display_name(p: dict) -> str:
    """Build a display name from Apollo's api_search shape (often no ``name`` field)."""
    name = (p.get("name") or "").strip()
    if name:
        return name
    first = (p.get("first_name") or "").strip()
    last = (p.get("last_name") or p.get("last_name_obfuscated") or "").strip()
    if first and last:
        return f"{first} {last}"
    if first:
        return first
    return "Unknown"


def _normalize_person(p: dict, company: str) -> dict:
    """Reduce an Apollo person record to the fields we store.

    Apollo returns an unlocked-email placeholder (e.g.
    ``email_not_unlocked@domain.com``) when the address isn't revealed on the
    current plan; treat that as no email rather than storing junk.

    ``api_search`` (Basic+) often omits ``name``/``linkedin_url``/``email`` and
    returns ``first_name`` + ``last_name_obfuscated`` instead — enrichment is a
    separate paid call we don't make here.
    """
    email = p.get("email")
    if email and "not_unlocked" in email:
        email = None
    org = p.get("organization") or {}
    apollo_id = p.get("id")
    return {
        "name": _display_name(p),
        "title": (p.get("title") or "").strip() or None,
        "email": email,
        "linkedin_url": p.get("linkedin_url"),
        "company": org.get("name") or company,
        "apollo_person_id": str(apollo_id) if apollo_id else None,
    }


_client_singleton: ApolloClient | None = None


def get_apollo() -> ApolloClient | None:
    """Return a cached client, or None when Apollo isn't configured/installed."""
    global _client_singleton
    if _client_singleton is not None:
        return _client_singleton
    s = get_settings()
    if not s.apollo_enabled:
        return None
    try:
        _client_singleton = ApolloClient(s.apollo_api_key)
    except Exception:  # httpx missing / bad construction — stay offline
        logger.exception("Apollo client unavailable; recruiter discovery disabled")
        return None
    return _client_singleton


def discover_recruiters(
    company: str, *, titles: list[str] | None = None, limit: int | None = None
) -> list[dict]:
    """Find recruiters at ``company``. Always returns a list (empty on any issue).

    This is the safe entry point the rest of the app calls — it never raises and
    never blocks, matching the router's graceful-fallback contract.
    """
    _clear_discovery_issue()
    global _discovery_meta
    _discovery_meta = DiscoveryMeta()
    client = get_apollo()
    if client is None:
        return []

    s = get_settings()
    cap = s.apollo_max_results
    effective_limit = min(limit if limit is not None else cap, cap)

    blocked = _check_limits()
    if blocked:
        _set_discovery_issue(blocked)
        return []

    try:
        result = client.find_people(
            company, titles=titles or DEFAULT_TITLES, limit=effective_limit
        )
        _record_call("people_search", company)
        _discovery_meta.people_search = True
        return result
    except Exception as exc:  # network, auth, rate limit, bad payload — never block
        parsed = _issue_from_exception(exc)
        if parsed:
            _set_discovery_issue(parsed)
        logger.exception("Apollo search failed for %r; returning no recruiters", company)
        return []
