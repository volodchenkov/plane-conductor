"""Plane Conductor *runtime* settings — host-wide concerns only.

Per-workspace concerns (Plane creds, project, prompts dir, secrets, agents,
labels) live in the per-workspace YAMLs under `conductor_dir/`. See
`conductor_config.WorkspaceConfig`.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Host-wide runtime knobs (port, log dir, capacity caps, claude binary).

    Loaded from `runtime.env` (system) or `.env` (cwd) and process env.
    """

    model_config = SettingsConfigDict(
        # System-wide first; a local file in cwd overrides; process env wins.
        env_file=(
            "/etc/plane-conductor/runtime.env",
            "/etc/plane-conductor/.env",  # legacy filename, still honoured
            "runtime.env",
            ".env",
        ),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Webhook server ---
    webhook_host: str = Field(default="0.0.0.0")
    webhook_port: int = Field(default=8000, ge=1, le=65535)

    # --- Where workspace configs live ---
    conductor_dir: Path = Field(
        default=Path("/etc/plane-conductor/conductor.d"),
        description="Directory of per-workspace YAML configs (one file = one workspace).",
    )

    # --- Agent invocation ---
    claude_binary: str = Field(default="claude", description="Path to the `claude` CLI binary")

    # --- Operations ---
    log_dir: Path = Field(default=Path("./logs"))
    log_level: str = Field(default="INFO")
    log_format: str = Field(default="pretty", description="pretty | json")
    max_concurrent_sessions: int = Field(default=5, ge=1)
    session_timeout_seconds: int = Field(default=3600, ge=10)
    shutdown_grace_seconds: float = Field(default=30.0, ge=0.0)

    @field_validator("log_format")
    @classmethod
    def _validate_log_format(cls, v: str) -> str:
        v = v.lower()
        if v not in {"pretty", "json"}:
            raise ValueError("log_format must be 'pretty' or 'json'")
        return v

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        upper = v.upper()
        if upper not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid log_level: {v}")
        return upper
