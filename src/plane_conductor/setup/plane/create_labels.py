"""Create the configured labels on the project (idempotent)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from plane_conductor.conductor_config import ConductorConfig
from plane_conductor.exceptions import PlaneAPIError
from plane_conductor.logging_config import get_logger
from plane_conductor.plane_client import PlaneClient

log = get_logger(__name__)


def _existing_label_names(labels: list[dict[str, Any]]) -> set[str]:
    return {str(lbl.get("name", "")).lower() for lbl in labels}


async def create_labels(
    plane: PlaneClient,
    project_id: UUID,
    config: ConductorConfig,
    *,
    dry_run: bool = False,
) -> dict[str, str]:
    """Create every label declared in `config.labels`. Returns {name: status}.

    Status values: 'created', 'exists', 'failed'.
    """
    existing = _existing_label_names(await plane.list_labels(project_id))

    statuses: dict[str, str] = {}
    for lbl in config.all_labels():
        if lbl.name.lower() in existing:
            log.info("label_exists", name=lbl.name)
            statuses[lbl.name] = "exists"
            continue
        if dry_run:
            log.info("label_create_dry_run", name=lbl.name, color=lbl.color)
            statuses[lbl.name] = "created"
            continue
        try:
            await plane.create_label(project_id, lbl.name, color=lbl.color)
            log.info("label_created", name=lbl.name)
            statuses[lbl.name] = "created"
        except PlaneAPIError as exc:
            if exc.status_code in (400, 409):
                log.info("label_already_exists", name=lbl.name)
                statuses[lbl.name] = "exists"
                continue
            log.error(
                "label_create_failed",
                name=lbl.name,
                status=exc.status_code,
                error=exc.message,
            )
            statuses[lbl.name] = "failed"

    return statuses
