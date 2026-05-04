from __future__ import annotations

import hmac
import json
from hashlib import sha256
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from plane_conductor.conductor_config import ConductorConfig
from plane_conductor.config import Settings
from plane_conductor.webhook import build_router, extract_mention_uuids, verify_signature

SARK = "11111111-1111-1111-1111-111111111111"
RINZLER = "22222222-2222-2222-2222-222222222222"


class StubPlane:
    def __init__(self, members: dict[str, dict[str, Any]] | None = None) -> None:
        self.members = members or {}

    async def get_member(self, member_id: str) -> dict[str, Any]:
        key = str(member_id).lower()
        if key not in self.members:
            from plane_conductor.exceptions import PlaneAPIError

            raise PlaneAPIError(404, f"missing {member_id}")
        return self.members[key]


class StubRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def spawn(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()


def _app(settings: Settings, config: ConductorConfig, plane: Any, runner: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(build_router(settings, config, plane, runner))
    return app


def _send(client: TestClient, settings: Settings, body: bytes) -> Any:
    return client.post(
        "/webhook",
        content=body,
        headers={settings.webhook_signature_header: _sign(settings.webhook_secret, body)},
    )


# --- signature ---------------------------------------------------------------


def test_verify_signature(webhook_secret: str) -> None:
    body = b'{"x":1}'
    sig = _sign(webhook_secret, body)
    assert verify_signature(webhook_secret, body, sig) is True
    assert verify_signature(webhook_secret, body, "sha256=" + sig) is True
    assert verify_signature(webhook_secret, body, sig.upper()) is True
    assert verify_signature(webhook_secret, body, None) is False
    assert verify_signature(webhook_secret, body, "deadbeef") is False


def test_webhook_rejects_bad_signature(
    settings: Settings, conductor_config: ConductorConfig
) -> None:
    client = TestClient(_app(settings, conductor_config, StubPlane(), StubRunner()))
    resp = client.post(
        "/webhook",
        content=b"{}",
        headers={settings.webhook_signature_header: "deadbeef"},
    )
    assert resp.status_code == 401


def test_webhook_rejects_invalid_json(
    settings: Settings, conductor_config: ConductorConfig
) -> None:
    client = TestClient(_app(settings, conductor_config, StubPlane(), StubRunner()))
    resp = _send(client, settings, b"not-json")
    assert resp.status_code == 400


# --- routing -----------------------------------------------------------------


def test_webhook_ignores_non_comment_event(
    settings: Settings, conductor_config: ConductorConfig
) -> None:
    client = TestClient(_app(settings, conductor_config, StubPlane(), StubRunner()))
    body = json.dumps({"event": "issue", "action": "created", "data": {}}).encode()
    resp = _send(client, settings, body)
    assert resp.status_code == 200
    assert resp.json()["ignored"] == "issue"


def test_webhook_spawns_for_known_mention(
    settings: Settings, conductor_config: ConductorConfig, project_uuid: UUID, initiator_uuid: UUID
) -> None:
    plane = StubPlane({SARK: {"email": "sark@example.io"}})
    runner = StubRunner()
    client = TestClient(_app(settings, conductor_config, plane, runner))

    body = json.dumps(
        {
            "event": "issue_comment",
            "action": "created",
            "data": {
                "issue": "33333333-3333-3333-3333-333333333333",
                "project": str(project_uuid),
                "actor": str(initiator_uuid),
                "comment_html": (
                    f'<mention-component entity_identifier="{SARK}" '
                    f'entity_name="user_mention"></mention-component>'
                ),
            },
        }
    ).encode()
    resp = _send(client, settings, body)
    assert resp.status_code == 200
    assert resp.json()["spawned"] == ["sark"]
    assert len(runner.calls) == 1
    assert runner.calls[0]["nickname"] == "sark"
    assert runner.calls[0]["triggered_by_email"] == "sark@example.io"


def test_webhook_skips_initiator(
    settings: Settings, conductor_config: ConductorConfig, project_uuid: UUID, initiator_uuid: UUID
) -> None:
    runner = StubRunner()
    client = TestClient(_app(settings, conductor_config, StubPlane(), runner))
    body = json.dumps(
        {
            "event": "issue_comment",
            "action": "created",
            "data": {
                "issue": "33333333-3333-3333-3333-333333333333",
                "comment_html": (
                    f'<mention-component entity_identifier="{initiator_uuid}" '
                    f'entity_name="user_mention"></mention-component>'
                ),
            },
        }
    ).encode()
    resp = _send(client, settings, body)
    assert resp.status_code == 200
    assert resp.json()["spawned"] == []
    assert runner.calls == []


def test_webhook_handles_multiple_mentions_with_unknowns(
    settings: Settings, conductor_config: ConductorConfig, initiator_uuid: UUID
) -> None:
    rando = "99999999-9999-9999-9999-999999999999"
    plane = StubPlane(
        {
            SARK: {"email": "sark@example.io"},
            RINZLER: {"member": {"email": "rinzler@example.io"}},
            rando: {"email": "stranger@elsewhere.io"},
        }
    )
    runner = StubRunner()
    client = TestClient(_app(settings, conductor_config, plane, runner))
    body = json.dumps(
        {
            "event": "issue_comment",
            "action": "created",
            "data": {
                "issue": "33333333-3333-3333-3333-333333333333",
                "comment_html": (
                    f'<mention-component entity_identifier="{initiator_uuid}" '
                    f'entity_name="user_mention"></mention-component>'
                    f'<mention-component entity_identifier="{SARK}" '
                    f'entity_name="user_mention"></mention-component>'
                    f'<mention-component entity_identifier="{rando}" '
                    f'entity_name="user_mention"></mention-component>'
                    f'<mention-component entity_identifier="{RINZLER}" '
                    f'entity_name="user_mention"></mention-component>'
                ),
            },
        }
    ).encode()
    resp = _send(client, settings, body)
    assert resp.status_code == 200
    body_json = resp.json()
    assert sorted(body_json["spawned"]) == ["rinzler", "sark"]
    # `rando` is skipped because their nickname isn't in the roster.
    assert any(s.get("reason") == "not allowed" for s in body_json["skipped"])


def test_webhook_respects_allowlist(
    settings: Settings, conductor_config: ConductorConfig, project_uuid: UUID, initiator_uuid: UUID
) -> None:
    settings.allowed_nicknames = "sark"
    plane = StubPlane(
        {
            SARK: {"email": "sark@example.io"},
            RINZLER: {"email": "rinzler@example.io"},
        }
    )
    runner = StubRunner()
    client = TestClient(_app(settings, conductor_config, plane, runner))
    body = json.dumps(
        {
            "event": "issue_comment",
            "action": "created",
            "data": {
                "issue": "33333333-3333-3333-3333-333333333333",
                "comment_html": (
                    f'<mention-component entity_identifier="{SARK}"></mention-component>'
                    f'<mention-component entity_identifier="{RINZLER}"></mention-component>'
                ),
            },
        }
    ).encode()
    resp = _send(client, settings, body)
    assert resp.json()["spawned"] == ["sark"]


@pytest.mark.parametrize("event_kind", ["issue_comment", "comment"])
def test_webhook_accepts_alias_event_names(
    settings: Settings, conductor_config: ConductorConfig, project_uuid: UUID, event_kind: str
) -> None:
    runner = StubRunner()
    client = TestClient(_app(settings, conductor_config, StubPlane(), runner))
    body = json.dumps(
        {
            "event": event_kind,
            "action": "created",
            "data": {
                "issue": "33333333-3333-3333-3333-333333333333",
                "comment_html": "<p>no mentions</p>",
            },
        }
    ).encode()
    resp = _send(client, settings, body)
    assert resp.status_code == 200


# --- mention extraction ------------------------------------------------------


def test_extract_mention_uuids_finds_unique_in_order() -> None:
    html = (
        f'<mention-component entity_identifier="{SARK}"></mention-component>'
        f'<mention-component entity_name="user_mention" entity_identifier="{RINZLER}">'
        f"</mention-component>"
        f'<mention-component entity_identifier="{SARK}"></mention-component>'
    )
    assert extract_mention_uuids(html) == [UUID(SARK), UUID(RINZLER)]


@pytest.mark.parametrize(
    "html",
    [
        "",
        "<p>plain</p>",
        '<mention-component entity_identifier="not-a-uuid"></mention-component>',
    ],
)
def test_extract_mention_uuids_robust(html: str) -> None:
    assert extract_mention_uuids(html) == []


# --- payload edge cases ------------------------------------------------------


def test_webhook_ignores_missing_issue_id(
    settings: Settings, conductor_config: ConductorConfig
) -> None:
    client = TestClient(_app(settings, conductor_config, StubPlane(), StubRunner()))
    body = json.dumps(
        {"event": "issue_comment", "action": "created", "data": {"comment_html": ""}}
    ).encode()
    resp = _send(client, settings, body)
    assert resp.status_code == 200
    assert resp.json()["ignored"] == "no issue id"


def test_webhook_ignores_bad_issue_uuid(
    settings: Settings, conductor_config: ConductorConfig
) -> None:
    client = TestClient(_app(settings, conductor_config, StubPlane(), StubRunner()))
    body = json.dumps(
        {
            "event": "issue_comment",
            "action": "created",
            "data": {"issue": "not-a-uuid", "comment_html": ""},
        }
    ).encode()
    resp = _send(client, settings, body)
    assert resp.status_code == 200
    assert resp.json()["ignored"] == "bad issue uuid"


def test_webhook_returns_empty_when_no_mentions(
    settings: Settings, conductor_config: ConductorConfig
) -> None:
    client = TestClient(_app(settings, conductor_config, StubPlane(), StubRunner()))
    body = json.dumps(
        {
            "event": "issue_comment",
            "action": "created",
            "data": {
                "issue": "33333333-3333-3333-3333-333333333333",
                "comment_html": "<p>just a regular comment</p>",
            },
        }
    ).encode()
    resp = _send(client, settings, body)
    assert resp.status_code == 200
    assert resp.json()["spawned"] == []


def test_webhook_skipped_when_member_lookup_fails(
    settings: Settings, conductor_config: ConductorConfig, project_uuid: UUID
) -> None:
    # Empty StubPlane → every lookup raises 404.
    client = TestClient(_app(settings, conductor_config, StubPlane(), StubRunner()))
    body = json.dumps(
        {
            "event": "issue_comment",
            "action": "created",
            "data": {
                "issue": "33333333-3333-3333-3333-333333333333",
                "comment_html": (
                    f'<mention-component entity_identifier="{SARK}"></mention-component>'
                ),
            },
        }
    ).encode()
    resp = _send(client, settings, body)
    assert resp.status_code == 200
    body_json = resp.json()
    assert body_json["spawned"] == []
    assert body_json["skipped"] == [{"member_id": str(UUID(SARK)), "reason": "lookup failed"}]


def test_webhook_skipped_when_member_has_no_email(
    settings: Settings, conductor_config: ConductorConfig
) -> None:
    plane = StubPlane({SARK: {"display_name": "Sark"}})  # no email
    client = TestClient(_app(settings, conductor_config, plane, StubRunner()))
    body = json.dumps(
        {
            "event": "issue_comment",
            "action": "created",
            "data": {
                "issue": "33333333-3333-3333-3333-333333333333",
                "comment_html": (
                    f'<mention-component entity_identifier="{SARK}"></mention-component>'
                ),
            },
        }
    ).encode()
    resp = _send(client, settings, body)
    assert resp.json()["skipped"][0]["reason"] == "no email"


def test_webhook_handles_spawn_failure(
    settings: Settings, conductor_config: ConductorConfig
) -> None:
    """When runner raises AgentSpawnError, webhook records skip but returns 200."""
    from plane_conductor.exceptions import AgentSpawnError

    class CrashingRunner:
        async def spawn(self, **kwargs: Any) -> None:
            raise AgentSpawnError("boom")

    plane = StubPlane({SARK: {"email": "sark@example.io"}})
    client = TestClient(_app(settings, conductor_config, plane, CrashingRunner()))
    body = json.dumps(
        {
            "event": "issue_comment",
            "action": "created",
            "data": {
                "issue": "33333333-3333-3333-3333-333333333333",
                "comment_html": (
                    f'<mention-component entity_identifier="{SARK}"></mention-component>'
                ),
            },
        }
    ).encode()
    resp = _send(client, settings, body)
    assert resp.status_code == 200
    body_json = resp.json()
    assert body_json["spawned"] == []
    assert body_json["skipped"] == [{"nickname": "sark", "reason": "spawn failed"}]


def _comment_body(mention_uuid: str) -> bytes:
    return json.dumps(
        {
            "event": "issue_comment",
            "action": "created",
            "data": {
                "issue": "33333333-3333-3333-3333-333333333333",
                "comment_html": (
                    f'<mention-component entity_identifier="{mention_uuid}"></mention-component>'
                ),
            },
        }
    ).encode()


def test_webhook_skipped_on_dedup(settings: Settings, conductor_config: ConductorConfig) -> None:
    from plane_conductor.exceptions import SessionAlreadyRunningError

    class DupRunner:
        async def spawn(self, **kwargs: Any) -> None:
            raise SessionAlreadyRunningError("already running")

    plane = StubPlane({SARK: {"email": "sark@example.io"}})
    client = TestClient(_app(settings, conductor_config, plane, DupRunner()))
    resp = _send(client, settings, _comment_body(SARK))
    assert resp.status_code == 200
    assert resp.json()["skipped"] == [{"nickname": "sark", "reason": "already running"}]


def test_webhook_skipped_on_capacity(settings: Settings, conductor_config: ConductorConfig) -> None:
    from plane_conductor.exceptions import CapacityFullError

    class FullRunner:
        async def spawn(self, **kwargs: Any) -> None:
            raise CapacityFullError("full")

    plane = StubPlane({SARK: {"email": "sark@example.io"}})
    client = TestClient(_app(settings, conductor_config, plane, FullRunner()))
    resp = _send(client, settings, _comment_body(SARK))
    assert resp.status_code == 200
    assert resp.json()["skipped"] == [{"nickname": "sark", "reason": "capacity full"}]


def test_webhook_returns_503_on_transient_plane_error(
    settings: Settings, conductor_config: ConductorConfig
) -> None:
    """5xx / transport errors during member lookup → 503 so Plane retries."""
    from plane_conductor.exceptions import PlaneAPIError

    class FlakyPlane:
        async def get_member(self, member_id: str) -> dict[str, Any]:
            raise PlaneAPIError(503, "service unavailable")

    client = TestClient(_app(settings, conductor_config, FlakyPlane(), StubRunner()))
    resp = _send(client, settings, _comment_body(SARK))
    assert resp.status_code == 503


def test_webhook_skipped_on_4xx_plane_error(
    settings: Settings, conductor_config: ConductorConfig
) -> None:
    """4xx errors during member lookup are not transient — skip, return 200."""
    from plane_conductor.exceptions import PlaneAPIError

    class NotFoundPlane:
        async def get_member(self, member_id: str) -> dict[str, Any]:
            raise PlaneAPIError(404, "not found")

    client = TestClient(_app(settings, conductor_config, NotFoundPlane(), StubRunner()))
    resp = _send(client, settings, _comment_body(SARK))
    assert resp.status_code == 200
    assert resp.json()["skipped"][0]["reason"] == "lookup failed"
