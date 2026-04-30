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


@lru_cache
def get_settings() -> Settings:
    return Settings()
