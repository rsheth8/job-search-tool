"""Environment-driven configuration.

Everything has a sane default so the system runs with zero setup.
Real backends activate only when their keys are present.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_path: str = "job_search.db"

    # Claude (Anthropic) intent router. Haiku 4.5 is the cheapest capable model
    # and is well-suited to this classification/extraction task.
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5"

    # Token-saving guards for the LLM router.
    llm_rate_limit_per_min: int = 30   # token-bucket cap on paid API calls
    llm_max_sms_chars: int = 480       # truncate inbound SMS before sending
    # Per-user daily cap on paid Anthropic calls (chat + discovery + drafts).
    # 0 = unlimited. Testers share one key; this keeps one chatty account from
    # starving everyone else's scoring budget.
    llm_max_calls_per_user_per_day: int = 80

    twilio_auth_token: str = ""
    twilio_validate_signature: bool = False
    # Outbound SMS (reminders). All three are required to actually send; until
    # they're set, reminders fall back to LogSender. Blocked on A2P 10DLC.
    twilio_account_sid: str = ""
    twilio_from_number: str = ""

    # Slack transport (now the primary channel — no A2P approval, outbound works
    # immediately). Bot token (xoxb-) enables both inbound replies and outbound
    # reminders; signing secret validates inbound Events API requests.
    slack_bot_token: str = ""
    slack_signing_secret: str = ""

    apollo_api_key: str = ""

    # Apollo credit / abuse guards. people api_search is free; org search costs credits.
    apollo_max_results: int = 3              # contacts per discovery (cap per search)
    apollo_max_discoveries_per_day: int = 5    # people searches / day (new companies)
    apollo_rate_limit_per_min: int = 3         # token-bucket; over-limit skips gracefully
    apollo_org_lookup_enabled: bool = False    # org search burns credits — off by default
    apollo_max_org_searches_per_day: int = 3   # credit-consuming org lookups / UTC day
    apollo_org_miss_cache_days: int = 30       # don't re-spend credits on unknown companies

    default_followup_days: int = 7

    # --- Job discovery (Phase 1) -------------------------------------------
    # Poll cadence for the background discovery loop (free ATS feeds tolerate
    # frequent polls). Each new posting is deduped, so we never re-alert.
    job_poll_seconds: int = 600
    # A posting must score >= this (0..1) to be alerted. Tune from Slack later.
    job_relevance_threshold: float = 0.6
    # Auto-stage very-high-confidence matches straight into the apply queue (skip
    # triage) — they're prepared and waiting at /apply. 0 = off; set e.g. 0.85 to
    # only auto-queue your strongest matches.
    job_auto_queue_threshold: float = 0.0
    # Cap how many *new* postings get LLM-scored per tick (token-cost guard).
    # Survivors of the free pre-filter beyond this carry over to the next tick.
    job_max_scored_per_tick: int = 60
    # How to notify on new matches: digest (one summary per tick), instant
    # (one message per job, legacy), or silent (store only).
    job_alert_mode: str = "digest"
    # How many top matches to show in a digest body (rest summarized as "+N more").
    job_digest_top_n: int = 5
    # The user the background loop alerts (Slack user id). Empty = alert the
    # busiest known user, mirroring dashboard.default_user().
    job_alert_user: str = ""
    # Free sources are on by default. Paid sources (added later) stay off until
    # explicitly enabled with their own budget caps, mirroring the Apollo guards.
    job_sources_enabled: str = "greenhouse,lever,ashby,rss,directory,swelist"

    # --- Wide discovery (A/B/C) — profile-driven, no company list required ---
    job_wide_rss_enabled: bool = True
    job_wide_rss_feeds: str = "hn-hiring,remoteok,weworkremotely"
    job_wide_directory_enabled: bool = True
    job_directory_boards_per_tick: int = 24
    job_directory_max_jobs_per_board: int = 25
    job_directory_data_path: str = "data/ats_boards.json"
    job_wide_aggregator_enabled: bool = False
    serpapi_api_key: str = ""
    job_aggregator_max_per_day: int = 5
    # Pitt CSC / Simplify internship list (listings.json, not the README buttons).
    job_wide_swelist_enabled: bool = True
    job_swelist_list: str = "summer2027"
    job_swelist_max_age_days: int = 21

    # Ghost-job filter (Matching v2, Phase 3): drop never-really-hiring reqs
    # (evergreen language, reposts, stale, scam contact) beyond quality.py's spam
    # gate. Conservative + free (rules only). On by default; flip off to disable.
    ghost_filter_enabled: bool = True

    # --- Resume tailoring (assisted apply) ---------------------------------
    # Base .tex files live on the Fly volume, not in git (personal info).
    resume_tex_dir: str = "/data/resumes"
    resume_tailor_enabled: bool = True
    tectonic_bin: str = "tectonic"

    # --- Embedding matching (Matching v2, Phase 1) -------------------------
    # Off by default. When enabled + keyed, matcher.score ranks postings by
    # cosine similarity (profile vs JD) instead of the keyword heuristic. Gated
    # + budget-capped like the paid aggregator; falls back to heuristic on any
    # failure, so the free path is never broken.
    embedding_enabled: bool = False
    voyage_api_key: str = ""
    embedding_model: str = "voyage-3-lite"
    embedding_max_calls_per_day: int = 200   # billable embedding requests / UTC day
    # Token-bucket on Voyage calls. Default matches Voyage's FREE tier (~3 req/min)
    # so the live app doesn't 429; raise it if you're on a paid tier.
    embedding_rate_limit_per_min: int = 3

    # --- Personalized re-ranker (Matching v2, Phase 2) --------------------
    # Off by default. When on, a small logistic-regression model trained on the
    # user's own apply/dismiss/snooze history re-scores postings — personalizing
    # the ranking. Cold-starts gracefully: below the label minimums it's a no-op
    # and the matcher's score stands. Pure-Python, no extra deps.
    reranker_enabled: bool = False
    reranker_min_positive: int = 5   # min 'applied' labels before the model engages
    reranker_min_negative: int = 5   # min 'dismissed'/'snoozed' labels before engaging
    # Grade applied labels by their real outcome stage (the CRM funnel): an
    # application that reached a phone screen / onsite / offer is a STRONGER
    # positive than one that's only 'Applied', and a rejected/ghosted one is a
    # weaker positive. Learns what leads to traction, not just what you clicked.
    reranker_outcome_weighting: bool = True

    # --- Eligibility / qualification gate (Matching v2) -------------------
    # Drop roles the candidate clearly isn't qualified for / couldn't realistically
    # do, given their level. Rule tier (free, on) catches seniority gaps, big
    # experience requirements, and hard credentials. LLM tier (off; batched Haiku)
    # adds nuanced judgement. Candidate level comes from the profile's seniority,
    # falling back to ``eligibility_candidate_level``.
    eligibility_filter_enabled: bool = True
    eligibility_candidate_level: str = "entry"   # fallback when profile has no seniority
    # Drop clearly non-technical roles (sales/recruiting/marketing/admin/etc.) for
    # a technical candidate — unless the title has a technical signal. On by default.
    eligibility_field_filter: bool = True
    eligibility_llm_enabled: bool = False
    eligibility_max_calls_per_day: int = 100     # batched Haiku eligibility checks / day

    # --- Deck TL;DR insights (swipe trainer) -----------------------------
    # Plain-language "what is this role + is it for me" on each swipe card.
    # Off by default; reuses ANTHROPIC_API_KEY. Cheap by design: ONE batched
    # Haiku call per deck, results cached per posting (summarized once ever),
    # daily-capped. Each call covers a whole deck (~15 roles), so the cap is in
    # decks/day, not roles/day.
    deck_tldr_enabled: bool = False
    deck_tldr_max_calls_per_day: int = 50

    # --- Application autofill (browser extension, Track C) ----------------
    # The autofill extension calls /apply/* from ATS origins (greenhouse.io, …).
    # When set, the cross-origin autofill endpoints require this token in an
    # X-Apply-Token header; left blank (default) they're open (fine for local/dev,
    # a personal single-user tool). Always set it before exposing publicly.
    apply_api_token: str = ""
    # Comma-separated allowed CORS origins for the autofill endpoints; "*" allows
    # any (token still gates writes). Prod sets APPLY_CORS_ORIGIN_REGEX instead.
    apply_cors_origins: str = "*"
    # Optional regex (Starlette allow_origin_regex). Used when origins isn't "*".
    apply_cors_origin_regex: str = ""
    # When false, POST /apply/autosubmit is 403 — testers use in-app Autofill.
    apply_autosubmit_enabled: bool = False
    # Local/tests: personal APIs accept ?user= without a session when the apply
    # token is also blank. Production MUST set this false (see fly.toml).
    auth_fail_open: bool = True
    # Invite-only Sign in with Apple. Empty = anyone with a valid Apple token.
    # Comma-separated emails (case-insensitive). Private Relay addresses work.
    auth_allowed_emails: str = ""
    # Optional: ping this user_id (chat/Slack) when a tester submits feedback.
    feedback_notify_user: str = ""
    # Sentry DSN. Empty = no-op (tests, local). Set as a Fly secret in prod.
    sentry_dsn: str = ""

    # Push notifications to the iPhone app (APNs). Off until PUSH_ENABLED plus all
    # four APNs values are set; app/push.py is a no-op meanwhile, so this ships
    # dark and switches on with config alone. Needs an Apple Developer account:
    # APNS_KEY_ID/TEAM_ID come from the .p8 auth key, BUNDLE_ID is the app's id,
    # and APNS_KEY_PATH points at the .p8 on the volume (e.g. /data/apns.p8).
    # Sandbox is the right host for a development build; TestFlight/App Store use
    # production — mismatching them yields BadDeviceToken.
    push_enabled: bool = False
    apns_key_id: str = ""
    apns_team_id: str = ""
    apns_bundle_id: str = ""
    apns_key_path: str = ""
    apns_use_sandbox: bool = False

    # --- Sign in with Apple + in-app chat ---------------------------------
    # Comma-separated audiences accepted on Apple identity tokens. Include the
    # iOS bundle id (com.rahil.apply) and, for web, the Services ID.
    apple_client_ids: str = "com.rahil.apply"
    # Optional Services ID used by the minimal web chat page (Sign in with Apple JS).
    apple_services_id: str = ""
    # Session lifetime for Bearer tokens minted at login.
    auth_session_days: int = 90
    # When true, POST /auth/dev mints a session without Apple (tests + local).
    auth_allow_dev_login: bool = False
    # Stable user id for POST /auth/dev when the client omits user_id. Keeps the
    # simulator on one account (queue, identity, knowledge) across reinstalls /
    # Dev sign-ins. Empty = mint a fresh usr_… each time (tests often want that).
    auth_dev_user_id: str = ""
    # On first Apple sign-in, fold this legacy Slack/phone user_id into the new
    # account (one-shot via usermerge). Empty = no migration.
    auth_legacy_user_id: str = ""
    # Slack transport is retired once in-app chat ships. Keep false unless you
    # explicitly need the old Events webhook during a transition.
    slack_transport_enabled: bool = False

    @property
    def allowed_emails(self) -> set[str]:
        return {
            e.strip().lower()
            for e in (self.auth_allowed_emails or "").split(",")
            if e.strip()
        }

    @property
    def serpapi_enabled(self) -> bool:
        return bool(self.serpapi_api_key.strip())

    @property
    def embedding_active(self) -> bool:
        """Embedding scoring runs only when explicitly enabled AND keyed."""
        return self.embedding_enabled and bool(self.voyage_api_key.strip())

    @property
    def use_llm_router(self) -> bool:
        return bool(self.anthropic_api_key.strip())

    @property
    def apollo_enabled(self) -> bool:
        return bool(self.apollo_api_key.strip())

    @property
    def job_alert_mode_normalized(self) -> str:
        mode = (self.job_alert_mode or "digest").strip().lower()
        if mode not in ("digest", "instant", "silent"):
            return "digest"
        return mode

    @property
    def job_sources(self) -> list[str]:
        """Enabled discovery sources, normalized (lowercased, de-duped, ordered)."""
        seen: list[str] = []
        for raw in self.job_sources_enabled.split(","):
            name = raw.strip().lower()
            if name and name not in seen:
                seen.append(name)
        return seen

    @property
    def slack_enabled(self) -> bool:
        """Slack is off by default now that in-app chat owns the conversation.

        Requires both a bot token *and* an explicit ``SLACK_TRANSPORT_ENABLED=true``
        so a leftover token in .env can't silently resurrect the old channel.
        """
        return self.slack_transport_enabled and bool(self.slack_bot_token.strip())

    @property
    def push_active(self) -> bool:
        return bool(
            self.push_enabled
            and self.apns_key_id.strip()
            and self.apns_team_id.strip()
            and self.apns_bundle_id.strip()
            and self.apns_key_path.strip()
        )

    @property
    def outbound_sms_enabled(self) -> bool:
        return bool(
            self.twilio_account_sid.strip()
            and self.twilio_auth_token.strip()
            and self.twilio_from_number.strip()
        )


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    # Some shells export ANTHROPIC_API_KEY="" (empty). pydantic-settings ranks
    # env vars above the .env file, so an empty export would silently shadow a
    # real key in .env. Treat an empty env var as absent and fall back to .env.
    if not s.anthropic_api_key.strip():
        from dotenv import dotenv_values

        from_file = dotenv_values(".env").get("ANTHROPIC_API_KEY")
        if from_file:
            s.anthropic_api_key = from_file
    return s
