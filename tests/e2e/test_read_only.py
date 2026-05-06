"""Read-only e2e — does not mutate Plane state."""

from __future__ import annotations

from uuid import UUID

from plane_conductor.plane_client import PlaneClient


async def test_workspace_reachable(plane: PlaneClient, plane_workspace_slug: str) -> None:
    projects = await plane.ping()
    # ping() returns the project list; just verify it didn't error.
    assert isinstance(projects, list)


async def test_workspace_members_listable(plane: PlaneClient) -> None:
    members = await plane.list_workspace_members()
    assert isinstance(members, list)
    # Must have at least the API key owner.
    assert len(members) >= 1


async def test_project_labels_listable(plane: PlaneClient, plane_project_id: UUID) -> None:
    labels = await plane.list_labels(plane_project_id)
    assert isinstance(labels, list)


async def test_project_states_listable(plane: PlaneClient, plane_project_id: UUID) -> None:
    states = await plane.list_states(plane_project_id)
    assert isinstance(states, list)
    # Plane projects always ship at least Backlog/Todo/InProgress/Done/Cancelled.
    assert len(states) >= 1
