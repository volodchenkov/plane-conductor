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
    assert out is None
    await client.aclose()


@respx.mock
async def test_list_issues_sends_thin_fields_by_default(client: PlaneClient) -> None:
    """Default thin field whitelist must reach the wire — Plane returns
    `description_html` per row otherwise, which blows the MCP tool-result
    cap on projects with bloated SPECs (see PlaneClient.LIST_ISSUES_DEFAULT_FIELDS).
    """
    url = f"{BASE}/api/v1/workspaces/{SLUG}/projects/{PROJECT}/issues/"
    route = respx.get(url).mock(return_value=httpx.Response(200, json={"results": []}))
    await client.list_issues(PROJECT)
    assert route.called
    sent_fields = route.calls.last.request.url.params.get("fields")
    assert sent_fields == PlaneClient.LIST_ISSUES_DEFAULT_FIELDS
    assert "description_html" not in (sent_fields or "")
    await client.aclose()


@respx.mock
async def test_list_issues_fields_none_omits_param(client: PlaneClient) -> None:
    """`fields=None` opts back into the full record — escape hatch for any
    future caller that genuinely needs the body."""
    url = f"{BASE}/api/v1/workspaces/{SLUG}/projects/{PROJECT}/issues/"
    route = respx.get(url).mock(return_value=httpx.Response(200, json={"results": []}))
    await client.list_issues(PROJECT, fields=None)
    assert route.called
    assert "fields" not in route.calls.last.request.url.params
    await client.aclose()


@respx.mock
async def test_list_issues_forwards_fields_on_pagination(client: PlaneClient) -> None:
    """The `fields` whitelist must be re-sent on every cursor page, otherwise
    page 2+ silently regress to fat responses."""
    url = f"{BASE}/api/v1/workspaces/{SLUG}/projects/{PROJECT}/issues/"
    responses = iter(
        [
            httpx.Response(200, json={"results": [{"id": "a"}], "next_cursor": "tok"}),
            httpx.Response(200, json={"results": [{"id": "b"}], "next_cursor": None}),
        ]
    )
    route = respx.get(url).mock(side_effect=lambda req: next(responses))
    items = await client.list_issues(PROJECT, fields="id,name")
    assert [i["id"] for i in items] == ["a", "b"]
    assert route.call_count == 2
    for call in route.calls:
        assert call.request.url.params.get("fields") == "id,name"
    await client.aclose()


@respx.mock
async def test_retrieve_issue_by_sequence_id_finds_match(client: PlaneClient) -> None:
    """First-match short-circuit — the iteration stops as soon as the sequence_id
    is found, but the underlying list_issues walks pages until then."""
    url = f"{BASE}/api/v1/workspaces/{SLUG}/projects/{PROJECT}/issues/"
    respx.get(url).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": "uuid-71", "sequence_id": 71, "name": "Other"},
                    {"id": "uuid-72", "sequence_id": 72, "name": "Target"},
                    {"id": "uuid-73", "sequence_id": 73, "name": "Newer"},
                ],
                "next_cursor": None,
            },
        )
    )
    issue = await client.retrieve_issue_by_sequence_id(PROJECT, 72)
    assert issue is not None
    assert issue["id"] == "uuid-72"
    assert issue["name"] == "Target"
    await client.aclose()


@respx.mock
async def test_retrieve_issue_by_sequence_id_returns_none_when_missing(
    client: PlaneClient,
) -> None:
    """Caller-friendly contract: missing sequence_id yields None, not an exception."""
    url = f"{BASE}/api/v1/workspaces/{SLUG}/projects/{PROJECT}/issues/"
    respx.get(url).mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": "uuid-1", "sequence_id": 1}], "next_cursor": None},
        )
    )
    issue = await client.retrieve_issue_by_sequence_id(PROJECT, 9999)
    assert issue is None
    await client.aclose()


@respx.mock
async def test_request_retries_on_429_with_retry_after(client: PlaneClient) -> None:
    """429 with `Retry-After: N` must sleep N seconds and retry. Plane's
    `ApiKeyRateThrottle` (DRF) emits a whole-second Retry-After header."""
    slept: list[float] = []

    async def fake_sleep(d: float) -> None:
        slept.append(d)

    client._sleep = fake_sleep  # type: ignore[method-assign]
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "2"}, json={"error": "throttled"}),
            httpx.Response(200, json={"results": [{"id": "ok"}]}),
        ]
    )
    route = respx.get(f"{BASE}/api/v1/workspaces/{SLUG}/projects/").mock(
        side_effect=lambda req: next(responses)
    )
    items = await client.ping()
    assert items == [{"id": "ok"}]
    assert route.call_count == 2
    assert slept == [2.0]
    await client.aclose()


@respx.mock
async def test_request_retries_on_429_with_exponential_backoff(client: PlaneClient) -> None:
    """No Retry-After → exponential backoff: backoff_base * 2**attempt
    (1.0, 2.0, 4.0 with defaults)."""
    slept: list[float] = []

    async def fake_sleep(d: float) -> None:
        slept.append(d)

    client._sleep = fake_sleep  # type: ignore[method-assign]
    responses = iter(
        [
            httpx.Response(429, json={}),
            httpx.Response(429, json={}),
            httpx.Response(200, json={"results": [{"id": "ok"}]}),
        ]
    )
    route = respx.get(f"{BASE}/api/v1/workspaces/{SLUG}/projects/").mock(
        side_effect=lambda req: next(responses)
    )
    await client.ping()
    assert route.call_count == 3
    assert slept == [1.0, 2.0]
    await client.aclose()


@respx.mock
async def test_request_raises_429_after_max_retries(client: PlaneClient) -> None:
    """Persistent 429 past `max_retries` re-raises PlaneAPIError(429) — the
    caller still gets the error, just with retries first."""

    async def fake_sleep(d: float) -> None:
        pass

    client._sleep = fake_sleep  # type: ignore[method-assign]
    route = respx.get(f"{BASE}/api/v1/workspaces/{SLUG}/projects/").mock(
        return_value=httpx.Response(429, json={"error": "throttled"})
    )
    with pytest.raises(PlaneAPIError) as exc_info:
        await client.ping()
    assert exc_info.value.status_code == 429
    # default max_retries=3 → 1 initial + 3 retries = 4 calls
    assert route.call_count == 4
    await client.aclose()


@respx.mock
async def test_request_does_not_retry_non_429(client: PlaneClient) -> None:
    """500 (or any non-429 4xx/5xx) must NOT be retried — those are not
    rate-limit signals and silent retries could mask real bugs."""
    slept: list[float] = []

    async def fake_sleep(d: float) -> None:
        slept.append(d)

    client._sleep = fake_sleep  # type: ignore[method-assign]
    route = respx.get(f"{BASE}/api/v1/workspaces/{SLUG}/projects/").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )
    with pytest.raises(PlaneAPIError) as exc_info:
        await client.ping()
    assert exc_info.value.status_code == 500
    assert route.call_count == 1
    assert slept == []
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
