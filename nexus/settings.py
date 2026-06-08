from __future__ import annotations

import logging
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# python-dotenv emits warnings to its logger when it can't parse a line
# (e.g. aliases, conditionals, function definitions in ~/.zshrc). Those
# lines are intentionally ignored — silence the noise so a busy .zshrc
# doesn't print warnings on every `nexus` invocation.
logging.getLogger("dotenv.main").setLevel(logging.ERROR)


def _env_file_sources() -> tuple[str, ...]:
    """Files pydantic-settings will read, in increasing precedence order.

    Higher-precedence sources override earlier ones; environment variables
    always win over file values.

    - ``~/.zshrc`` (if present) — many users keep API keys here behind
      ``export FOO=bar``; python-dotenv tolerates the `export` prefix.
      Lines using zsh-specific syntax (aliases, conditionals, functions)
      are silently skipped — only simple ``export KEY=VALUE`` lines are
      picked up. Lowest precedence.
    - ``.env`` in the project root. Higher precedence; lets a project
      override what's in the user's shell rc.
    """
    sources: list[str] = []
    home_zshrc = Path.home() / ".zshrc"
    if home_zshrc.exists():
        sources.append(str(home_zshrc))
    sources.append(".env")
    return tuple(sources)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
    )

    postgres_url: str = Field(
        default="postgresql+asyncpg://nexus:nexus@localhost:5432/nexus",
        alias="POSTGRES_URL",
    )
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")
    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_bot_username: str | None = Field(
        default=None, alias="TELEGRAM_BOT_USERNAME"
    )

    llm_provider: str = Field(default="anthropic", alias="LLM_PROVIDER")
    # When None, each provider falls back to its own DEFAULT_MODEL.
    llm_model: str | None = Field(default=None, alias="LLM_MODEL")

    # Web client.
    web_session_secret: str = Field(
        default="dev-only-insecure-change-me", alias="WEB_SESSION_SECRET"
    )
    # When True, /auth/dev accepts a telegram_id and creates a session without
    # going through the Telegram Login Widget. For local testing only.
    web_dev_auth: bool = Field(default=False, alias="WEB_DEV_AUTH")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    env: str = Field(default="dev", alias="ENV")


def get_settings() -> Settings:
    # Re-evaluate sources at each call so tests (and runtime $HOME changes)
    # see the current shell rc / .env state. The cost is one stat per .zshrc
    # / .env file per call, which is negligible.
    return Settings(_env_file=_env_file_sources())
