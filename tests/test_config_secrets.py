"""Credentials in workspace YAML: never echoed by errors, readable from files.

The service is started from a unit whose journal is read by people and agents,
so a malformed config must not print the API key or the webhook secret. The
`*_file` form lets the host lay the values out in a runtime directory (from a
vault) and keep the YAML itself value-free.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from plane_conductor.conductor_config import (
    WorkspaceConfig,
    load_workspace,
    load_workspaces,
)
from plane_conductor.config import Settings
from plane_conductor.server import create_app

API_KEY = "plane_api_SENTINEL_KEY_0123456789"
SECRET = "SENTINEL_SECRET_" + "a1b2c3d4" * 4

BASE_YAML = """\
workspace_slug: acme
plane_base_url: https://plane.test
project_id: 00000000-0000-0000-0000-000000000001
initiator_uuid: 00000000-0000-0000-0000-000000000099
email_domain: x.io
prompts_dir: /tmp/prompts
agents:
  - nickname: dev
    prompt_role: developer
"""


def _base(**overrides: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "workspace_slug": "acme",
        "plane_base_url": "https://plane.test",
        "project_id": "00000000-0000-0000-0000-000000000001",
        "initiator_uuid": "00000000-0000-0000-0000-000000000099",
        "email_domain": "x.io",
        "prompts_dir": "/tmp/prompts",
        "agents": [{"nickname": "dev", "prompt_role": "developer"}],
        "plane_api_key": API_KEY,
        "webhook_secret": SECRET,
    }
    raw.update(overrides)
    return raw


def _write_secrets(tmp_path: Path) -> tuple[Path, Path]:
    key = tmp_path / "acme.plane_api_key"
    sec = tmp_path / "acme.webhook_secret"
    key.write_text(API_KEY + "\n")
    sec.write_text(SECRET + "\n")
    return key, sec


# --- values never appear in errors -----------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"plane_base_url": 42},  # wrong type of an unrelated field
        {"agents": "dev"},  # wrong shape of a nested list
        {"agents": [{"nickname": "dev", "prompt_role": "developer", "x": 1}]},  # nested extra
        {"unknown_key": API_KEY},  # extra key whose value is the secret
        {"plane_api_key": [API_KEY]},  # wrong type of the secret itself
        {"webhook_secret": SECRET[:20]},  # too short — validator error
    ],
)
def test_validation_error_does_not_print_values(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as exc:
        WorkspaceConfig.model_validate(_base(**overrides))
    # str() is what `serve` prints; `errors()` keeps the input by design and
    # is not printed anywhere.
    text = str(exc.value) + repr(exc.value)
    assert API_KEY not in text
    assert SECRET not in text
    assert SECRET[:20] not in text


def test_yaml_syntax_error_does_not_print_the_line(tmp_path: Path) -> None:
    p = tmp_path / "acme.yaml"
    # Unbalanced quote on the secret line: PyYAML's message quotes the buffer.
    p.write_text(BASE_YAML + f'plane_api_key: "{API_KEY}\nwebhook_secret: {SECRET}\n  : [\n')
    with pytest.raises(ValueError) as exc:
        load_workspace(p)
    text = str(exc.value) + repr(exc.value)
    assert API_KEY not in text
    assert SECRET not in text
    assert str(p) in text
    assert exc.value.__cause__ is None  # the YAML error, with its snippet, is not chained


def test_loaded_config_repr_masks_values(tmp_path: Path) -> None:
    ws = WorkspaceConfig.model_validate(_base())
    assert API_KEY not in repr(ws) and SECRET not in repr(ws)
    assert ws.plane_api_key.get_secret_value() == API_KEY
    assert ws.webhook_secret.get_secret_value() == SECRET


# --- *_file form ------------------------------------------------------------


def test_file_form_reads_values_and_strips_newline(tmp_path: Path) -> None:
    key, sec = _write_secrets(tmp_path)
    raw = _base(plane_api_key_file=str(key), webhook_secret_file=str(sec))
    del raw["plane_api_key"], raw["webhook_secret"]
    ws = WorkspaceConfig.model_validate(raw)
    assert ws.plane_api_key.get_secret_value() == API_KEY
    assert ws.webhook_secret.get_secret_value() == SECRET
    assert ws.credential_sources() == {"plane_api_key": "file", "webhook_secret": "file"}


def test_inline_form_reports_inline_source() -> None:
    ws = WorkspaceConfig.model_validate(_base())
    assert ws.credential_sources() == {"plane_api_key": "inline", "webhook_secret": "inline"}


def test_both_forms_rejected(tmp_path: Path) -> None:
    key, _ = _write_secrets(tmp_path)
    with pytest.raises(ValidationError, match=r"plane_api_key.*exactly one"):
        WorkspaceConfig.model_validate(_base(plane_api_key_file=str(key)))


def test_neither_form_rejected() -> None:
    raw = _base()
    del raw["webhook_secret"]
    with pytest.raises(ValidationError, match=r"webhook_secret.*exactly one"):
        WorkspaceConfig.model_validate(raw)


def test_missing_file_named_by_field_and_path(tmp_path: Path) -> None:
    raw = _base(plane_api_key_file=str(tmp_path / "nope"))
    del raw["plane_api_key"]
    with pytest.raises(ValidationError) as exc:
        WorkspaceConfig.model_validate(raw)
    assert "plane_api_key_file" in str(exc.value)
    assert str(tmp_path / "nope") in str(exc.value)


def test_empty_file_rejected(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.write_text("\n")
    raw = _base(plane_api_key_file=str(empty))
    del raw["plane_api_key"]
    with pytest.raises(ValidationError, match=r"plane_api_key_file.*empty"):
        WorkspaceConfig.model_validate(raw)


def test_file_secret_still_checked_for_strength(tmp_path: Path) -> None:
    weak = tmp_path / "weak"
    weak.write_text("0" * 64)
    raw = _base(webhook_secret_file=str(weak))
    del raw["webhook_secret"]
    with pytest.raises(ValidationError, match="monorepeat"):
        WorkspaceConfig.model_validate(raw)


def test_relative_file_path_resolved_against_yaml_dir(tmp_path: Path) -> None:
    d = tmp_path / "conductor.d"
    d.mkdir()
    (tmp_path / "secrets").mkdir()
    _write_secrets(tmp_path / "secrets")
    (d / "acme.yaml").write_text(
        BASE_YAML
        + "plane_api_key_file: ../secrets/acme.plane_api_key\n"
        + "webhook_secret_file: ../secrets/acme.webhook_secret\n"
    )
    ws = load_workspaces(d)["acme"]
    assert ws.plane_api_key.get_secret_value() == API_KEY


# --- /health ----------------------------------------------------------------


def test_health_reports_sources_without_values(tmp_path: Path, settings: Settings) -> None:
    key, _ = _write_secrets(tmp_path)
    raw = _base(plane_api_key_file=str(key))
    del raw["plane_api_key"]
    ws = WorkspaceConfig.model_validate(raw)
    app = create_app(settings, {"acme": ws})
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["credential_sources"] == {
        "acme": {"plane_api_key": "file", "webhook_secret": "inline"}
    }
    assert API_KEY not in str(body) and SECRET not in str(body)
