"""Smoke tests for the FastAPI factory."""

from __future__ import annotations

from fastapi.testclient import TestClient

from plane_conductor.conductor_config import ConductorConfig
from plane_conductor.config import Settings
from plane_conductor.server import create_app


def test_create_app_health_endpoint(settings: Settings, conductor_config: ConductorConfig) -> None:
    app = create_app(settings, conductor_config)
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "version" in body


def test_create_app_wires_state(settings: Settings, conductor_config: ConductorConfig) -> None:
    app = create_app(settings, conductor_config)
    with TestClient(app):
        assert app.state.settings is settings
        assert app.state.config is conductor_config
        assert app.state.runner is not None
        assert app.state.plane is not None


def test_create_app_webhook_endpoint_present(
    settings: Settings, conductor_config: ConductorConfig
) -> None:
    app = create_app(settings, conductor_config)
    with TestClient(app) as client:
        # Without signature → 401, but the route exists.
        resp = client.post("/webhook", content=b"{}")
        assert resp.status_code == 401
