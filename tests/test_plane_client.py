from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest
import respx

from plane_conductor.exceptions import PlaneAPIError
from plane_conductor.plane_client import PlaneClient

BASE = "https://plane.test"
SLUG = "testws"
PROJECT = UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def client() -> PlaneClient:
    return PlaneClient(BASE, "test-key", SLUG)


@respx.mock
async def test_ping(client: PlaneClient) -> None:
    respx.get(f"{BASE}/api/v1/workspaces/{SLUG}/projects/").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "p1"}]})
    )
    projects = await client.ping()
    assert projects == [{"id": "p1"}]
    await client.aclose()


@respx.mock
async def test_list_workspace_members_unwraps_results(client: PlaneClient) -> None:
    respx.get(f"{BASE}/api/v1/workspaces/{SLUG}/members/").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": "u1", "email": "a@x.io"}, {"id": "u2", "email": "b@x.io"}]},
        )
    )
    members = await client.list_workspace_members()
    assert [m["id"] for m in members] == ["u1", "u2"]
    await client.aclose()


@respx.mock
async def test_get_member_filters_from_list(client: PlaneClient) -> None:
    member_id = "11111111-1111-1111-1111-111111111111"
    respx.get(f"{BASE}/api/v1/workspaces/{SLUG}/members/").mock(
        return_value=httpx.Response(200, json=[{"id": member_id, "email": "sark@x.io"}])
    )
    member = await client.get_member(member_id)
    assert member["email"] == "sark@x.io"
    await client.aclose()


@respx.mock
async def test_get_member_raises_when_truly_missing(client: PlaneClient) -> None:
    member_id = "11111111-1111-1111-1111-111111111111"
    respx.get(f"{BASE}/api/v1/workspaces/{SLUG}/members/{member_id}/").mock(
        return_value=httpx.Response(404, json={})
    )
    respx.get(f"{BASE}/api/v1/workspaces/{SLUG}/members/").mock(
        return_value=httpx.Response(200, json=[])
    )
    with pytest.raises(PlaneAPIError) as ei:
        await client.get_member(member_id)
    assert ei.value.status_code == 404
    await client.aclose()


@respx.mock
async def test_list_labels_and_create_label(client: PlaneClient) -> None:
    base = f"{BASE}/api/v1/workspaces/{SLUG}/projects/{PROJECT}/labels/"
    respx.get(base).mock(return_value=httpx.Response(200, json=[{"name": "bug"}]))
    labels = await client.list_labels(PROJECT)
    assert labels == [{"name": "bug"}]

    respx.post(base).mock(
        return_value=httpx.Response(201, json={"id": "l1", "name": "artifact:spec"})
    )
    created = await client.create_label(PROJECT, "artifact:spec", color="#3b82f6")
    assert created["name"] == "artifact:spec"
    await client.aclose()


@respx.mock
async def test_create_issue_comment(client: PlaneClient) -> None:
    issue = "33333333-3333-3333-3333-333333333333"
    url = f"{BASE}/api/v1/workspaces/{SLUG}/projects/{PROJECT}/issues/{issue}/comments/"
    route = respx.post(url).mock(return_value=httpx.Response(201, json={"id": "c1"}))
    out = await client.create_issue_comment(PROJECT, issue, "<p>hi</p>")
    assert out["id"] == "c1"
    body = route.calls.last.request.content.decode()
    assert "<p>hi</p>" in body
    await client.aclose()


@respx.mock
async def test_invite_member(client: PlaneClient) -> None:
    url = f"{BASE}/api/v1/workspaces/{SLUG}/invitations/"
    route = respx.post(url).mock(return_value=httpx.Response(201, json={"ok": True}))
    await client.invite_member("sark@example.io", role=15)
    body = json.loads(route.calls.last.request.content)
    # Plane v1 expects a single {email, role} object (not nested in `emails`).
    assert body == {"email": "sark@example.io", "role": 15}
    await client.aclose()


@respx.mock
async def test_request_raises_on_4xx(client: PlaneClient) -> None:
    respx.get(f"{BASE}/api/v1/workspaces/{SLUG}/projects/").mock(
        return_value=httpx.Response(403, text="forbidden")
    )
    with pytest.raises(PlaneAPIError) as ei:
        await client.ping()
    assert ei.value.status_code == 403
    await client.aclose()


@respx.mock
async def test_aexit_closes_client() -> None:
    respx.get(f"{BASE}/api/v1/workspaces/{SLUG}/projects/").mock(
        return_value=httpx.Response(200, json={"id": "ws"})
    )
    async with PlaneClient(BASE, "k", SLUG) as c:
        await c.ping()
    # After aexit, follow-up calls should fail because the client is closed.
    with pytest.raises(Exception):  # noqa: B017 — httpx raises various flavours
        await c.ping()


@respx.mock
async def test_results_helper_handles_list_payload(client: PlaneClient) -> None:
    respx.get(f"{BASE}/api/v1/workspaces/{SLUG}/projects/{PROJECT}/states/").mock(
        return_value=httpx.Response(200, json=[{"name": "Backlog"}])
    )
    states = await client.list_states(PROJECT)
    assert states == [{"name": "Backlog"}]
    await client.aclose()


@respx.mock
async def test_request_wraps_transport_error(client: PlaneClient) -> None:
    respx.get(f"{BASE}/api/v1/workspaces/{SLUG}/projects/").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    with pytest.raises(PlaneAPIError) as ei:
        await client.ping()
    assert ei.value.status_code == 0
    assert "transport error" in ei.value.message
    await client.aclose()


@respx.mock
async def test_request_returns_none_for_204(client: PlaneClient) -> None:
    issue = "33333333-3333-3333-3333-333333333333"
    respx.post(f"{BASE}/api/v1/workspaces/{SLUG}/projects/{PROJECT}/issues/{issue}/comments/").mock(
        return_value=httpx.Response(204)
    )
    out = await client.create_issue_comment(PROJECT, issue, "<p>x</p>")
    assert out is None  # type: ignore[comparison-overlap]
    await client.aclose()


@respx.mock
async def test_create_state_and_get_issue(client: PlaneClient) -> None:
    state_url = f"{BASE}/api/v1/workspaces/{SLUG}/projects/{PROJECT}/states/"
    respx.post(state_url).mock(
        return_value=httpx.Response(201, json={"id": "s1", "name": "Review"})
    )
    s = await client.create_state(PROJECT, "Review", group="started", color="#fff")
    assert s["name"] == "Review"

    issue_id = "33333333-3333-3333-3333-333333333333"
    respx.get(f"{BASE}/api/v1/workspaces/{SLUG}/projects/{PROJECT}/issues/{issue_id}/").mock(
        return_value=httpx.Response(200, json={"id": issue_id, "name": "QSALE-42"})
    )
    issue = await client.get_issue(PROJECT, issue_id)
    assert issue["name"] == "QSALE-42"
    await client.aclose()
