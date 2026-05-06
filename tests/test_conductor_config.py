"""Tests for the per-workspace YAML config and the directory loader."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from plane_conductor.conductor_config import (
    AgentDef,
    LabelDef,
    LabelsConfig,
    StateDef,
    WorkspaceConfig,
    load_workspace,
    load_workspaces,
)

INITIATOR = UUID("00000000-0000-0000-0000-000000000099")
PROJECT = UUID("00000000-0000-0000-0000-000000000001")


def _ws(slug: str = "acme", **overrides: object) -> WorkspaceConfig:
    base: dict[str, object] = {
        "workspace_slug": slug,
        "plane_base_url": "https://plane.test",
        "plane_api_key": "k",
        "project_id": PROJECT,
        "initiator_uuid": INITIATOR,
        "webhook_secret": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "email_domain": "x.io",
        "prompts_dir": Path("/tmp/prompts"),
        "agents": [AgentDef(nickname="dev", prompt_role="developer")],
    }
    base.update(overrides)
    return WorkspaceConfig.model_validate(base)


def test_agent_nickname_is_lowercased() -> None:
    a = AgentDef(nickname="Sark", prompt_role="system-analyst")
    assert a.nickname == "sark"


def test_workspace_slug_is_lowercased() -> None:
    ws = _ws(slug="ACME")
    assert ws.workspace_slug == "acme"


def test_workspace_strips_trailing_slash_from_base_url() -> None:
    ws = _ws(plane_base_url="https://plane.test/")
    assert ws.plane_base_url == "https://plane.test"


def test_duplicate_nicknames_rejected() -> None:
    with pytest.raises(ValidationError):
        _ws(
            agents=[
                AgentDef(nickname="sark", prompt_role="system-analyst"),
                AgentDef(nickname="SARK", prompt_role="another"),
            ]
        )


def test_extra_keys_rejected() -> None:
    with pytest.raises(ValidationError):
        AgentDef(nickname="x", prompt_role="y", unknown_field="oops")  # type: ignore[call-arg]


def test_workspace_extra_keys_rejected() -> None:
    with pytest.raises(ValidationError):
        _ws(unsupported_root_key="oops")


def test_agents_by_nickname_lookup() -> None:
    cfg = _ws(
        agents=[
            AgentDef(nickname="sark", prompt_role="system-analyst"),
            AgentDef(nickname="rinzler", prompt_role="python-developer"),
        ]
    )
    by = cfg.agents_by_nickname()
    assert set(by) == {"sark", "rinzler"}
    assert by["sark"].prompt_role == "system-analyst"


def test_all_labels_concatenates_artifacts_and_roles() -> None:
    cfg = _ws(
        labels=LabelsConfig(
            artifacts=[LabelDef(name="artifact:spec")],
            roles=[LabelDef(name="role:dev")],
        ),
    )
    assert cfg.all_label_names() == ["artifact:spec", "role:dev"]


def test_allowed_nicknames_normalised() -> None:
    cfg = _ws(allowed_nicknames=["  Sark ", "", "RINZLER"])
    assert cfg.allowed_nicknames == ["sark", "rinzler"]
    assert cfg.allowed_nicknames_set == frozenset({"sark", "rinzler"})


def test_announce_spawn_default_true() -> None:
    cfg = _ws()
    assert cfg.announce_spawn is True


# --- single-file loader ------------------------------------------------------


def _write_ws_yaml(path: Path, slug: str = "acme") -> None:
    path.write_text(
        "workspace_slug: " + slug + "\n"
        "plane_base_url: https://plane.test\n"
        "plane_api_key: k\n"
        f"project_id: {PROJECT}\n"
        f"initiator_uuid: {INITIATOR}\n"
        "webhook_secret: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n"
        "email_domain: x.io\n"
        "prompts_dir: /tmp/prompts\n"
        "agents:\n"
        "  - { nickname: dev, prompt_role: developer }\n"
        "announce_spawn: false\n",
        encoding="utf-8",
    )


def test_load_workspace_from_yaml(tmp_path: Path) -> None:
    p = tmp_path / "ws.yaml"
    _write_ws_yaml(p)
    cfg = load_workspace(p)
    assert cfg.workspace_slug == "acme"
    assert cfg.agents[0].nickname == "dev"
    assert cfg.announce_spawn is False


def test_load_workspace_rejects_non_mapping(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="top-level must be a mapping"):
        load_workspace(p)


def test_load_workspace_empty_file_rejected(tmp_path: Path) -> None:
    p = tmp_path / "empty.yaml"
    p.write_text("", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_workspace(p)


# --- directory loader --------------------------------------------------------


def test_load_workspaces_picks_up_yaml_files(tmp_path: Path) -> None:
    d = tmp_path / "conductor.d"
    d.mkdir()
    _write_ws_yaml(d / "acme.yaml", "acme")
    _write_ws_yaml(d / "other.yml", "other")
    workspaces = load_workspaces(d)
    assert set(workspaces) == {"acme", "other"}


def test_load_workspaces_rejects_filename_slug_mismatch(tmp_path: Path) -> None:
    d = tmp_path / "conductor.d"
    d.mkdir()
    _write_ws_yaml(d / "qsale.yaml", "acme")  # filename != slug
    with pytest.raises(ValueError, match="does not match filename stem"):
        load_workspaces(d)


def test_load_workspaces_rejects_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_workspaces(tmp_path / "no-such")


def test_load_workspaces_rejects_empty_dir(tmp_path: Path) -> None:
    d = tmp_path / "conductor.d"
    d.mkdir()
    with pytest.raises(FileNotFoundError, match=r"no \*.yaml workspace configs"):
        load_workspaces(d)


def test_load_workspaces_skips_non_yaml(tmp_path: Path) -> None:
    d = tmp_path / "conductor.d"
    d.mkdir()
    _write_ws_yaml(d / "acme.yaml", "acme")
    (d / "README.md").write_text("docs", encoding="utf-8")
    workspaces = load_workspaces(d)
    assert set(workspaces) == {"acme"}


# --- example yamls in the repo must always parse -----------------------------


def test_example_sdlc_yaml_rejects_placeholder_secret() -> None:
    """Examples ship with a placeholder webhook_secret that MUST be rejected
    so an unedited install never starts up with a public HMAC secret."""
    repo = Path(__file__).resolve().parents[1]
    with pytest.raises(ValidationError, match="placeholder"):
        load_workspace(repo / "examples" / "conductor.d" / "sdlc.yaml")


def test_example_minimal_yaml_rejects_placeholder_secret() -> None:
    repo = Path(__file__).resolve().parents[1]
    with pytest.raises(ValidationError, match="placeholder"):
        load_workspace(repo / "examples" / "conductor.d" / "minimal.yaml")


def test_example_content_yaml_rejects_placeholder_secret() -> None:
    repo = Path(__file__).resolve().parents[1]
    with pytest.raises(ValidationError, match="placeholder"):
        load_workspace(repo / "examples" / "conductor.d" / "content.yaml")


def test_example_yaml_structure_is_otherwise_valid() -> None:
    """Verify the rest of the example YAML is syntactically and structurally
    valid (everything except the placeholder secret) by patching the secret
    in-memory and re-validating."""
    import yaml as _yaml

    repo = Path(__file__).resolve().parents[1]
    raw = _yaml.safe_load(
        (repo / "examples" / "conductor.d" / "sdlc.yaml").read_text(encoding="utf-8")
    )
    raw["webhook_secret"] = "x" * 64
    cfg = WorkspaceConfig.model_validate(raw)
    assert cfg.workspace_slug == "sdlc"
    assert len(cfg.agents) == 10
    assert cfg.states == [
        StateDef(name="Review", group="started", color="#f59e0b"),
        StateDef(name="Blocked", group="unstarted", color="#ef4444"),
    ]


# --- security validators -----------------------------------------------------


def test_webhook_secret_rejects_placeholder() -> None:
    with pytest.raises(ValidationError, match="placeholder"):
        _ws(webhook_secret="replace-me-with-openssl-rand-hex-32")


def test_webhook_secret_rejects_too_short() -> None:
    with pytest.raises(ValidationError, match="at least 32"):
        _ws(webhook_secret="short")


def test_webhook_secret_accepts_32_chars() -> None:
    cfg = _ws(webhook_secret="x" * 32)
    assert cfg.webhook_secret == "x" * 32


@pytest.mark.parametrize(
    "bad_slug",
    [
        "Has Space",
        "with/slash",
        "{templated}",
        "with:colon",
        "-leading-dash",
        "",
        "a" * 64,  # too long
        "trailing.",
        "uppercase.allowed.through.lowercase.but.dot.is.bad",
    ],
)
def test_workspace_slug_rejects_unsafe_characters(bad_slug: str) -> None:
    with pytest.raises(ValidationError, match="workspace_slug"):
        _ws(slug=bad_slug)


@pytest.mark.parametrize("good_slug", ["acme", "acme-prod", "qsale", "ws1", "0-1-2", "a"])
def test_workspace_slug_accepts_safe_characters(good_slug: str) -> None:
    cfg = _ws(slug=good_slug)
    assert cfg.workspace_slug == good_slug


def test_webhook_secret_rejects_zero_hex() -> None:
    with pytest.raises(ValidationError, match="monorepeat"):
        _ws(webhook_secret="0" * 64)


def test_webhook_secret_rejects_all_f_hex() -> None:
    with pytest.raises(ValidationError, match="monorepeat"):
        _ws(webhook_secret="f" * 32)


def test_agent_nickname_strips_surrounding_whitespace() -> None:
    a = AgentDef(nickname="  Sark  ", prompt_role="x")
    assert a.nickname == "sark"


def test_agent_nickname_rejects_internal_whitespace() -> None:
    with pytest.raises(ValidationError, match="single token"):
        AgentDef(nickname="dev ops", prompt_role="x")


def test_agent_nickname_rejects_empty_after_strip() -> None:
    with pytest.raises(ValidationError, match="single token"):
        AgentDef(nickname="   ", prompt_role="x")
