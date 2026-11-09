import base64
import hashlib
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "home-agent"
    ha_base_url: str = "http://127.0.0.1:8123"
    ha_token: str = ""

    app_secret_key: str = "dev-secret-change-me"
    app_encryption_key: str = ""

    database_url: str = "sqlite+aiosqlite:///./data/agent.db"

    log_level: str = "INFO"
    cors_origins: str = ""

    access_token_minutes: int = 120
    refresh_token_days: int = 7

    initial_admin_username: str = "admin"
    initial_admin_password: str = ""

    auto_create_schema: bool = True

    chat_timeout_seconds: float = 120.0
    chat_slow_path_limit_per_minute: int = 20
    ha_call_timeout_seconds: float = 15.0

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def fernet_key(self) -> str:
        if self.app_encryption_key:
            return self.app_encryption_key
        digest = hashlib.sha256(self.app_secret_key.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii")


@lru_cache
def get_settings() -> Settings:
    return Settings()
