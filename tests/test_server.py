"""Smoke tests for the FastAPI factory."""

from __future__ import annotations

from fastapi.testclient import TestClient

from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.config import Settings
from plane_conductor.server import create_app


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
