"""Create the configured project states (idempotent)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.exceptions import PlaneAPIError
from plane_conductor.logging_config import get_logger
from plane_conductor.plane_client import PlaneClient

log = get_logger(__name__)


def _existing_state_names(states: list[dict[str, Any]]) -> set[str]:
    return {str(s.get("name", "")).lower() for s in states}


async def create_states(
    plane: PlaneClient,
    project_id: UUID,
    workspace: WorkspaceConfig,
    *,
    dry_run: bool = False,
) -> dict[str, str]:
    """Create every state declared in `workspace.states`. Returns {state_name: status}."""
    existing = _existing_state_names(await plane.list_states(project_id))

    statuses: dict[str, str] = {}
    for state in workspace.states:
        if state.name.lower() in existing:
            log.info("state_exists", name=state.name)
            statuses[state.name] = "exists"
            continue
        if dry_run:
            log.info("state_create_dry_run", name=state.name, group=state.group)
            statuses[state.name] = "created"
            continue
        try:
            await plane.create_state(project_id, state.name, group=state.group, color=state.color)
            log.info("state_created", name=state.name, group=state.group)
            statuses[state.name] = "created"
        except PlaneAPIError as exc:
            log.error(
                "state_create_failed",
                name=state.name,
                status=exc.status_code,
                error=exc.message,
            )
            statuses[state.name] = "failed"

    return statuses
