from __future__ import annotations

from uuid import UUID

import httpx
import pytest
import respx

from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.config import Settings
from plane_conductor.plane_client import PlaneClient
from plane_conductor.setup.plane.add_project_members import add_roster_to_project
from plane_conductor.setup.plane.create_labels import create_labels
from plane_conductor.setup.plane.create_users import invite_roster

BASE = "https://plane.test"
SLUG = "testws"
PROJECT = UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def client() -> PlaneClient:
    return PlaneClient(BASE, "k", SLUG)


@respx.mock
async def test_invite_roster_skips_existing_and_invites_rest(
    client: PlaneClient, workspace_config: WorkspaceConfig
) -> None:
    # workspace_config has 3 agents: sark, rinzler, gem
    existing = [
        {"email": "sark@example.io"},
        {"member": {"email": "rinzler@example.io"}},
    ]
    respx.get(f"{BASE}/api/v1/workspaces/{SLUG}/members/").mock(
        return_value=httpx.Response(200, json=existing)
    )
    invite_route = respx.post(f"{BASE}/api/v1/workspaces/{SLUG}/invitations/").mock(
        return_value=httpx.Response(201, json={})
    )

    statuses = await invite_roster(client, workspace_config)
    assert statuses["sark"] == "exists"
    assert statuses["rinzler"] == "exists"
    assert statuses["gem"] == "invited"
    assert invite_route.call_count == 1
    await client.aclose()


@respx.mock
async def test_invite_roster_dry_run_does_not_call(
    client: PlaneClient, workspace_config: WorkspaceConfig
) -> None:
    respx.get(f"{BASE}/api/v1/workspaces/{SLUG}/members/").mock(
        return_value=httpx.Response(200, json=[])
    )
    invite_route = respx.post(f"{BASE}/api/v1/workspaces/{SLUG}/invitations/").mock(
        return_value=httpx.Response(201, json={})
    )
    statuses = await invite_roster(client, workspace_config, dry_run=True)
    assert all(v == "invited" for v in statuses.values())
    assert invite_route.call_count == 0
    await client.aclose()


@respx.mock
async def test_invite_roster_treats_409_as_exists(
    client: PlaneClient, workspace_config: WorkspaceConfig
) -> None:
    respx.get(f"{BASE}/api/v1/workspaces/{SLUG}/members/").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.post(f"{BASE}/api/v1/workspaces/{SLUG}/invitations/").mock(
        return_value=httpx.Response(409, json={"detail": "already invited"})
    )
    statuses = await invite_roster(client, workspace_config)
    assert all(v == "exists" for v in statuses.values())
    await client.aclose()


@respx.mock
async def test_create_labels_idempotent(
    client: PlaneClient, workspace_config: WorkspaceConfig
) -> None:
    base = f"{BASE}/api/v1/workspaces/{SLUG}/projects/{PROJECT}/labels/"
    respx.get(base).mock(return_value=httpx.Response(200, json=[{"name": "artifact:spec"}]))
    create_route = respx.post(base).mock(return_value=httpx.Response(201, json={}))

    statuses = await create_labels(client, PROJECT, workspace_config)
    assert len(statuses) == 3
    assert statuses["artifact:spec"] == "exists"
    assert statuses["artifact:backend"] == "created"
    assert statuses["role:system-analyst"] == "created"
    assert create_route.call_count == 2
    await client.aclose()


