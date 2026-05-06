"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest

from plane_conductor.conductor_config import (
    AgentDef,
    LabelDef,
    LabelsConfig,
    StateDef,
    WorkspaceConfig,
)
from plane_conductor.config import Settings


@pytest.fixture
def initiator_uuid() -> UUID:
    return UUID("00000000-0000-0000-0000-000000000099")


@pytest.fixture
def project_uuid() -> UUID:
    return UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def webhook_secret() -> str:
    return "test-secret-do-not-use-please-this-is-32-chars-long"


@pytest.fixture
def workspace_config(
    tmp_path: Path,
    initiator_uuid: UUID,
    project_uuid: UUID,
    webhook_secret: str,
) -> WorkspaceConfig:
    """In-memory WorkspaceConfig used by most unit tests."""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    return WorkspaceConfig(
        workspace_slug="testws",
        plane_base_url="https://plane.test",
        plane_api_key="test-key",
        project_id=project_uuid,
        initiator_uuid=initiator_uuid,
        webhook_secret=webhook_secret,
        email_domain="example.io",
        prompts_dir=prompts_dir,
        agent_working_dir=tmp_path,
        agents=[
            AgentDef(nickname="sark", prompt_role="system-analyst", display_name="Sark"),
            AgentDef(nickname="rinzler", prompt_role="python-developer", display_name="Rinzler"),
            AgentDef(nickname="gem", prompt_role="ui-tester", display_name="Gem"),
        ],
        labels=LabelsConfig(
            artifacts=[
                LabelDef(name="artifact:spec", color="#3b82f6"),
                LabelDef(name="artifact:backend", color="#10b981"),
            ],
            roles=[
                LabelDef(name="role:system-analyst", color="#60a5fa"),
            ],
        ),
        states=[
            StateDef(name="Review", group="started", color="#f59e0b"),
        ],
        announce_spawn=False,
        allowed_nicknames=[],
    )


@pytest.fixture
def workspaces(workspace_config: WorkspaceConfig) -> dict[str, WorkspaceConfig]:
    """Single-workspace dict in the same shape the server uses."""
    return {workspace_config.workspace_slug: workspace_config}


@pytest.fixture
def settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Settings]:
    # Make sure no real env or .env leaks in.
    for var in (
        "WEBHOOK_HOST",
        "WEBHOOK_PORT",
        "CONDUCTOR_DIR",
        "LOG_DIR",
        "LOG_LEVEL",
        "LOG_FORMAT",
        "MAX_CONCURRENT_SESSIONS",
        "SESSION_TIMEOUT_SECONDS",
        "SHUTDOWN_GRACE_SECONDS",
        "CLAUDE_BINARY",
    ):
        monkeypatch.delenv(var, raising=False)

    log_dir = tmp_path / "logs"
    yield Settings(
        webhook_host="127.0.0.1",
        webhook_port=8000,
        conductor_dir=tmp_path / "conductor.d",
        log_dir=log_dir,
        log_level="WARNING",
        log_format="pretty",
        claude_binary="claude",
        _env_file=None,  # type: ignore[call-arg]
    )
