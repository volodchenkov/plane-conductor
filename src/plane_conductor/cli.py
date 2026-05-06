"""Plane Conductor CLI — typer-based.

plane-conductor serve                       - start the multi-workspace server
plane-conductor setup [--workspace <slug>]  - bulk-create bots + labels (per workspace, or all)
plane-conductor verify [--workspace <slug>] - smoke check Plane connectivity & roster
plane-conductor agents [--workspace <slug>] - print configured agents
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from plane_conductor import __version__
from plane_conductor.conductor_config import WorkspaceConfig, load_workspaces
from plane_conductor.config import Settings
from plane_conductor.exceptions import ConfigError, PlaneConductorError
from plane_conductor.logging_config import configure_logging

app = typer.Typer(
    name="plane-conductor",
    help="Webhook orchestrator that turns Plane mentions into Claude Code agent runs.",
    add_completion=False,
    no_args_is_help=True,
)


def _load_settings() -> Settings:
    try:
        return Settings()
    except Exception as exc:
        raise ConfigError(str(exc)) from exc


def _load_workspaces(settings: Settings) -> dict[str, WorkspaceConfig]:
    try:
        return load_workspaces(settings.conductor_dir)
    except Exception as exc:
        raise ConfigError(
            f"failed to load workspaces from {settings.conductor_dir}: {exc}"
        ) from exc


def _resolve_workspaces(
    workspaces: dict[str, WorkspaceConfig],
    slug: str | None,
) -> list[WorkspaceConfig]:
    """If --workspace was passed, return just that one. Otherwise all."""
    if slug is None:
        return list(workspaces.values())
    if slug.lower() not in workspaces:
        raise ConfigError(
            f"workspace {slug!r} not found. Available: {sorted(workspaces) or '(none)'}"
        )
    return [workspaces[slug.lower()]]


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"plane-conductor {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Print version and exit.",
            is_eager=True,
            callback=_version_callback,
        ),
    ] = False,
) -> None:
    pass


@app.command()
def serve(
    host: Annotated[str | None, typer.Option(help="Override WEBHOOK_HOST.")] = None,
    port: Annotated[int | None, typer.Option(help="Override WEBHOOK_PORT.")] = None,
) -> None:
    """Start the webhook server (uvicorn). Serves all workspaces in conductor.d/."""
    import uvicorn

    settings = _load_settings()
    workspaces = _load_workspaces(settings)
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    from plane_conductor.server import create_app

    app_obj = create_app(settings, workspaces)
    uvicorn.run(
        app_obj,
        host=host or settings.webhook_host,
        port=port or settings.webhook_port,
        log_config=None,
    )


@app.command()
def setup(
    workspace: Annotated[
        str | None,
        typer.Option("--workspace", help="Run for one workspace slug. Omit = run for all."),
    ] = None,
    states: Annotated[
        bool,
        typer.Option(
            "--states/--no-states",
            help="Also create the optional project states declared in the config.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print actions without making changes."),
    ] = False,
) -> None:
    """Bulk-create configured bots + labels (+ optional states). Idempotent."""
    settings = _load_settings()
    workspaces = _load_workspaces(settings)
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    from plane_conductor.setup.plane.runner import run_setup

    selected = _resolve_workspaces(workspaces, workspace)
    rc_total = 0
    for ws in selected:
        if len(selected) > 1:
            typer.echo(f"\n=== workspace: {ws.workspace_slug} ===")
        rc = asyncio.run(run_setup(ws, create_states=states, dry_run=dry_run))
        rc_total |= rc
    raise typer.Exit(code=rc_total)


@app.command()
def verify(
    workspace: Annotated[
        str | None,
        typer.Option("--workspace", help="Verify one workspace slug. Omit = verify all."),
    ] = None,
) -> None:
    """Smoke check: Plane connectivity, configured agents, label inventory."""
    settings = _load_settings()
    workspaces = _load_workspaces(settings)
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    from plane_conductor.setup.plane.verify import run_verify

    selected = _resolve_workspaces(workspaces, workspace)
    rc_total = 0
    for ws in selected:
        if len(selected) > 1:
            typer.echo(f"\n=== workspace: {ws.workspace_slug} ===")
        rc = asyncio.run(run_verify(ws))
        rc_total |= rc
    raise typer.Exit(code=rc_total)


@app.command(name="agents")
def agents(
    workspace: Annotated[
        str | None, typer.Option("--workspace", help="Show one workspace's agents. Omit = all.")
    ] = None,
) -> None:
    """Print the configured nickname → prompt-role mapping."""
    settings = _load_settings()
    workspaces = _load_workspaces(settings)
    selected = _resolve_workspaces(workspaces, workspace)
    for ws in selected:
        if len(selected) > 1:
            typer.echo(f"\n[{ws.workspace_slug}]")
        width = max((len(a.nickname) for a in ws.agents), default=0)
        for agent in ws.agents:
            typer.echo(f"  {agent.nickname:<{width}}  →  {agent.prompt_role}")


def main() -> None:
    try:
        app()
    except PlaneConductorError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


if __name__ == "__main__":
    main()