@respx.mock
async def test_create_labels_handles_failure(
    client: PlaneClient, workspace_config: WorkspaceConfig
) -> None:
    base = f"{BASE}/api/v1/workspaces/{SLUG}/projects/{PROJECT}/labels/"
    respx.get(base).mock(return_value=httpx.Response(200, json=[]))

    def respond(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "artifact:spec" in body:
            return httpx.Response(500, text="boom")
        return httpx.Response(201, json={})

    respx.post(base).mock(side_effect=respond)

    statuses = await create_labels(client, PROJECT, workspace_config)
    assert statuses["artifact:spec"] == "failed"
    assert statuses["artifact:backend"] == "created"
    assert statuses["role:system-analyst"] == "created"
    await client.aclose()


@respx.mock
async def test_add_roster_to_project_adds_missing(
    client: PlaneClient, workspace_config: WorkspaceConfig
) -> None:
    # workspace_config has 3 agents: sark, rinzler, gem (email_domain=example.io)
    workspace_members = [
        {"id": "11111111-1111-1111-1111-111111111111", "email": "sark@example.io"},
        {"id": "22222222-2222-2222-2222-222222222222", "email": "rinzler@example.io"},
        {"id": "33333333-3333-3333-3333-333333333333", "email": "gem@example.io"},
    ]
    project_members = [
        {"id": "11111111-1111-1111-1111-111111111111"},  # sark already added
    ]
    members_url = f"{BASE}/api/v1/workspaces/{SLUG}/members/"
    project_members_url = f"{BASE}/api/v1/workspaces/{SLUG}/projects/{PROJECT}/members/"
    respx.get(members_url).mock(return_value=httpx.Response(200, json=workspace_members))
    respx.get(project_members_url).mock(return_value=httpx.Response(200, json=project_members))
    add_route = respx.post(project_members_url).mock(
        return_value=httpx.Response(201, json={"id": "x"})
    )

    statuses = await add_roster_to_project(client, workspace_config)
    assert statuses["sark"] == "exists"
    assert statuses["rinzler"] == "added"
    assert statuses["gem"] == "added"
    assert add_route.call_count == 2
    await client.aclose()


@respx.mock
async def test_add_roster_to_project_marks_uninvited_as_pending(
    client: PlaneClient, workspace_config: WorkspaceConfig
) -> None:
    # Only sark is in the workspace; rinzler and gem haven't accepted invites yet.
    workspace_members = [
        {"id": "11111111-1111-1111-1111-111111111111", "email": "sark@example.io"},
    ]
    members_url = f"{BASE}/api/v1/workspaces/{SLUG}/members/"
    project_members_url = f"{BASE}/api/v1/workspaces/{SLUG}/projects/{PROJECT}/members/"
    respx.get(members_url).mock(return_value=httpx.Response(200, json=workspace_members))
    respx.get(project_members_url).mock(return_value=httpx.Response(200, json=[]))
    add_route = respx.post(project_members_url).mock(
        return_value=httpx.Response(201, json={"id": "x"})
    )

    statuses = await add_roster_to_project(client, workspace_config)
    assert statuses["sark"] == "added"
    assert statuses["rinzler"] == "pending_invite"
    assert statuses["gem"] == "pending_invite"
    assert add_route.call_count == 1
    await client.aclose()


@respx.mock
async def test_add_roster_to_project_treats_409_as_exists(
    client: PlaneClient, workspace_config: WorkspaceConfig
) -> None:
    workspace_members = [
        {"id": "11111111-1111-1111-1111-111111111111", "email": "sark@example.io"},
        {"id": "22222222-2222-2222-2222-222222222222", "email": "rinzler@example.io"},
        {"id": "33333333-3333-3333-3333-333333333333", "email": "gem@example.io"},
    ]
    members_url = f"{BASE}/api/v1/workspaces/{SLUG}/members/"
    project_members_url = f"{BASE}/api/v1/workspaces/{SLUG}/projects/{PROJECT}/members/"
    respx.get(members_url).mock(return_value=httpx.Response(200, json=workspace_members))
    respx.get(project_members_url).mock(return_value=httpx.Response(200, json=[]))
    respx.post(project_members_url).mock(
        return_value=httpx.Response(409, json={"detail": "already a member"})
    )

    statuses = await add_roster_to_project(client, workspace_config)
    assert all(v == "exists" for v in statuses.values())
    await client.aclose()


def test_settings_validators_reject_bad_log_format() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(
            log_format="xml",  # invalid
            _env_file=None,  # type: ignore[call-arg]
        )
