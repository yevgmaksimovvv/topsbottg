from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_domain: str = Field(min_length=1)
    bot_token: str = Field(min_length=1)
    database_url: str = Field(min_length=1)
    admin_telegram_ids: str = Field(min_length=1)
    mini_app_url: str = Field(min_length=1)
    broadcast_rate_per_second: float = Field(ge=0.1, le=100.0)
    mini_app_init_data_max_age_seconds: int = Field(default=86400, ge=1)
    postgresql_max_connections: int = Field(default=5, ge=1, le=20)
    app_host: str = Field(default="0.0.0.0")
    app_port: int = Field(default=8000, ge=1, le=65535)
    environment: Literal["development", "test", "production"] = "development"

    @field_validator(
        "app_domain",
        "bot_token",
        "database_url",
        "admin_telegram_ids",
        "mini_app_url",
        mode="before",
    )
    @classmethod
    def _require_nonempty(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("required env var is missing")
        stripped = value.strip()
        if not stripped:
            raise ValueError("required env var is empty")
        return stripped

    @field_validator("app_domain")
    @classmethod
    def _validate_app_domain(cls, value: str) -> str:
        normalized = value.lower()
        parsed = urlsplit(f"https://{normalized}")
        if normalized != parsed.hostname or parsed.scheme != "https" or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("APP_DOMAIN must be a hostname without scheme or path")
        if parsed.port is not None:
            raise ValueError("APP_DOMAIN must not include a port")
        return normalized

    @model_validator(mode="after")
    def _validate_mini_app_url(self) -> Settings:
        parsed = urlsplit(self.mini_app_url)
        if parsed.scheme != "https":
            raise ValueError("MINI_APP_URL must use https")
        if not parsed.hostname:
            raise ValueError("MINI_APP_URL must include a host")
        if parsed.path and not parsed.path.startswith("/"):
            raise ValueError("MINI_APP_URL path is invalid")
        if self.app_domain == "localhost":
            if parsed.hostname != "localhost":
                raise ValueError("MINI_APP_URL host must be localhost when APP_DOMAIN is localhost")
        elif parsed.hostname != self.app_domain:
            raise ValueError("MINI_APP_URL host must match APP_DOMAIN")
        return self

    @property
    def admin_ids_set(self) -> set[int]:
        ids: set[int] = set()
        for raw in self.admin_telegram_ids.split(","):
            raw = raw.strip()
            if not raw:
                continue
            ids.add(int(raw))
        return ids

    @property
    def is_testing(self) -> bool:
        return self.environment == "test"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
