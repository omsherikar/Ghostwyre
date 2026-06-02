"""Application settings, loaded from the environment via pydantic-settings.

All secrets live here and nowhere else — never hardcode tokens (the block-secrets
PreToolUse hook enforces this). Fields that aren't needed until later phases are
optional so the skeleton boots before Slack/Anthropic/X are wired up.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Slack (Phase 1) ---
    slack_bot_token: str | None = Field(default=None, description="xoxb-… bot token")
    slack_app_token: str | None = Field(default=None, description="xapp-… Socket Mode token")
    slack_signing_secret: str | None = None
    target_channel_id: str | None = Field(
        default=None, description="Channel /draft-post reads from"
    )
    message_fetch_limit: int = Field(default=50, description="How many recent messages to pull")

    # --- LLM (Phase 2) ---
    anthropic_api_key: str | None = None
    llm_model: str = "claude-sonnet-4-6"
    max_drafts: int = 3

    # --- Database (Phase 0+) ---
    database_url: str = "postgresql+asyncpg://ghostwyre:ghostwyre@localhost:5432/ghostwyre"

    # --- Publishing (Phase 4) ---
    publisher: Literal["dry", "x"] = "dry"
    x_api_key: str | None = None
    x_api_secret: str | None = None
    x_access_token: str | None = None
    x_access_token_secret: str | None = None

    # --- Misc ---
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Cached singleton so the env file is parsed once per process."""
    return Settings()
