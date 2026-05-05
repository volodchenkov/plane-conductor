"""Run the setup flow in dry-run mode against the real Plane (read-only)."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from plane_conductor.conductor_config import load_workspace
from plane_conductor.plane_client import PlaneClient
from plane_conductor.setup.plane.create_labels import create_labels
from plane_conductor.setup.plane.create_users import invite_roster

# Use the shipped SDLC example as the canonical "full" workflow shape.
EXAMPLE_CONFIG = Path(__file__).resolve().parents[2] / "examples" / "conductor.d" / "sdlc.yaml"


async def test_invite_roster_dry_run_against_real_plane(plane: PlaneClient) -> None:
    workspace = load_workspace(EXAMPLE_CONFIG)
    statuses = await invite_roster(plane, workspace, dry_run=True)
    assert len(statuses) == len(workspace.agents)


async def test_create_labels_dry_run_against_real_plane(
    plane: PlaneClient, plane_project_id: UUID
) -> None:
    workspace = load_workspace(EXAMPLE_CONFIG)
    statuses = await create_labels(plane, plane_project_id, workspace, dry_run=True)
    assert set(statuses.keys()) == set(workspace.all_label_names())
