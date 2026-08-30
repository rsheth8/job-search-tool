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

    # Claude Haiku for scoring / outreach / resume drafts — never for chat NLU.
    # Chat and POST /agent always use the heuristic router (and on-device
    # classification on Apple Intelligence devices).
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5"

    # Token-saving guards for paid Anthropic calls (scoring / drafts).
    llm_rate_limit_per_min: int = 30   # token-bucket cap on paid API calls
    llm_max_sms_chars: int = 480       # truncate inbound text before sending
    # Per-user daily cap on paid Anthropic calls (discovery scoring + drafts).
    # 0 = unlimited. Testers share one key; this keeps one chatty account from
    # starving everyone else's scoring budget.
    llm_max_calls_per_user_per_day: int = 80

    default_followup_days: int = 7

    # --- Job discovery -----------------------------------------------------
    # Poll cadence for the background discovery loop (free ATS feeds tolerate
    # frequent polls). Each new posting is deduped, so we never re-alert.
    job_poll_seconds: int = 600
    # A posting must score >= this (0..1) to be alerted. Tune via chat (TUNE intent).
    job_relevance_threshold: float = 0.6
    # Auto-stage very-high-confidence matches straight into the apply queue (skip
    # triage). 0 = off; set e.g. 0.85 to only auto-queue your strongest matches.
    job_auto_queue_threshold: float = 0.0
    # Cap how many *new* postings get LLM-scored per tick (token-cost guard).
    # Survivors of the free pre-filter beyond this carry over to the next tick.
    job_max_scored_per_tick: int = 60
    # How to notify on new matches: digest (one summary per tick), instant
    # (one message per job, legacy), or silent (store only).
    job_alert_mode: str = "digest"
    # How many top matches to show in a digest body (rest summarized as "+N more").
    job_digest_top_n: int = 5
    # Optional override: alert this user_id only. Empty = every discovery user.
    job_alert_user: str = ""
    # Free sources are on by default.
    job_sources_enabled: str = (
        "greenhouse,lever,ashby,workable,smartrecruiters,rss,directory,swelist,yc"
    )

    # --- Wide discovery — profile-driven, no company list required ----------
    job_wide_rss_enabled: bool = True
    job_wide_rss_feeds: str = "hn-hiring,remoteok,weworkremotely,himalayas,remotive"
    job_wide_directory_enabled: bool = True
    job_directory_boards_per_tick: int = 32
    job_directory_max_jobs_per_board: int = 25
    job_directory_data_path: str = "data/ats_boards.json"
    job_company_catalog_path: str = "data/company_catalog.json"
    # Slug-probe catalog names in the user's sector; persist live tokens.
    # Capped per tick so we never poll thousands of names in one pass.
    job_catalog_probe_enabled: bool = True
    job_catalog_probe_per_tick: int = 6
    # HEAD/GET the apply URL on the scored shortlist only. Fail-open.
    job_verify_apply_urls: bool = True
    # Pitt CSC / Simplify lists (listings.json, not the README buttons).
    job_wide_swelist_enabled: bool = True
    job_swelist_list: str = "summer2027,newgrad"
    job_swelist_max_age_days: int = 21
    # Y Combinator public jobs landing page (featured postings).
    job_wide_yc_enabled: bool = True

    # Ghost-job filter: drop never-really-hiring reqs (evergreen language,
    # reposts, stale, scam contact) beyond quality.py's spam gate. Conservative
    # + free (rules only). On by default; flip off to disable.
    ghost_filter_enabled: bool = True

    # --- Resume tailoring (assisted apply) ---------------------------------
    # Base .tex files live on the Fly volume, not in git (personal info).
    resume_tex_dir: str = "/data/resumes"
    resume_tailor_enabled: bool = True
    tectonic_bin: str = "tectonic"
    # Optional one-page cover letter PDF, built on demand (Apply → documents).
    cover_letter_enabled: bool = True

    # --- Personalized re-ranker --------------------------------------------
    # On for beta. Cold-starts as a no-op until the per-class label minimums;
    # trains on apply → response → interview → ghost. Tests force this off.
    reranker_enabled: bool = True
    reranker_min_positive: int = 5   # min 'applied' labels before the model engages
    reranker_min_negative: int = 5   # min 'dismissed'/'snoozed' labels before engaging
    # Grade applied labels by their real outcome stage (the CRM funnel).
    reranker_outcome_weighting: bool = True

    # --- Eligibility / qualification gate ----------------------------------
    # Drop roles the candidate clearly isn't qualified for / couldn't realistically
    # do, given their level. Rule tier only (free). Candidate level comes from the
    # profile's seniority, falling back to ``eligibility_candidate_level``.
    eligibility_filter_enabled: bool = True
    eligibility_candidate_level: str = "entry"   # fallback when profile has no seniority
    # Drop clearly non-technical roles (sales/recruiting/marketing/admin/etc.) when
    # the profile looks technical (or is empty). Off for marketing/HR/etc. profiles.
    eligibility_field_filter: bool = True

    # --- Apply API (iOS uses Bearer session; token still useful for scripts) -
    apply_api_token: str = ""
    # Comma-separated allowed CORS origins; "*" allows any (token still gates writes).
    apply_cors_origins: str = "*"
    # Optional regex (Starlette allow_origin_regex). Used when origins isn't "*".
    apply_cors_origin_regex: str = ""
    # Local/tests: personal APIs accept ?user= without a session when the apply
    # token is also blank. Production MUST set this false (see fly.toml).
    auth_fail_open: bool = True
    # Invite-only Sign in with Apple. Empty = anyone with a valid Apple token.
    # Comma-separated emails (case-insensitive). Private Relay addresses work.
    auth_allowed_emails: str = ""
    # Optional: ping this user_id in chat when a tester submits feedback.
    feedback_notify_user: str = ""
    # Sentry DSN. Empty = no-op (tests, local). Set as a Fly secret in prod.
    sentry_dsn: str = ""
    # Optional GitHub token — raises unauthenticated rate limits for profile import.
    github_token: str = ""

    # Push notifications to the iPhone app (APNs). Off until PUSH_ENABLED plus all
    # four APNs values are set; app/push.py is a no-op meanwhile.
    push_enabled: bool = False
    apns_key_id: str = ""
    apns_team_id: str = ""
    apns_bundle_id: str = ""
    apns_key_path: str = ""
    apns_use_sandbox: bool = False

    # --- Sign in with Apple + in-app chat ---------------------------------
    # Comma-separated audiences accepted on Apple identity tokens (iOS bundle id).
    apple_client_ids: str = "com.rahil.jobpilot"
    # Session lifetime for Bearer tokens minted at login.
    auth_session_days: int = 90
    # When true, POST /auth/dev mints a session without Apple (tests + local).
    auth_allow_dev_login: bool = False
    # Stable user id for POST /auth/dev when the client omits user_id.
    auth_dev_user_id: str = ""
    # On first Apple sign-in, fold this legacy user id into the new account
    # (one-shot via usermerge). Empty = no migration.
    auth_legacy_user_id: str = ""

    @property
    def allowed_emails(self) -> set[str]:
        return {
            e.strip().lower()
            for e in (self.auth_allowed_emails or "").split(",")
            if e.strip()
        }

    @property
    def use_llm_router(self) -> bool:
        return bool(self.anthropic_api_key.strip())

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
    def push_active(self) -> bool:
        return bool(
            self.push_enabled
            and self.apns_key_id.strip()
            and self.apns_team_id.strip()
            and self.apns_bundle_id.strip()
            and self.apns_key_path.strip()
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
