"""Smoke tests for the FastAPI factory."""

from __future__ import annotations

import hmac
import json
import time
from hashlib import sha256
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.config import Settings
from plane_conductor.server import create_app
from tests.helpers import fake_claude, good_run, handoff_lines, invocations


def test_create_app_health_endpoint(
    settings: Settings, workspaces: dict[str, WorkspaceConfig]
) -> None:
    app = create_app(settings, workspaces)
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "version" in body
        assert body["workspaces"] == ["testws"]


def test_create_app_wires_state(settings: Settings, workspaces: dict[str, WorkspaceConfig]) -> None:
    app = create_app(settings, workspaces)
    with TestClient(app):
        assert app.state.settings is settings
        assert app.state.workspaces is workspaces
        assert app.state.runner is not None
        assert "testws" in app.state.workspaces_with_clients


def test_create_app_webhook_endpoint_present(
    settings: Settings, workspaces: dict[str, WorkspaceConfig]
) -> None:
    app = create_app(settings, workspaces)
    with TestClient(app) as client:
        # Without signature → 401, but the slug-routed endpoint exists.
        resp = client.post("/testws/webhook", content=b"{}")
        assert resp.status_code == 401

        # An unknown slug → 404 (no route mounted).
        resp = client.post("/unknown/webhook", content=b"{}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# QSALE-55: durable state, single instance, real lifespan end to end
# ---------------------------------------------------------------------------

ISSUE_ID = "33333333-3333-3333-3333-333333333333"
SARK_ID = "11111111-1111-1111-1111-111111111111"


def test_health_reports_version(settings: Settings, workspaces: dict[str, WorkspaceConfig]) -> None:
    from plane_conductor import __version__

    with TestClient(create_app(settings, workspaces)) as client:
        assert client.get("/health").json()["version"] == __version__


def test_second_conductor_on_the_same_state_dir_refuses_to_start(
    settings: Settings, workspaces: dict[str, WorkspaceConfig]
) -> None:
    first = create_app(settings, workspaces)
    with TestClient(first):
        second = create_app(settings, workspaces)
        with pytest.raises(RuntimeError, match="another conductor"), TestClient(second):
            pass


def _mock_plane(ws: WorkspaceConfig) -> None:
    base = f"{ws.plane_base_url}/api/v1/workspaces/{ws.workspace_slug}"
    members = {"results": [{"id": SARK_ID, "email": "sark@example.io"}]}
    respx.get(f"{base}/members/").mock(return_value=httpx.Response(200, json=members))
    respx.get(f"{base}/projects/{ws.project_id}/members/").mock(
        return_value=httpx.Response(200, json=members)
    )
    respx.get(f"{base}/projects/{ws.project_id}/issues/{ISSUE_ID}/").mock(
        return_value=httpx.Response(200, json={"id": ISSUE_ID, "parent": None})
    )
    respx.post(f"{base}/projects/{ws.project_id}/issues/{ISSUE_ID}/comments/").mock(
        return_value=httpx.Response(201, json={"id": "alert-1"})
    )


@respx.mock
def test_webhook_to_agent_run_to_clean_finish_through_the_real_lifespan(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    """Webhook (mention) → durable trigger → Dispatcher → fake claude with a stream → handoff
    registered from the stream → no alert; the state survives in conductor.db."""
    _mock_plane(workspace_config)
    claude = fake_claude(tmp_path, [{"lines": good_run(*handoff_lines("t1", "c-1"))}])
    s = settings.model_copy(
        update={
            "claude_binary": claude,
            "dispatcher_tick_seconds": 0.05,
            "session_timeout_seconds": 30,
        }
    )
    app = create_app(s, {workspace_config.workspace_slug: workspace_config})
    payload = {
        "event": "issue_comment",
        "action": "created",
        "data": {
            "id": "cm-1",
            "issue": ISSUE_ID,
            "actor": "99999999-9999-9999-9999-9999999999aa",
            "comment_html": (
                f'<mention-component entity_identifier="{SARK_ID}" '
                f'entity_name="user_mention"></mention-component>'
            ),
        },
    }
    body = json.dumps(payload).encode()
    sig = hmac.new(
        workspace_config.webhook_secret.get_secret_value().encode(), body, sha256
    ).hexdigest()
    with TestClient(app) as client:
        r = client.post(
            "/testws/webhook",
            content=body,
            headers={workspace_config.webhook_signature_header: sig},
        )
        assert r.status_code == 200 and r.json()["accepted"] == [SARK_ID]
        deadline = time.monotonic() + 15
        store = app.state.store
        while time.monotonic() < deadline:
            row = store.one("SELECT state FROM triggers")
            if row is not None and row["state"] == "done":
                break
            time.sleep(0.05)
        else:
            raise AssertionError(f"trigger never finished: {store.all('SELECT * FROM triggers')}")
        assert invocations(tmp_path) == 1
        assert store.all("SELECT * FROM alerts") == []
        assert store.one("SELECT count(*) AS n FROM slots")["n"] == 0
    assert (s.state_dir / "conductor.db").exists()


@respx.mock
def test_shutdown_waits_for_a_running_agent_and_keeps_its_handoff(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    """Real lifespan teardown while an agent is mid-run: it finishes within the grace period."""
    _mock_plane(workspace_config)
    claude = fake_claude(tmp_path, [{"lines": good_run(*handoff_lines("t1", "c-1")), "sleep": 1.0}])
    s = settings.model_copy(
        update={
            "claude_binary": claude,
            "dispatcher_tick_seconds": 0.05,
            "shutdown_grace_seconds": 20.0,
        }
    )
    app = create_app(s, {workspace_config.workspace_slug: workspace_config})
    with TestClient(app):
        app.state.store.execute(
            "INSERT INTO triggers(comment_id, member_uuid, nickname, workspace, plane_base_url,"
            " project_id, issue_id, kind, state, snapshot, received_at) VALUES"
            " ('c',NULL,'sark','testws',?,?,?,'redeliver','received','{}',?)",
            (
                workspace_config.plane_base_url,
                str(workspace_config.project_id),
                ISSUE_ID,
                time.time(),
            ),
        )
        app.state.dispatcher.kick()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not app.state.runner._tasks:
            time.sleep(0.02)
        assert app.state.runner._tasks, "agent never started"
    # lifespan exited: the agent was allowed to finish and its handoff is on disk
    from plane_conductor.store import Store

    st = Store(s.state_dir / "conductor.db")
    assert st.one("SELECT state FROM handoffs")["state"] == "posted"
    assert st.one("SELECT count(*) AS n FROM slots")["n"] == 0
