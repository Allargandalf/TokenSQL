"""Application configuration loaded from environment variables."""

from functools import lru_cache
import ipaddress
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _validate_qwen_base_url(value: str, *, allow_remote: bool) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("qwen_base_url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("qwen_base_url must not contain credentials, query, or fragment")
    host = parsed.hostname.casefold()
    is_loopback = host == "localhost"
    try:
        is_loopback = is_loopback or ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    if not allow_remote and not is_loopback:
        raise ValueError(
            "qwen_base_url must be loopback unless qwen_allow_remote=true"
        )
    return candidate


class Settings(BaseSettings):
    """Runtime settings with Qwen disabled and loopback-only by default."""

    model_config = SettingsConfigDict(
        env_prefix="TOKENSQL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(default="sqlite+pysqlite:///:memory:")
    database_echo: bool = False
    sqlite_busy_timeout_ms: int = Field(default=5_000, ge=0)

    qwen_enabled: bool = False
    qwen_base_url: str = "http://127.0.0.1:18080/v1"
    qwen_model: str | None = None
    qwen_api_key: str | None = None
    qwen_timeout_seconds: float = Field(default=30.0, gt=0.0, le=300.0)
    qwen_min_confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    qwen_allow_remote: bool = False

    @model_validator(mode="after")
    def validate_qwen(self) -> "Settings":
        self.qwen_base_url = _validate_qwen_base_url(
            self.qwen_base_url, allow_remote=self.qwen_allow_remote
        )
        if self.qwen_model is not None:
            self.qwen_model = self.qwen_model.strip() or None
        if self.qwen_enabled and not self.qwen_model:
            raise ValueError("TOKENSQL_QWEN_MODEL is required when Qwen is enabled")
        if self.qwen_api_key is not None:
            self.qwen_api_key = self.qwen_api_key.strip() or None
        return self

    @property
    def qwen_configured(self) -> bool:
        return bool(self.qwen_model)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
