from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = Field(alias="DATABASE_URL")

    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.5-flash-lite", alias="GEMINI_MODEL")

    api_auth_key: str = Field(default="", alias="API_AUTH_KEY")

    slack_webhook_url: str = Field(default="", alias="SLACK_WEBHOOK_URL")

    external_callback_url: str = Field(default="", alias="EXTERNAL_CALLBACK_URL")
    external_callback_key: str = Field(default="", alias="EXTERNAL_CALLBACK_KEY")

    seven_day_job_enabled: bool = Field(default=True, alias="SEVEN_DAY_JOB_ENABLED")
    # Cron lands on Day 8 at 00:00 IST for any rad whose 7-day window has
    # ended. IST chosen to match incubation_started_at semantics in workflow.
    seven_day_job_cron_hour: int = Field(default=0, alias="SEVEN_DAY_JOB_CRON_HOUR")
    seven_day_job_cron_minute: int = Field(default=0, alias="SEVEN_DAY_JOB_CRON_MINUTE")
    seven_day_job_timezone: str = Field(
        default="Asia/Kolkata", alias="SEVEN_DAY_JOB_TIMEZONE"
    )

    incubation_days: int = 7
    total_pool_cases: int = 80
    first_checkpoint: int = 20
    final_checkpoint: int = 80
    # How many cases to return on every activation-data / webhook call.
    cases_per_call: int = Field(default=2, alias="CASES_PER_CALL")

    # ---------- ClickHouse (read-only; rad_details + presence) ----------
    # Used by T1 (populate_other_details on rad onboarding) and T2
    # (slot_compliance cron). All reads, no writes. Empty host disables both
    # features without raising at startup.
    clickhouse_host: str = Field(default="", alias="CLICKHOUSE_HOST")
    clickhouse_port: int = Field(default=8123, alias="CLICKHOUSE_PORT")
    clickhouse_user: str = Field(default="", alias="CLICKHOUSE_USER")
    clickhouse_password: str = Field(default="", alias="CLICKHOUSE_PASSWORD")
    clickhouse_timeout_s: float = Field(default=30.0, alias="CLICKHOUSE_TIMEOUT_S")

    # ---------- Slot-compliance cron (T2) ----------
    # Every 6h IST by default (00/06/12/18). Enabled by default — the cron
    # only does work for rads with other_details populated, so it's a
    # cheap no-op until rads onboard.
    slot_compliance_job_enabled: bool = Field(
        default=True, alias="SLOT_COMPLIANCE_JOB_ENABLED"
    )
    slot_compliance_job_cron_hours: str = Field(
        default="0,6,12,18", alias="SLOT_COMPLIANCE_JOB_CRON_HOURS"
    )
    slot_compliance_job_timezone: str = Field(
        default="Asia/Kolkata", alias="SLOT_COMPLIANCE_JOB_TIMEZONE"
    )
    # The look-back window for "slots that just ended". Defaults to 6h to
    # match the cron cadence.
    slot_compliance_lookback_hours: int = Field(
        default=6, alias="SLOT_COMPLIANCE_LOOKBACK_HOURS"
    )

    # ---------- Startup auto-actions (zero-touch deploy) ----------
    # Run alembic upgrade head at app startup. Idempotent. Off only for tests.
    auto_migrate_on_startup: bool = Field(
        default=True, alias="AUTO_MIGRATE_ON_STARTUP"
    )
    # On every boot, kick off in the background:
    #   1. populate_other_details for any rad with NULL other_details
    #   2. one deep slot_compliance tick covering the last N hours
    # Both are idempotent. Set to 0 to disable the deep tick (the regular
    # 6h cron still runs).
    startup_backfill_enabled: bool = Field(
        default=True, alias="STARTUP_BACKFILL_ENABLED"
    )
    startup_backfill_lookback_hours: int = Field(
        default=720, alias="STARTUP_BACKFILL_LOOKBACK_HOURS"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
