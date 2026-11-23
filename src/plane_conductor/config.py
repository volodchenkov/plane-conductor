"""Plane Conductor *runtime* settings — host-wide concerns only.

Per-workspace concerns (Plane creds, project, prompts dir, secrets, agents,
labels) live in the per-workspace YAMLs under `conductor_dir/`. See
`conductor_config.WorkspaceConfig`.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator, model_validator
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

    # --- Notifications to a workbench instance (Telegram bridge); empty URL = off ---
    workbench_notify_url: str = Field(
        default="",
        description="Base URL of the workbench ingest (any host). Empty = notifications off. "
        "https is required unless the host is a loopback literal (127.0.0.0/8, ::1).",
    )
    workbench_notify_token_file: Path | None = Field(
        default=None, description="File holding the ingest bearer; re-read on every send."
    )
    workbench_notify_timeout: float = Field(
        default=3.0, gt=0, description="Total seconds per call."
    )
    workbench_notify_connect_timeout: float = Field(default=2.0, gt=0)
    workbench_notify_ca_file: Path | None = Field(
        default=None, description="Private CA bundle; empty = system trust store."
    )
    workbench_notify_spool_dir: Path | None = Field(
        default=None, description="Failure spool; default <log_dir>/workbench-notify-spool."
    )
    workbench_notify_spool_max: int = Field(default=200, ge=1)
    workbench_notify_spool_max_age: int = Field(default=24 * 3600, ge=60, description="Seconds.")

    # --- Reliability state (QSALE-55) ---
    state_dir: Path = Field(
        default=Path(),
        description="Directory of conductor.db (SQLite WAL), conductor.lock and run stream "
        "journals. Empty = <LOG_DIR>/state (absolute) — writable under the unit sandbox.",
    )
    start_retries: int = Field(
        default=3, ge=0, description="Retries after the first start attempt."
    )
    start_retry_backoff_seconds: list[float] = Field(default_factory=lambda: [5.0, 15.0, 45.0])
    start_no_tower_timeout_seconds: float = Field(default=180.0, gt=0)
    handoff_ack_seconds: float = Field(default=300.0, gt=0)
    handoff_retry_interval_seconds: float = Field(default=300.0, gt=0)
    handoff_max_retries: int = Field(default=3, ge=0)
    queue_max_age_seconds: float = Field(default=5400.0, gt=0)
    source_wait_seconds: float = Field(default=10.0, ge=0)
    reader_drain_seconds: float = Field(default=10.0, ge=0)
    alert_max_attempts: int = Field(default=20, ge=1)
    retry_after_max_seconds: float = Field(default=30.0, ge=0)
    retry_total_max_seconds: float = Field(default=60.0, ge=0)
    request_deadline_seconds: float = Field(default=90.0, gt=0)
    state_retention_days: int = Field(default=14, ge=1)
    dedupe_retention_days: int = Field(default=365, ge=1)
    dispatcher_tick_seconds: float = Field(default=5.0, gt=0)
    watcher_tick_seconds: float = Field(default=30.0, gt=0)

    @model_validator(mode="after")
    def _default_state_dir(self) -> Settings:
        if self.state_dir == Path():
            self.state_dir = self.log_dir.absolute() / "state"
        else:
            self.state_dir = self.state_dir.absolute()
        return self

    @model_validator(mode="after")
    def _queue_age_exceeds_session(self) -> Settings:
        if self.queue_max_age_seconds <= self.session_timeout_seconds:
            raise ValueError("queue_max_age_seconds must exceed session_timeout_seconds")
        return self

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
