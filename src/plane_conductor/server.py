"""FastAPI app factory — multi-workspace.

Loads every workspace from `settings.conductor_dir`, builds one PlaneClient
per workspace, and mounts `/<slug>/webhook` for each.
"""

from __future__ import annotations

import fcntl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import IO

from fastapi import FastAPI

from plane_conductor import __version__
from plane_conductor.conductor_config import WorkspaceConfig, load_workspaces
from plane_conductor.config import Settings
from plane_conductor.dispatcher import Dispatcher
from plane_conductor.logging_config import configure_logging, get_logger
from plane_conductor.plane_client import PlaneClient
from plane_conductor.recovery import recover
from plane_conductor.roots import RootCache
from plane_conductor.runner import Runner
from plane_conductor.store import Store
from plane_conductor.watcher import Watcher
from plane_conductor.webhook import build_router
from plane_conductor.workbench_client import build_notifier, make_root_resolver

log = get_logger(__name__)


def _build_clients(
    workspaces: dict[str, WorkspaceConfig],
    settings: Settings,
) -> dict[str, tuple[WorkspaceConfig, PlaneClient]]:
    return {
        slug: (
            ws,
            PlaneClient(
                ws.plane_base_url,
                ws.plane_api_key.get_secret_value(),
                ws.workspace_slug,
                retry_after_max_seconds=settings.retry_after_max_seconds,
                retry_total_max_seconds=settings.retry_total_max_seconds,
                request_deadline_seconds=settings.request_deadline_seconds,
            ),
        )
        for slug, ws in workspaces.items()
    }


def _acquire_lock(settings: Settings) -> IO[str]:
    """Exclusive ``flock`` on ``<state_dir>/conductor.lock`` for the process life.

    A second conductor on the same state dir exits non-zero: old and new never run together
    (QSALE-55 Transition).
    """
    settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    fh = (settings.state_dir / "conductor.lock").open("a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        fh.close()
        raise RuntimeError(
            f"another conductor holds {settings.state_dir / 'conductor.lock'}; refusing to start"
        ) from exc
    return fh


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

    workspaces_with_clients = _build_clients(workspaces, settings)
    settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = Store(settings.state_dir / "conductor.db")
    roots = RootCache(workspaces_with_clients)
    runner = Runner(settings, store, workspaces_with_clients)
    dispatcher = Dispatcher(settings, store, workspaces_with_clients, runner, roots)
    watcher = Watcher(settings, store, workspaces_with_clients, roots, on_trigger=dispatcher.kick)
    runner.on_change = dispatcher.kick
    runner.pre_release = dispatcher.prepare_rerun
    runner.on_alert = watcher.kick
    notifier = build_notifier(settings)
    if notifier is not None:
        notifier.resolver = make_root_resolver(workspaces_with_clients)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        log.info(
            "server_start",
            version=__version__,
            conductor_dir=str(settings.conductor_dir),
            workspaces=sorted(workspaces),
            agents_total=sum(len(ws.agents) for ws in workspaces.values()),
        )
        lock = _acquire_lock(settings)
        try:
            try:
                recover(settings, store, workspaces)
            except Exception:
                log.exception("recovery_failed")
            if notifier is not None:
                notifier.start()
            dispatcher.start()
            watcher.start()
            try:
                yield
            finally:
                # stop starting runs first, then let the running ones finish (or kill them
                # after the grace), then publish what they left in the outbox
                await dispatcher.stop()
                if notifier is not None:
                    await notifier.stop()
                await runner.wait_idle(grace_seconds=settings.shutdown_grace_seconds)
                await watcher.stop()
                for _ws, plane in workspaces_with_clients.values():
                    await plane.aclose()
                store.close()
                log.info("server_stop")
        finally:
            with suppress(OSError):
                fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()

    app = FastAPI(title="Plane Conductor", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.workspaces = workspaces
    app.state.workspaces_with_clients = workspaces_with_clients
    app.state.runner = runner
    app.state.store = store
    app.state.dispatcher = dispatcher
    app.state.watcher = watcher
    app.state.notifier = notifier

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "version": __version__,
            "workspaces": sorted(workspaces),
            # Where each workspace's credentials were read from (file|inline),
            # never the values: lets the host check a move to the file form.
            "credential_sources": {
                slug: ws.credential_sources() for slug, ws in sorted(workspaces.items())
            },
        }

    app.include_router(
        build_router(settings, workspaces_with_clients, store, dispatcher.kick, notifier)
    )
    return app
