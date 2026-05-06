"""FastAPI app factory — multi-workspace.

Loads every workspace from `settings.conductor_dir`, builds one PlaneClient
per workspace, and mounts `/<slug>/webhook` for each.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from plane_conductor import __version__
from plane_conductor.conductor_config import WorkspaceConfig, load_workspaces
from plane_conductor.config import Settings
from plane_conductor.logging_config import configure_logging, get_logger
from plane_conductor.plane_client import PlaneClient
from plane_conductor.runner import Runner, recover_orphaned_sessions
from plane_conductor.webhook import build_router

log = get_logger(__name__)


def _build_clients(
    workspaces: dict[str, WorkspaceConfig],
) -> dict[str, tuple[WorkspaceConfig, PlaneClient]]:
    return {
        slug: (
            ws,
            PlaneClient(ws.plane_base_url, ws.plane_api_key, ws.workspace_slug),
        )
        for slug, ws in workspaces.items()
    }


def create_app(
    settings: Settings | None = None,
    workspaces: dict[str, WorkspaceConfig] | None = None,
) -> FastAPI:
    """Build a FastAPI app wired with settings, all workspaces, runner.

    `workspaces` is optional — useful for tests that want to inject configs
    directly. In production it's `None` and the factory loads from
    `settings.conductor_dir`.
    """
    settings = settings or Settings()
    if workspaces is None:
        workspaces = load_workspaces(settings.conductor_dir)

    configure_logging(level=settings.log_level, fmt=settings.log_format)

    workspaces_with_clients = _build_clients(workspaces)
    runner = Runner(settings=settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        log.info(
            "server_start",
            version=__version__,
            conductor_dir=str(settings.conductor_dir),
            workspaces=sorted(workspaces),
            agents_total=sum(len(ws.agents) for ws in workspaces.values()),
        )
        try:
            await recover_orphaned_sessions(settings, workspaces_with_clients)
        except Exception as exc:
            log.error("recovery_scan_failed", error=str(exc))
        try:
            yield
        finally:
            await runner.wait_idle(grace_seconds=settings.shutdown_grace_seconds)
            for _ws, plane in workspaces_with_clients.values():
                await plane.aclose()
            log.info("server_stop")

    app = FastAPI(title="Plane Conductor", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.workspaces = workspaces
    app.state.workspaces_with_clients = workspaces_with_clients
    app.state.runner = runner

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "version": __version__,
            "workspaces": sorted(workspaces),
        }

    app.include_router(build_router(settings, workspaces_with_clients, runner))
    return app
