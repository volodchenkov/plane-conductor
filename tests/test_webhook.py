# mypy: disable-error-code="arg-type, dict-item"
from __future__ import annotations

import hmac
import json
from hashlib import sha256
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.config import Settings
from plane_conductor.webhook import build_router, extract_mention_uuids, verify_signature

SARK = "11111111-1111-1111-1111-111111111111"
RINZLER = "22222222-2222-2222-2222-222222222222"


class StubPlane:
    def __init__(
        self,
        members: dict[str, dict[str, Any]] | None = None,
        comments: list[dict[str, Any]] | None = None,
    ) -> None:
        self.members = members or {}
        self.comments = comments or []

    async def get_member(self, member_id: str) -> dict[str, Any]:
        key = str(member_id).lower()
        if key not in self.members:
            from plane_conductor.exceptions import PlaneAPIError

            raise PlaneAPIError(404, f"missing {member_id}")
        return self.members[key]

    async def list_issue_comments(self, project_id: Any, issue_id: Any) -> list[dict[str, Any]]:
        return list(self.comments)

    async def aclose(self) -> None:
        pass


class StubRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def spawn(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()


def _app(
    settings: Settings,
    workspace: WorkspaceConfig,
    plane: Any,
    runner: Any,
) -> FastAPI:
    app = FastAPI()
    app.include_router(
        build_router(
            settings,
            {workspace.workspace_slug: (workspace, plane)},
            runner,
        )
    )
    return app


def _url(workspace: WorkspaceConfig) -> str:
    return f"/{workspace.workspace_slug}/webhook"


def _send(client: TestClient, settings: Settings, workspace: WorkspaceConfig, body: bytes) -> Any:
    return client.post(
        _url(workspace),
        content=body,
        headers={workspace.webhook_signature_header: _sign(workspace.webhook_secret, body)},
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
    settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    client = TestClient(_app(settings, workspace_config, StubPlane(), StubRunner()))
    resp = client.post(
        _url(workspace_config),
        content=b"{}",
        headers={workspace_config.webhook_signature_header: "deadbeef"},
    )
    assert resp.status_code == 401


def test_webhook_unknown_workspace_slug_404(
    settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    client = TestClient(_app(settings, workspace_config, StubPlane(), StubRunner()))
    resp = client.post("/no-such/webhook", content=b"{}")
    assert resp.status_code == 404


def test_webhook_rejects_invalid_json(
    settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    client = TestClient(_app(settings, workspace_config, StubPlane(), StubRunner()))
    resp = _send(client, settings, workspace_config, b"not-json")
    assert resp.status_code == 400


# --- routing -----------------------------------------------------------------


def test_webhook_ignores_non_comment_event(
    settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    client = TestClient(_app(settings, workspace_config, StubPlane(), StubRunner()))
    body = json.dumps({"event": "issue", "action": "created", "data": {}}).encode()
    resp = _send(client, settings, workspace_config, body)
    assert resp.status_code == 200
    assert resp.json()["ignored"] == "issue"
    assert resp.json()["workspace"] == workspace_config.workspace_slug


def test_webhook_spawns_for_known_mention(
    settings: Settings, workspace_config: WorkspaceConfig, project_uuid: UUID, initiator_uuid: UUID
) -> None:
    plane = StubPlane({SARK: {"email": "sark@example.io"}})
    runner = StubRunner()
    client = TestClient(_app(settings, workspace_config, plane, runner))

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
    resp = _send(client, settings, workspace_config, body)
    assert resp.status_code == 200
    assert resp.json()["spawned"] == ["sark"]
    assert len(runner.calls) == 1
    assert runner.calls[0]["nickname"] == "sark"
    assert runner.calls[0]["triggered_by_email"] == "sark@example.io"
    # Runner.spawn receives the workspace + plane client too:
    assert runner.calls[0]["workspace"] is workspace_config
    assert runner.calls[0]["plane"] is plane


def test_webhook_skips_initiator(
    settings: Settings, workspace_config: WorkspaceConfig, initiator_uuid: UUID
) -> None:
    runner = StubRunner()
    client = TestClient(_app(settings, workspace_config, StubPlane(), runner))
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
    resp = _send(client, settings, workspace_config, body)
    assert resp.status_code == 200
    assert resp.json()["spawned"] == []
    assert runner.calls == []


def test_webhook_handles_multiple_mentions_with_unknowns(
    settings: Settings, workspace_config: WorkspaceConfig, initiator_uuid: UUID
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
    client = TestClient(_app(settings, workspace_config, plane, runner))
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
    resp = _send(client, settings, workspace_config, body)
    assert resp.status_code == 200
    body_json = resp.json()
    assert sorted(body_json["spawned"]) == ["rinzler", "sark"]
    assert any(s.get("reason") == "not allowed" for s in body_json["skipped"])


def test_webhook_respects_allowlist(settings: Settings, workspace_config: WorkspaceConfig) -> None:
    ws = workspace_config.model_copy(update={"allowed_nicknames": ["sark"]})
    plane = StubPlane(
        {
            SARK: {"email": "sark@example.io"},
            RINZLER: {"email": "rinzler@example.io"},
        }
    )
    runner = StubRunner()
    client = TestClient(_app(settings, ws, plane, runner))
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
    resp = client.post(
        _url(ws),
        content=body,
        headers={ws.webhook_signature_header: _sign(ws.webhook_secret, body)},
    )
    assert resp.json()["spawned"] == ["sark"]


@pytest.mark.parametrize("event_kind", ["issue_comment", "comment"])
def test_webhook_accepts_alias_event_names(
    settings: Settings, workspace_config: WorkspaceConfig, event_kind: str
) -> None:
    runner = StubRunner()
    client = TestClient(_app(settings, workspace_config, StubPlane(), runner))
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
    resp = _send(client, settings, workspace_config, body)
    assert resp.status_code == 200


# --- per-workspace HMAC isolation -------------------------------------------


def test_each_workspace_has_its_own_secret(
    settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    """Two workspaces, two secrets. A signature good for one is bad for the other."""
    ws_a = workspace_config.model_copy(
        update={"workspace_slug": "alpha", "webhook_secret": "secret-alpha"}
    )
    ws_b = workspace_config.model_copy(
        update={"workspace_slug": "beta", "webhook_secret": "secret-beta"}
    )
    app = FastAPI()
    plane = StubPlane()
    runner = StubRunner()
    app.include_router(
        build_router(
            settings,
            {
                ws_a.workspace_slug: (ws_a, plane),
                ws_b.workspace_slug: (ws_b, plane),
            },
            runner,
        )
    )
    client = TestClient(app)
    body = b'{"event":"x"}'
    sig_a = _sign(ws_a.webhook_secret, body)
    sig_b = _sign(ws_b.webhook_secret, body)

    # Right secret → 200 (event is ignored, but route accepted).
    r = client.post("/alpha/webhook", content=body, headers={ws_a.webhook_signature_header: sig_a})
    assert r.status_code == 200
    # Wrong secret (alpha's sig on beta's URL) → 401.
    r = client.post("/beta/webhook", content=body, headers={ws_b.webhook_signature_header: sig_a})
    assert r.status_code == 401
    # Right secret on beta → 200.
    r = client.post("/beta/webhook", content=body, headers={ws_b.webhook_signature_header: sig_b})
    assert r.status_code == 200


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
    settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    client = TestClient(_app(settings, workspace_config, StubPlane(), StubRunner()))
    body = json.dumps(
        {"event": "issue_comment", "action": "created", "data": {"comment_html": ""}}
    ).encode()
    resp = _send(client, settings, workspace_config, body)
    assert resp.status_code == 200
    assert resp.json()["ignored"] == "no issue id"


def test_webhook_ignores_bad_issue_uuid(
    settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    client = TestClient(_app(settings, workspace_config, StubPlane(), StubRunner()))
    body = json.dumps(
        {
            "event": "issue_comment",
            "action": "created",
            "data": {"issue": "not-a-uuid", "comment_html": ""},
        }
    ).encode()
    resp = _send(client, settings, workspace_config, body)
    assert resp.status_code == 200
    assert resp.json()["ignored"] == "bad issue uuid"


def test_webhook_returns_empty_when_no_mentions(
    settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    client = TestClient(_app(settings, workspace_config, StubPlane(), StubRunner()))
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
    resp = _send(client, settings, workspace_config, body)
    assert resp.status_code == 200
    assert resp.json()["spawned"] == []


def test_webhook_skipped_when_member_lookup_fails(
    settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    client = TestClient(_app(settings, workspace_config, StubPlane(), StubRunner()))
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
    resp = _send(client, settings, workspace_config, body)
    assert resp.status_code == 200
    body_json = resp.json()
    assert body_json["spawned"] == []
    assert body_json["skipped"] == [{"member_id": str(UUID(SARK)), "reason": "lookup failed"}]


def test_webhook_skipped_when_member_has_no_email(
    settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    plane = StubPlane({SARK: {"display_name": "Sark"}})
    client = TestClient(_app(settings, workspace_config, plane, StubRunner()))
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
    resp = _send(client, settings, workspace_config, body)
    assert resp.json()["skipped"][0]["reason"] == "no email"


def test_webhook_handles_spawn_failure(
    settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    from plane_conductor.exceptions import AgentSpawnError

    class CrashingRunner:
        async def spawn(self, **kwargs: Any) -> None:
            raise AgentSpawnError("boom")

    plane = StubPlane({SARK: {"email": "sark@example.io"}})
    client = TestClient(_app(settings, workspace_config, plane, CrashingRunner()))
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
    resp = _send(client, settings, workspace_config, body)
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


def test_webhook_skipped_on_dedup(settings: Settings, workspace_config: WorkspaceConfig) -> None:
    from plane_conductor.exceptions import SessionAlreadyRunningError

    class DupRunner:
        async def spawn(self, **kwargs: Any) -> None:
            raise SessionAlreadyRunningError("already running")

    plane = StubPlane({SARK: {"email": "sark@example.io"}})
    client = TestClient(_app(settings, workspace_config, plane, DupRunner()))
    resp = _send(client, settings, workspace_config, _comment_body(SARK))
    assert resp.status_code == 200
    assert resp.json()["skipped"] == [{"nickname": "sark", "reason": "already running"}]


def test_webhook_skipped_on_capacity(settings: Settings, workspace_config: WorkspaceConfig) -> None:
    from plane_conductor.exceptions import CapacityFullError

    class FullRunner:
        async def spawn(self, **kwargs: Any) -> None:
            raise CapacityFullError("full")

    plane = StubPlane({SARK: {"email": "sark@example.io"}})
    client = TestClient(_app(settings, workspace_config, plane, FullRunner()))
    resp = _send(client, settings, workspace_config, _comment_body(SARK))
    assert resp.status_code == 200
    assert resp.json()["skipped"] == [{"nickname": "sark", "reason": "capacity full"}]


def test_webhook_returns_503_on_transient_plane_error(
    settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    from plane_conductor.exceptions import PlaneAPIError

    class FlakyPlane:
        async def get_member(self, member_id: str) -> dict[str, Any]:
            raise PlaneAPIError(503, "service unavailable")

        async def aclose(self) -> None:
            pass

    client = TestClient(_app(settings, workspace_config, FlakyPlane(), StubRunner()))
    resp = _send(client, settings, workspace_config, _comment_body(SARK))
    assert resp.status_code == 503


def test_webhook_skipped_on_4xx_plane_error(
    settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    from plane_conductor.exceptions import PlaneAPIError

    class NotFoundPlane:
        async def get_member(self, member_id: str) -> dict[str, Any]:
            raise PlaneAPIError(404, "not found")

        async def aclose(self) -> None:
            pass

    client = TestClient(_app(settings, workspace_config, NotFoundPlane(), StubRunner()))
    resp = _send(client, settings, workspace_config, _comment_body(SARK))
    assert resp.status_code == 200
    assert resp.json()["skipped"][0]["reason"] == "lookup failed"


# --- auto-resume on initiator reply ------------------------------------------


def _initiator_reply_body(initiator_uuid: UUID, issue: str | None = None) -> bytes:
    """Plane webhook for an initiator's free-text reply (no mention component)."""
    return json.dumps(
        {
            "event": "issue_comment",
            "action": "created",
            "data": {
                "issue": issue or "44444444-4444-4444-4444-444444444444",
                "actor": str(initiator_uuid),
                "comment_html": "<p>ответы выше, продолжай</p>",
            },
        }
    ).encode()


def _agent_handoff_comment(
    actor: str, initiator_uuid: UUID, created_at: str, text: str = "PLAN ready"
) -> dict[str, Any]:
    """Comment shaped like one produced by `request_handoff(target_role='initiator', …)`.
    Initiator mention is auto-stamped by tower at the top of `comment_html`."""
    return {
        "actor": actor,
        "created_at": created_at,
        "comment_html": (
            f'<mention-component entity_identifier="{initiator_uuid}" '
            f'entity_name="user_mention"></mention-component> {text}'
        ),
    }


def test_auto_resume_respawns_agent_when_initiator_replies(
    settings: Settings, workspace_config: WorkspaceConfig, initiator_uuid: UUID
) -> None:
    plane = StubPlane(
        members={SARK: {"email": "sark@example.io"}},
        comments=[
            _agent_handoff_comment(SARK, initiator_uuid, "2026-05-18T20:43:00Z", "SPEC ready"),
        ],
    )
    runner = StubRunner()
    client = TestClient(_app(settings, workspace_config, plane, runner))

    resp = _send(client, settings, workspace_config, _initiator_reply_body(initiator_uuid))

    assert resp.status_code == 200
    body = resp.json()
    assert body["spawned"] == ["sark"]
    assert body["auto_resumed"] == SARK
    assert len(runner.calls) == 1
    assert runner.calls[0]["nickname"] == "sark"


def test_auto_resume_picks_latest_agent_when_several_have_pinged(
    settings: Settings, workspace_config: WorkspaceConfig, initiator_uuid: UUID
) -> None:
    """Multiple agents pinged the initiator at different times — the latest one
    is the one currently waiting for input, so it's the one we re-spawn."""
    plane = StubPlane(
        members={
            SARK: {"email": "sark@example.io"},
            RINZLER: {"email": "rinzler@example.io"},
        },
        comments=[
            _agent_handoff_comment(SARK, initiator_uuid, "2026-05-18T18:00:00Z"),
            _agent_handoff_comment(RINZLER, initiator_uuid, "2026-05-18T20:43:00Z"),
        ],
    )
    runner = StubRunner()
    client = TestClient(_app(settings, workspace_config, plane, runner))

    resp = _send(client, settings, workspace_config, _initiator_reply_body(initiator_uuid))

    assert resp.status_code == 200
    assert resp.json()["spawned"] == ["rinzler"]


def test_auto_resume_skips_when_no_agent_pinged_initiator(
    settings: Settings, workspace_config: WorkspaceConfig, initiator_uuid: UUID
) -> None:
    """Initiator's first-ever comment on the issue (no prior agent activity)
    must not trigger any spawn — there's no one to «resume»."""
    plane = StubPlane(members={SARK: {"email": "sark@example.io"}}, comments=[])
    runner = StubRunner()
    client = TestClient(_app(settings, workspace_config, plane, runner))

    resp = _send(client, settings, workspace_config, _initiator_reply_body(initiator_uuid))

    assert resp.status_code == 200
    body = resp.json()
    assert body["spawned"] == []
    assert "auto_resumed" not in body
    assert runner.calls == []


def test_auto_resume_skips_when_agent_comment_has_no_initiator_mention(
    settings: Settings, workspace_config: WorkspaceConfig, initiator_uuid: UUID
) -> None:
    """An agent comment without the initiator-mention auto-stamp is treated
    as «not awaiting input» — it's progress noise, not a handoff."""
    plane = StubPlane(
        members={SARK: {"email": "sark@example.io"}},
        comments=[
            {
                "actor": SARK,
                "created_at": "2026-05-18T20:43:00Z",
                "comment_html": "<p>Step 3 done. ✅ pytest green.</p>",
            }
        ],
    )
    runner = StubRunner()
    client = TestClient(_app(settings, workspace_config, plane, runner))

    resp = _send(client, settings, workspace_config, _initiator_reply_body(initiator_uuid))

    assert resp.status_code == 200
    assert resp.json()["spawned"] == []
    assert runner.calls == []


def test_auto_resume_skips_when_commenter_is_not_initiator(
    settings: Settings, workspace_config: WorkspaceConfig, initiator_uuid: UUID
) -> None:
    """A no-mention comment from someone OTHER than the initiator must not
    auto-resume — that's a teammate / lurker, not the awaited reply."""
    stranger = "99999999-9999-9999-9999-999999999999"
    plane = StubPlane(
        members={SARK: {"email": "sark@example.io"}},
        comments=[_agent_handoff_comment(SARK, initiator_uuid, "2026-05-18T20:43:00Z")],
    )
    runner = StubRunner()
    client = TestClient(_app(settings, workspace_config, plane, runner))

    body = json.dumps(
        {
            "event": "issue_comment",
            "action": "created",
            "data": {
                "issue": "44444444-4444-4444-4444-444444444444",
                "actor": stranger,
                "comment_html": "<p>by the way…</p>",
            },
        }
    ).encode()
    resp = _send(client, settings, workspace_config, body)

    assert resp.status_code == 200
    assert resp.json()["spawned"] == []
    assert runner.calls == []


def test_auto_resume_skips_unregistered_agent(
    settings: Settings, workspace_config: WorkspaceConfig, initiator_uuid: UUID
) -> None:
    """Agent member who pinged the initiator is no longer in the workspace
    roster (e.g. removed mid-flight) — fail closed, don't spawn."""
    ex_agent = "abababab-abab-abab-abab-abababababab"
    plane = StubPlane(
        members={ex_agent: {"email": "ex-agent@example.io"}},
        comments=[_agent_handoff_comment(ex_agent, initiator_uuid, "2026-05-18T20:43:00Z")],
    )
    runner = StubRunner()
    client = TestClient(_app(settings, workspace_config, plane, runner))

    resp = _send(client, settings, workspace_config, _initiator_reply_body(initiator_uuid))

    assert resp.status_code == 200
    assert resp.json()["spawned"] == []
    assert runner.calls == []
