# mypy: disable-error-code="arg-type, dict-item, unused-ignore, attr-defined"
"""Webhook receiver — thin and durable-first (QSALE-55 ADR-6).

The receiver never calls Plane and never spawns: it persists one `triggers` row per
(comment, addressee) and wakes the Dispatcher. Member lookup, allow-list, auto-resume and
spawning are the Dispatcher's (see test_dispatcher.py and the auto-resume tests below).
"""

from __future__ import annotations

import hmac
import json
import sqlite3
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.config import Settings
from plane_conductor.members import extract_mention_uuids
from plane_conductor.store import Store
from plane_conductor.webhook import build_router, verify_signature

SARK = "11111111-1111-1111-1111-111111111111"
RINZLER = "22222222-2222-2222-2222-222222222222"
ISSUE_ID = "33333333-3333-3333-3333-333333333333"


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()


class Rig:
    def __init__(self, tmp: Path, settings: Settings, workspace: WorkspaceConfig) -> None:
        self.ws = workspace
        self.store = Store(tmp / "wh" / "conductor.db")
        self.kicks = 0
        app = FastAPI()
        app.include_router(
            build_router(
                settings,
                {workspace.workspace_slug: (workspace, object())},  # type: ignore[dict-item]
                self.store,
                self._kick,
            )
        )
        self.client = TestClient(app)

    def _kick(self) -> None:
        self.kicks += 1

    def send(self, payload: dict[str, Any]) -> Any:
        body = json.dumps(payload).encode()
        return self.client.post(
            f"/{self.ws.workspace_slug}/webhook",
            content=body,
            headers={
                self.ws.webhook_signature_header: _sign(
                    self.ws.webhook_secret.get_secret_value(), body
                )
            },
        )

    def triggers(self) -> list[sqlite3.Row]:
        return self.store.all("SELECT * FROM triggers ORDER BY id")


def _mention(uuid: str) -> str:
    return f'<mention-component entity_identifier="{uuid}" entity_name="user_mention"></mention-component>'


def _comment(
    *mentions: str,
    cid: str | None = "cm-1",
    action: str = "created",
    actor: str = "99999999-9999-9999-9999-9999999999aa",
    text: str = "",
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "issue": ISSUE_ID,
        "actor": actor,
        "comment_html": "".join(_mention(m) for m in mentions) + text,
    }
    if cid:
        data["id"] = cid
    return {"event": "issue_comment", "action": action, "data": data}


@pytest.fixture
def rig(tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig) -> Rig:
    return Rig(tmp_path, settings, workspace_config)


# --- signature / routing ----------------------------------------------------


def test_verify_signature(webhook_secret: str) -> None:
    body = b'{"x":1}'
    sig = _sign(webhook_secret, body)
    assert verify_signature(webhook_secret, body, sig) is True
    assert verify_signature(webhook_secret, body, "sha256=" + sig) is True
    assert verify_signature(webhook_secret, body, sig.upper()) is True
    assert verify_signature(webhook_secret, body, None) is False
    assert verify_signature(webhook_secret, body, "deadbeef") is False


def test_rejects_bad_signature(rig: Rig) -> None:
    resp = rig.client.post(
        "/testws/webhook", content=b"{}", headers={rig.ws.webhook_signature_header: "deadbeef"}
    )
    assert resp.status_code == 401 and rig.triggers() == []


def test_unknown_workspace_slug_404(rig: Rig) -> None:
    assert rig.client.post("/no-such/webhook", content=b"{}").status_code == 404


def test_rejects_invalid_json(rig: Rig) -> None:
    body = b"not-json"
    resp = rig.client.post(
        "/testws/webhook",
        content=body,
        headers={
            rig.ws.webhook_signature_header: _sign(rig.ws.webhook_secret.get_secret_value(), body)
        },
    )
    assert resp.status_code == 400


def test_ignores_non_comment_event(rig: Rig) -> None:
    resp = rig.send({"event": "issue", "action": "created", "data": {}})
    assert resp.status_code == 200 and resp.json()["ignored"] == "issue"


def test_ignores_missing_or_bad_issue_and_missing_comment_id(rig: Rig) -> None:
    assert (
        rig.send({"event": "issue_comment", "action": "created", "data": {}}).json()["ignored"]
        == "no issue id"
    )
    bad = {"event": "issue_comment", "action": "created", "data": {"issue": "nope"}}
    assert rig.send(bad).json()["ignored"] == "bad issue uuid"
    assert rig.send(_comment(SARK, cid=None)).json()["ignored"] == "no comment id"
    assert rig.triggers() == []


