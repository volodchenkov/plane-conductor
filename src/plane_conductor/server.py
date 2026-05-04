"""FastAPI app factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from plane_conductor import __version__
from plane_conductor.conductor_config import ConductorConfig, load_config
from plane_conductor.config import Settings
from plane_conductor.logging_config import configure_logging, get_logger
from plane_conductor.plane_client import PlaneClient
from plane_conductor.runner import Runner, recover_orphaned_sessions
from plane_conductor.webhook import build_router

log = get_logger(__name__)


def create_app(
    settings: Settings | None = None,
    config: ConductorConfig | None = None,
) -> FastAPI:
    """Build a FastAPI app wired with config, Plane client, and runner."""
    settings = settings or Settings()  # type: ignore[call-arg]
    if config is None:
        config = load_config(settings.conductor_config)

    configure_logging(level=settings.log_level, fmt=settings.log_format)

    plane = PlaneClient(
        settings.plane_base_url,
        settings.plane_api_key,
        settings.plane_workspace_slug,
    )
    runner = Runner(settings=settings, plane=plane, announce_spawn=config.announce_spawn)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        log.info(
            "server_start",
            version=__version__,
            workspace=settings.plane_workspace_slug,
            project=str(settings.plane_project_id),
            conductor_config=str(settings.conductor_config),
            agents=len(config.agents),
        )
        try:
            await recover_orphaned_sessions(settings, plane)
        except Exception as exc:
            log.error("recovery_scan_failed", error=str(exc))
        try:
            yield
        finally:
            await runner.wait_idle(grace_seconds=settings.shutdown_grace_seconds)
            await plane.aclose()
            log.info("server_stop")

    app = FastAPI(title="Plane Conductor", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.config = config
    app.state.plane = plane
    app.state.runner = runner

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    app.include_router(build_router(settings, config, plane, runner))
    return app
