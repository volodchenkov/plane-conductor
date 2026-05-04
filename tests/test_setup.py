from __future__ import annotations

from uuid import UUID

import httpx
import pytest
import respx

from plane_conductor.conductor_config import ConductorConfig
from plane_conductor.config import Settings
from plane_conductor.plane_client import PlaneClient
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
    client: PlaneClient, conductor_config: ConductorConfig
) -> None:
    # conductor_config has 3 agents: sark, rinzler, gem
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

    statuses = await invite_roster(client, conductor_config, "example.io")
    assert statuses["sark"] == "exists"
    assert statuses["rinzler"] == "exists"
    assert statuses["gem"] == "invited"
    assert invite_route.call_count == 1
    await client.aclose()


@respx.mock
async def test_invite_roster_dry_run_does_not_call(
    client: PlaneClient, conductor_config: ConductorConfig
) -> None:
    respx.get(f"{BASE}/api/v1/workspaces/{SLUG}/members/").mock(
        return_value=httpx.Response(200, json=[])
    )
    invite_route = respx.post(f"{BASE}/api/v1/workspaces/{SLUG}/invitations/").mock(
        return_value=httpx.Response(201, json={})
    )
    statuses = await invite_roster(client, conductor_config, "example.io", dry_run=True)
    assert all(v == "invited" for v in statuses.values())
    assert invite_route.call_count == 0
    await client.aclose()


@respx.mock
async def test_invite_roster_treats_409_as_exists(
    client: PlaneClient, conductor_config: ConductorConfig
) -> None:
    respx.get(f"{BASE}/api/v1/workspaces/{SLUG}/members/").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.post(f"{BASE}/api/v1/workspaces/{SLUG}/invitations/").mock(
        return_value=httpx.Response(409, json={"detail": "already invited"})
    )
    statuses = await invite_roster(client, conductor_config, "example.io")
    assert all(v == "exists" for v in statuses.values())
    await client.aclose()


@respx.mock
async def test_create_labels_idempotent(
    client: PlaneClient, conductor_config: ConductorConfig
) -> None:
    # conductor_config has 3 labels: artifact:spec, artifact:backend, role:system-analyst
    base = f"{BASE}/api/v1/workspaces/{SLUG}/projects/{PROJECT}/labels/"
    respx.get(base).mock(return_value=httpx.Response(200, json=[{"name": "artifact:spec"}]))
    create_route = respx.post(base).mock(return_value=httpx.Response(201, json={}))

    statuses = await create_labels(client, PROJECT, conductor_config)
    assert len(statuses) == 3
    assert statuses["artifact:spec"] == "exists"
    assert statuses["artifact:backend"] == "created"
    assert statuses["role:system-analyst"] == "created"
    assert create_route.call_count == 2
    await client.aclose()


@respx.mock
async def test_create_labels_handles_failure(
    client: PlaneClient, conductor_config: ConductorConfig
) -> None:
    base = f"{BASE}/api/v1/workspaces/{SLUG}/projects/{PROJECT}/labels/"
    respx.get(base).mock(return_value=httpx.Response(200, json=[]))

    def respond(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "artifact:spec" in body:
            return httpx.Response(500, text="boom")
        return httpx.Response(201, json={})

    respx.post(base).mock(side_effect=respond)

    statuses = await create_labels(client, PROJECT, conductor_config)
    assert statuses["artifact:spec"] == "failed"
    assert statuses["artifact:backend"] == "created"
    assert statuses["role:system-analyst"] == "created"
    await client.aclose()


def test_settings_validators_reject_bad_log_format(settings: Settings) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(
            plane_base_url="https://x",
            plane_api_key="k",
            plane_workspace_slug="ws",
            plane_project_id=settings.plane_project_id,
            webhook_secret="s",
            email_domain="x.io",
            prompts_dir=settings.prompts_dir,
            initiator_uuid=settings.initiator_uuid,
            log_format="xml",  # invalid
            _env_file=None,  # type: ignore[call-arg]
        )


def test_allowlist_parsing(settings: Settings) -> None:
    settings.allowed_nicknames = "sark, rinzler ,, gem"
    s = settings.allowed_nicknames_set
    assert s == frozenset({"sark", "rinzler", "gem"})
