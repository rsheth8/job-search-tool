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
    # Cap how many *new* postings get LLM-scored per tick (token-cost guard).
    # Survivors of the free pre-filter beyond this carry over to the next tick.
    job_max_scored_per_tick: int = 40
    # How to notify on new matches: digest (one summary per tick), instant
    # (one message per job, legacy), or silent (store only).
    job_alert_mode: str = "digest"
    # How many top matches to show in a digest body (rest summarized as "+N more").
    job_digest_top_n: int = 3
    # The user the background loop alerts (Slack user id). Empty = alert the
    # busiest known user, mirroring dashboard.default_user().
    job_alert_user: str = ""
    # Free sources are on by default. Paid sources (added later) stay off until
    # explicitly enabled with their own budget caps, mirroring the Apollo guards.
    job_sources_enabled: str = "greenhouse,lever,ashby,rss,directory"

    # --- Wide discovery (A/B/C) — profile-driven, no company list required ---
    job_wide_rss_enabled: bool = True
    job_wide_rss_feeds: str = "hn-hiring,remoteok"
    job_wide_directory_enabled: bool = True
    job_directory_boards_per_tick: int = 12
    job_directory_max_jobs_per_board: int = 25
    job_directory_data_path: str = "data/ats_boards.json"
    job_wide_aggregator_enabled: bool = False
    serpapi_api_key: str = ""
    job_aggregator_max_per_day: int = 5

    # --- Resume tailoring (assisted apply) ---------------------------------
    # Base .tex files live on the Fly volume, not in git (personal info).
    resume_tex_dir: str = "/data/resumes"
    resume_tailor_enabled: bool = True
    tectonic_bin: str = "tectonic"

    @property
    def serpapi_enabled(self) -> bool:
        return bool(self.serpapi_api_key.strip())

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
        return bool(self.slack_bot_token.strip())

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
