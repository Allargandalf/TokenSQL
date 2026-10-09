"""Configuration for the independent quote workspace."""
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class WorkspaceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TOKENSQL_", env_file=".env", extra="ignore"
    )

    workspace_dir: Path = PROJECT_ROOT / "var" / "workspace"
    opencode_url: str = "http://127.0.0.1:8766"
    opencode_provider: str = "qwen38-flash-next-local"
    opencode_model: str = "qwen3.8-flash-next-iq3_s"
    opencode_timeout_seconds: float = Field(default=900, gt=0, le=7200)
    workspace_max_file_mb: int = Field(default=20, ge=1, le=100)
    workspace_max_total_mb: int = Field(default=80, ge=1, le=1000)
    workspace_max_files: int = Field(default=20, ge=1, le=100)

    @field_validator("opencode_url")
    @classmethod
    def valid_server_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("OpenCode URL must be an HTTP(S) server URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("OpenCode URL must not contain credentials or query parameters")
        return value

    @property
    def database_path(self) -> Path:
        return self.workspace_dir.resolve() / "quotes.sqlite3"
