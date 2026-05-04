"""Tests for the YAML workflow config (`conductor.yaml`)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from plane_conductor.conductor_config import (
    AgentDef,
    ConductorConfig,
    LabelDef,
    LabelsConfig,
    StateDef,
    load_config,
)


def test_agent_nickname_is_lowercased() -> None:
    a = AgentDef(nickname="Sark", prompt_role="system-analyst")
    assert a.nickname == "sark"


def test_duplicate_nicknames_rejected() -> None:
    with pytest.raises(ValidationError):
        ConductorConfig(
            agents=[
                AgentDef(nickname="sark", prompt_role="system-analyst"),
                AgentDef(nickname="SARK", prompt_role="another"),
            ]
        )


def test_extra_keys_rejected() -> None:
    with pytest.raises(ValidationError):
        AgentDef(nickname="x", prompt_role="y", unknown_field="oops")  # type: ignore[call-arg]


def test_agents_by_nickname_lookup() -> None:
    cfg = ConductorConfig(
        agents=[
            AgentDef(nickname="sark", prompt_role="system-analyst"),
            AgentDef(nickname="rinzler", prompt_role="python-developer"),
        ]
    )
    by = cfg.agents_by_nickname()
    assert set(by) == {"sark", "rinzler"}
    assert by["sark"].prompt_role == "system-analyst"


def test_all_labels_concatenates_artifacts_and_roles() -> None:
    cfg = ConductorConfig(
        agents=[AgentDef(nickname="x", prompt_role="y")],
        labels=LabelsConfig(
            artifacts=[LabelDef(name="artifact:spec")],
            roles=[LabelDef(name="role:dev")],
        ),
    )
    assert cfg.all_label_names() == ["artifact:spec", "role:dev"]


def test_load_config_from_yaml(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text(
        "agents:\n"
        "  - nickname: sark\n"
        "    prompt_role: system-analyst\n"
        "    display_name: Sark — Analyst\n"
        "labels:\n"
        "  artifacts:\n"
        "    - { name: artifact:spec, color: '#3b82f6' }\n"
        "  roles: []\n"
        "states:\n"
        "  - { name: Review, group: started, color: '#f59e0b' }\n"
        "announce_spawn: false\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert len(cfg.agents) == 1
    assert cfg.agents[0].nickname == "sark"
    assert cfg.labels.artifacts[0].name == "artifact:spec"
    assert cfg.states == [StateDef(name="Review", group="started", color="#f59e0b")]
    assert cfg.announce_spawn is False


def test_load_config_rejects_non_mapping(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="top-level must be a mapping"):
        load_config(p)


def test_load_config_empty_file_rejected(tmp_path: Path) -> None:
    """Empty YAML loads as None → must be a mapping → reject."""
    p = tmp_path / "empty.yaml"
    p.write_text("", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(p)


def test_announce_spawn_default_true() -> None:
    cfg = ConductorConfig(agents=[AgentDef(nickname="x", prompt_role="y")])
    assert cfg.announce_spawn is True


def test_example_sdlc_config_loads() -> None:
    """The shipped example config must always be valid."""
    repo = Path(__file__).resolve().parents[1]
    cfg = load_config(repo / "examples" / "sdlc-conductor.yaml")
    assert len(cfg.agents) == 10
    assert {a.nickname for a in cfg.agents} >= {"sark", "rinzler", "dumont"}


def test_example_minimal_config_loads() -> None:
    repo = Path(__file__).resolve().parents[1]
    cfg = load_config(repo / "examples" / "minimal-conductor.yaml")
    assert len(cfg.agents) == 1
    assert cfg.agents[0].nickname == "dev"
