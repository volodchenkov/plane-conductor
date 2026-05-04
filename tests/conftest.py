"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest

from plane_conductor.conductor_config import (
    AgentDef,
    ConductorConfig,
    LabelDef,
    LabelsConfig,
    StateDef,
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
    return "test-secret-do-not-use"


@pytest.fixture
def conductor_config_path(tmp_path: Path) -> Path:
    """Path to a minimal conductor.yaml on disk (for tests that load it via I/O)."""
    p = tmp_path / "conductor.yaml"
    p.write_text(
        "agents:\n"
        "  - { nickname: sark, prompt_role: system-analyst }\n"
        "  - { nickname: rinzler, prompt_role: python-developer }\n"
        "labels: { artifacts: [], roles: [] }\n"
        "states: []\n"
        "announce_spawn: false\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def conductor_config() -> ConductorConfig:
    """In-memory ConductorConfig — used by most unit tests."""
    return ConductorConfig(
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
    )


@pytest.fixture
def settings(
    tmp_path: Path,
    initiator_uuid: UUID,
    project_uuid: UUID,
    webhook_secret: str,
    conductor_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Settings]:
    # Make sure no real .env leaks in.
    for var in (
        "PLANE_BASE_URL",
        "PLANE_API_KEY",
        "PLANE_WORKSPACE_SLUG",
        "PLANE_PROJECT_ID",
        "WEBHOOK_SECRET",
        "EMAIL_DOMAIN",
        "PROMPTS_DIR",
        "INITIATOR_UUID",
        "LOG_DIR",
        "ALLOWED_NICKNAMES",
        "CONDUCTOR_CONFIG",
    ):
        monkeypatch.delenv(var, raising=False)

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    log_dir = tmp_path / "logs"

    yield Settings(
        plane_base_url="https://plane.test",
        plane_api_key="test-key",
        plane_workspace_slug="testws",
        plane_project_id=project_uuid,
        webhook_secret=webhook_secret,
        conductor_config=conductor_config_path,
        email_domain="example.io",
        prompts_dir=prompts_dir,
        initiator_uuid=initiator_uuid,
        log_dir=log_dir,
        log_level="WARNING",
        log_format="pretty",
        allowed_nicknames="",
        agent_working_dir=tmp_path,
        claude_binary="claude",
        _env_file=None,  # type: ignore[call-arg]
    )
