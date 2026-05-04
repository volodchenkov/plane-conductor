"""Plane Conductor CLI — typer-based.

plane-conductor serve   - start the webhook server
plane-conductor setup   - bulk-create bot users + labels (+ optional states)
plane-conductor verify  - smoke check Plane connectivity & roster
plane-conductor agents  - print the configured agents (nickname → role)
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from plane_conductor import __version__
from plane_conductor.conductor_config import ConductorConfig, load_config
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
        return Settings()  # type: ignore[call-arg]
    except Exception as exc:  # pydantic ValidationError or env issues
        raise ConfigError(str(exc)) from exc


def _load_config(settings: Settings) -> ConductorConfig:
    try:
        return load_config(settings.conductor_config)
    except Exception as exc:
        raise ConfigError(f"failed to load {settings.conductor_config}: {exc}") from exc


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
    """Start the webhook server (uvicorn)."""
    import uvicorn

    settings = _load_settings()
    config = _load_config(settings)
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    from plane_conductor.server import create_app

    app_obj = create_app(settings, config)
    uvicorn.run(
        app_obj,
        host=host or settings.webhook_host,
        port=port or settings.webhook_port,
        log_config=None,
    )


@app.command()
def setup(
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
    """Bulk-create configured bot users + labels (+ optional states). Idempotent."""
    settings = _load_settings()
    config = _load_config(settings)
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    from plane_conductor.setup.plane.runner import run_setup

    rc = asyncio.run(run_setup(settings, config, create_states=states, dry_run=dry_run))
    raise typer.Exit(code=rc)


@app.command()
def verify() -> None:
    """Smoke check: Plane connectivity, configured agents, label inventory."""
    settings = _load_settings()
    config = _load_config(settings)
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    from plane_conductor.setup.plane.verify import run_verify

    rc = asyncio.run(run_verify(settings, config))
    raise typer.Exit(code=rc)


@app.command(name="agents")
def agents() -> None:
    """Print the configured nickname → prompt-role mapping."""
    settings = _load_settings()
    config = _load_config(settings)
    width = max((len(a.nickname) for a in config.agents), default=0)
    for agent in config.agents:
        typer.echo(f"  {agent.nickname:<{width}}  →  {agent.prompt_role}")


def main() -> None:
    try:
        app()
    except PlaneConductorError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


if __name__ == "__main__":
    main()
