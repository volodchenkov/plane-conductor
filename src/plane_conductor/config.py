from __future__ import annotations

from pathlib import Path
from uuid import UUID

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Plane Conductor *runtime* config — ports, secrets, paths.

    Workflow config (agents, labels, states) lives in a separate YAML file
    pointed to by `CONDUCTOR_CONFIG`; see `conductor_config.ConductorConfig`.

    Settings are loaded from environment variables (and an optional `.env` file).
    """

    model_config = SettingsConfigDict(
        # System-wide config first; a local `.env` next to the cwd overrides it.
        env_file=("/etc/plane-conductor/.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Plane connection ---
    plane_base_url: str = Field(..., description="Plane base URL, e.g. https://plane.example.io")
    plane_api_key: str = Field(..., description="Plane API token")
    plane_workspace_slug: str = Field(..., description="Workspace slug (lowercase)")
    plane_project_id: UUID = Field(..., description="Project UUID")

    # --- Webhook ---
    webhook_secret: str = Field(..., description="Shared HMAC secret for webhook verification")
    webhook_host: str = Field(default="0.0.0.0")
    webhook_port: int = Field(default=8000, ge=1, le=65535)
    webhook_signature_header: str = Field(default="X-Plane-Signature")

    # --- Workflow config (the YAML file) ---
    conductor_config: Path = Field(
        default=Path("/etc/plane-conductor/conductor.yaml"),
        description="Path to conductor.yaml (agents, labels, states, behaviour).",
    )

    # --- Agent invocation ---
    email_domain: str = Field(..., description="Bot email domain, e.g. example.io")
    prompts_dir: Path = Field(..., description="Directory with agent prompt files")
    agent_working_dir: Path | None = Field(
        default=None, description="Working dir for spawned claude subprocess; defaults to cwd"
    )
    initiator_uuid: UUID = Field(..., description="Human initiator UUID — ignored as a target")
    claude_binary: str = Field(default="claude", description="Path to the `claude` CLI binary")

    # --- Operations ---
    log_dir: Path = Field(default=Path("./logs"))
    log_level: str = Field(default="INFO")
    log_format: str = Field(default="pretty", description="pretty | json")
    max_concurrent_sessions: int = Field(default=5, ge=1)
    session_timeout_seconds: int = Field(default=3600, ge=10)
    shutdown_grace_seconds: float = Field(default=30.0, ge=0.0)
    allowed_nicknames: str = Field(
        default="", description="Comma-separated allow-list; empty = allow all configured agents"
    )

    # --- Optional S3 (for ui-tester / screenshot use cases — not used by Conductor itself) ---
    s3_bucket: str = Field(default="")
    s3_endpoint: str = Field(default="")

    @field_validator("plane_base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

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

    @property
    def allowed_nicknames_set(self) -> frozenset[str]:
        items = [n.strip().lower() for n in self.allowed_nicknames.split(",") if n.strip()]
        return frozenset(items)