def test_each_workspace_has_its_own_secret(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    ws_a = workspace_config.model_copy(
        update={"workspace_slug": "alpha", "webhook_secret": SecretStr("secret-alpha")}
    )
    ws_b = workspace_config.model_copy(
        update={"workspace_slug": "beta", "webhook_secret": SecretStr("secret-beta")}
    )
    app = FastAPI()
    app.include_router(
        build_router(
            settings,
            {"alpha": (ws_a, object()), "beta": (ws_b, object())},  # type: ignore[dict-item]
            Store(tmp_path / "x" / "c.db"),
            lambda: None,
        )
    )
    client = TestClient(app)
    body = b'{"event":"x"}'
    sig_a, sig_b = (
        _sign(ws_a.webhook_secret.get_secret_value(), body),
        _sign(ws_b.webhook_secret.get_secret_value(), body),
    )
    assert (
        client.post(
            "/alpha/webhook", content=body, headers={ws_a.webhook_signature_header: sig_a}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/beta/webhook", content=body, headers={ws_b.webhook_signature_header: sig_a}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/beta/webhook", content=body, headers={ws_b.webhook_signature_header: sig_b}
        ).status_code
        == 200
    )


# --- accept: durable rows ------------------------------------------------------


def test_mention_is_persisted_and_dispatcher_woken(rig: Rig) -> None:
    resp = rig.send(_comment(SARK))
    assert resp.status_code == 200 and resp.json()["accepted"] == [SARK]
    (t,) = rig.triggers()
    assert t["state"] == "received" and t["comment_id"] == "cm-1" and t["member_uuid"] == SARK
    assert t["issue_id"] == ISSUE_ID and t["workspace"] == "testws" and t["kind"] == "webhook"
    assert rig.kicks == 1


def test_no_plane_call_on_the_request_path(rig: Rig) -> None:
    """The route was built with a bare `object()` as Plane client: any call would raise."""
    assert rig.send(_comment(SARK, RINZLER)).status_code == 200
    assert len(rig.triggers()) == 2


def test_initiator_mention_is_skipped(rig: Rig, initiator_uuid: UUID) -> None:
    resp = rig.send(_comment(str(initiator_uuid)))
    assert resp.status_code == 200 and resp.json()["accepted"] == []
    assert rig.triggers() == [] and rig.kicks == 0


def test_alert_comment_starting_with_initiator_mention_creates_no_trigger(
    rig: Rig, initiator_uuid: UUID
) -> None:
    resp = rig.send(_comment(str(initiator_uuid), text="<p>conductor-alert:abc</p>"))
    assert resp.json()["accepted"] == []


def test_redelivered_webhook_creates_no_second_trigger(rig: Rig) -> None:
    """Plane's X-Plane-Delivery differs per attempt; the comment id + addressee is the key."""
    assert rig.send(_comment(SARK)).json()["accepted"] == [SARK]
    again = rig.send(_comment(SARK))
    assert again.json()["accepted"] == [] and again.json()["skipped"][0]["reason"] == "duplicate"
    assert len(rig.triggers()) == 1


def test_edit_without_new_addressee_creates_nothing_edit_adding_one_creates_one(rig: Rig) -> None:
    rig.send(_comment(SARK))
    assert rig.send(_comment(SARK, action="updated")).json()["accepted"] == []
    assert rig.send(_comment(SARK, RINZLER, action="updated")).json()["accepted"] == [RINZLER]
    assert len(rig.triggers()) == 2


def test_state_write_failure_answers_503(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    rig.store.conn.execute("DROP TABLE triggers")
    resp = rig.send(_comment(SARK))
    assert resp.status_code == 503


def test_read_only_state_dir_answers_503(rig: Rig) -> None:
    rig.store.conn.execute("PRAGMA query_only=ON")
    assert rig.send(_comment(SARK)).status_code == 503


@pytest.mark.parametrize("event_kind", ["issue_comment", "comment"])
def test_accepts_alias_event_names(rig: Rig, event_kind: str) -> None:
    payload = _comment(SARK)
    payload["event"] = event_kind
    assert rig.send(payload).status_code == 200


# --- auto-resume: initiator reply without mention ---------------------------------------


def test_initiator_reply_without_mention_becomes_an_auto_resume_row(
    rig: Rig, initiator_uuid: UUID
) -> None:
    resp = rig.send(_comment(actor=str(initiator_uuid), text="<p>ответы выше, продолжай</p>"))
    assert resp.status_code == 200
    (t,) = rig.triggers()
    assert t["kind"] == "auto_resume" and t["member_uuid"] is None
    snap = json.loads(t["snapshot"])
    assert snap["actor"] == str(initiator_uuid).lower() and snap["had_mention"] is False


def test_auto_resume_edit_or_redelivery_does_not_repeat(rig: Rig, initiator_uuid: UUID) -> None:
    payload = _comment(actor=str(initiator_uuid), text="<p>go</p>")
    rig.send(payload)
    payload["action"] = "updated"
    rig.send(payload)
    assert len(rig.triggers()) == 1


def test_comment_by_someone_else_without_mention_is_ignored(rig: Rig) -> None:
    rig.send(_comment(text="<p>just a comment</p>"))
    assert rig.triggers() == []


# --- mention extraction ------------------------------------------------------------------------


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
    ["", "<p>plain</p>", '<mention-component entity_identifier="not-a-uuid"></mention-component>'],
)
def test_extract_mention_uuids_robust(html: str) -> None:
    assert extract_mention_uuids(html) == []
