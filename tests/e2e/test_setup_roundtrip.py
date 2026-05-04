"""Run the setup flow in dry-run mode against the real Plane (read-only)."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

from plane_conductor.conductor_config import load_config
from plane_conductor.plane_client import PlaneClient
from plane_conductor.setup.plane.create_labels import create_labels
from plane_conductor.setup.plane.create_users import invite_roster

# Use the shipped SDLC example as the canonical "full" workflow shape.
EXAMPLE_CONFIG = Path(__file__).resolve().parents[2] / "examples" / "sdlc-conductor.yaml"


async def test_invite_roster_dry_run_against_real_plane(plane: PlaneClient) -> None:
    config = load_config(EXAMPLE_CONFIG)
    domain = os.environ.get("EMAIL_DOMAIN", "example.io")
    statuses = await invite_roster(plane, config, domain, dry_run=True)
    # Every configured agent gets a status (invited or exists — dry-run doesn't fail).
    assert len(statuses) == len(config.agents)


async def test_create_labels_dry_run_against_real_plane(
    plane: PlaneClient, plane_project_id: UUID
) -> None:
    config = load_config(EXAMPLE_CONFIG)
    statuses = await create_labels(plane, plane_project_id, config, dry_run=True)
    assert set(statuses.keys()) == set(config.all_label_names())
