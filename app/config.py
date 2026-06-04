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

    @property
    def use_llm_router(self) -> bool:
        return bool(self.anthropic_api_key.strip())

    @property
    def apollo_enabled(self) -> bool:
        return bool(self.apollo_api_key.strip())

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
