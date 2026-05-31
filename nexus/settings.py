from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
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

    llm_provider: str = Field(default="anthropic", alias="LLM_PROVIDER")
    # When None, each provider falls back to its own DEFAULT_MODEL.
    llm_model: str | None = Field(default=None, alias="LLM_MODEL")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    env: str = Field(default="dev", alias="ENV")


def get_settings() -> Settings:
    return Settings()
