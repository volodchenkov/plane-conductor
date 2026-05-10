"""Tests for plane-tower — virtual MCP layer.

These exercise the workspace registry, label cache, and the protocol
invariants that the agent-side prompts used to enforce by discipline:
one-sub-per-role, label-non-empty after create, phase ordering, etc.

We mock Plane REST via respx so each test can pretend a workspace is
already hydrated and inspect the tower's calls.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from plane_conductor import mcp_tower
from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.mcp_tower import (
    DuplicateSubIssueError,
    LabelNotFoundError,
    TowerError,
    TowerRegistry,
    UnlabelledSubIssueError,
    WorkspaceContext,
    WorkspaceNotResolvedError,
    create_sub_issue,
    escalate_upstream_gap,
    find_artifact_by_label,
    mark_phase_complete,
    mark_spec_approved,
    pickup_issue,
    post_changes,
    post_review,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ROOT_UUID = "11111111-1111-1111-1111-111111111111"
SPEC_SUB_UUID = "22222222-2222-2222-2222-222222222222"
BACKEND_SUB_UUID = "33333333-3333-3333-3333-333333333333"
LABEL_SPEC = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
LABEL_BACKEND = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
LABEL_API_TESTING = "cccccccc-cccc-cccc-cccc-cccccccccccc"
SARK_MEMBER = "44444444-4444-4444-4444-444444444444"
INITIATOR = "00000000-0000-0000-0000-000000000099"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKSPACE_SLUG", raising=False)
    monkeypatch.delenv("AGENT_NICKNAME", raising=False)


@pytest.fixture
def ctx(workspace_config: WorkspaceConfig) -> WorkspaceContext:
    """Pre-hydrated WorkspaceContext — bypasses bootstrap fetch."""
    c = WorkspaceContext(
        config=workspace_config,
        project_identifier="TEST",
        project_name="Test Project",
    )
    c.label_by_name = {
        "artifact:spec": LABEL_SPEC,
        "artifact:backend": LABEL_BACKEND,
        "artifact:api-testing": LABEL_API_TESTING,
    }
    c.state_by_group = {"backlog": "state-bl", "cancelled": "state-can"}
    c.state_by_name = {"Backlog": "state-bl", "Cancelled": "state-can"}
    c.member_by_email = {"sark@example.io": SARK_MEMBER}
    c.member_by_nickname = {"sark": SARK_MEMBER}
    return c


@pytest.fixture
def registry(ctx: WorkspaceContext, monkeypatch: pytest.MonkeyPatch) -> TowerRegistry:
    reg = TowerRegistry()
    reg.by_slug[ctx.config.workspace_slug] = ctx
    reg.by_project_id[str(ctx.config.project_id)] = ctx
    reg.by_project_identifier["TEST"] = ctx
    monkeypatch.setattr(mcp_tower, "_REGISTRY", reg)
    return reg


@pytest.fixture
def project_id(ctx: WorkspaceContext) -> str:
    return str(ctx.config.project_id)


# ---------------------------------------------------------------------------
# Registry / routing
# ---------------------------------------------------------------------------


def test_resolve_explicit_workspace(registry: TowerRegistry, ctx: WorkspaceContext) -> None:
    assert registry.resolve(workspace=ctx.config.workspace_slug) is ctx


def test_resolve_unknown_workspace_raises(registry: TowerRegistry) -> None:
    with pytest.raises(WorkspaceNotResolvedError, match="not registered"):
        registry.resolve(workspace="nonexistent")


def test_resolve_via_env(
    registry: TowerRegistry,
    ctx: WorkspaceContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKSPACE_SLUG", ctx.config.workspace_slug)
    assert registry.resolve() is ctx


def test_resolve_via_project_identifier(registry: TowerRegistry, ctx: WorkspaceContext) -> None:
    assert registry.resolve(project_identifier="test") is ctx
    assert registry.resolve(project_identifier="TEST") is ctx


def test_resolve_no_signal_raises(registry: TowerRegistry) -> None:
    with pytest.raises(WorkspaceNotResolvedError, match="cannot determine"):
        registry.resolve()


def test_label_uuid_resolves_known_artifact(ctx: WorkspaceContext) -> None:
    assert ctx.artifact_label_uuid("spec") == LABEL_SPEC


def test_label_uuid_unknown_role_raises(ctx: WorkspaceContext) -> None:
    with pytest.raises(LabelNotFoundError, match="unknown role"):
        ctx.artifact_label_uuid("nonsense")


def test_label_uuid_label_not_in_workspace_raises(ctx: WorkspaceContext) -> None:
    ctx.label_by_name = {}  # workspace has no labels yet
    with pytest.raises(LabelNotFoundError, match="not in cache"):
        ctx.artifact_label_uuid("spec")


def test_member_uuid_by_nickname(ctx: WorkspaceContext) -> None:
    assert ctx.member_uuid("sark") == SARK_MEMBER


def test_member_uuid_unknown_raises(ctx: WorkspaceContext) -> None:
    with pytest.raises(TowerError, match="no member matches"):
        ctx.member_uuid("ghost")


# ---------------------------------------------------------------------------
# pickup_issue
# ---------------------------------------------------------------------------


@respx.mock
async def test_pickup_issue_by_uuid(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    url = (
        f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/"
        f"projects/{project_id}/issues/{ROOT_UUID}/"
    )
    respx.get(url).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": ROOT_UUID,
                "name": "Add user dashboard",
                "sequence_id": 42,
                "labels": [],
                "parent": None,
            },
        )
    )
    result = await pickup_issue(
        issue_uuid=ROOT_UUID,
        workspace=ctx.config.workspace_slug,
    )
    assert result["id"] == ROOT_UUID
    assert result["sequence_id"] == 42
    assert result["workspace_slug"] == ctx.config.workspace_slug
    assert result["project_identifier"] == "TEST"


# ---------------------------------------------------------------------------
# find_artifact_by_label
# ---------------------------------------------------------------------------


def _list_issues_url(ctx: WorkspaceContext, project_id: str) -> str:
    return (
        f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/"
        f"projects/{project_id}/issues/"
    )


@respx.mock
async def test_find_artifact_returns_none_when_no_match(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    respx.get(_list_issues_url(ctx, project_id)).mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    result = await find_artifact_by_label(
        role="spec",
        root_uuid=ROOT_UUID,
        workspace=ctx.config.workspace_slug,
    )
    assert result == {"found": 0, "sub_issue": None}


@respx.mock
async def test_find_artifact_returns_one_match(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    respx.get(_list_issues_url(ctx, project_id)).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": SPEC_SUB_UUID,
                        "parent": ROOT_UUID,
                        "labels": [LABEL_SPEC],
                        "name": "SPEC: …",
                        "sequence_id": 38,
                    },
                    # decoy: same parent, different label
                    {
                        "id": BACKEND_SUB_UUID,
                        "parent": ROOT_UUID,
                        "labels": [LABEL_BACKEND],
                        "name": "Backend: …",
                        "sequence_id": 39,
                    },
                    # decoy: same label, different parent
                    {
                        "id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
                        "parent": "another-root",
                        "labels": [LABEL_SPEC],
                        "name": "other SPEC",
                        "sequence_id": 99,
                    },
                ]
            },
        )
    )
    result = await find_artifact_by_label(
        role="spec",
        root_uuid=ROOT_UUID,
        workspace=ctx.config.workspace_slug,
    )
    assert result["found"] == 1
    assert result["sub_issue"]["id"] == SPEC_SUB_UUID


@respx.mock
async def test_find_artifact_raises_on_duplicate(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    """The Sark COIN-37/38/48 incident: two SPEC subs with the same parent."""
    respx.get(_list_issues_url(ctx, project_id)).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": SPEC_SUB_UUID,
                        "parent": ROOT_UUID,
                        "labels": [LABEL_SPEC],
                        "name": "SPEC v1",
                        "sequence_id": 38,
                    },
                    {
                        "id": "55555555-5555-5555-5555-555555555555",
                        "parent": ROOT_UUID,
                        "labels": [LABEL_SPEC],
                        "name": "SPEC v2 (dupe)",
                        "sequence_id": 48,
                    },
                ]
            },
        )
    )
    with pytest.raises(DuplicateSubIssueError, match="2 sub-issues"):
        await find_artifact_by_label(
            role="spec",
            root_uuid=ROOT_UUID,
            workspace=ctx.config.workspace_slug,
        )


@respx.mock
async def test_find_artifact_handles_pagination_duplicates(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    """If a project has > one page of issues, both pages must be scanned for
    duplicate-detection. Regression for the silent-no-op risk of paginated
    responses against a real Plane project."""
    list_url = _list_issues_url(ctx, project_id)

    def _route(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        if cursor is None:
            return httpx.Response(
                200,
                json={
                    "next_cursor": "page2",
                    "results": [
                        {
                            "id": SPEC_SUB_UUID,
                            "parent": ROOT_UUID,
                            "labels": [LABEL_SPEC],
                            "name": "SPEC v1",
                            "sequence_id": 38,
                        },
                    ],
                },
            )
        if cursor == "page2":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "55555555-5555-5555-5555-555555555555",
                            "parent": ROOT_UUID,
                            "labels": [LABEL_SPEC],
                            "name": "SPEC v2 (dupe on page 2)",
                            "sequence_id": 48,
                        },
                    ],
                },
            )
        return httpx.Response(404)

    respx.get(list_url).mock(side_effect=_route)
    with pytest.raises(DuplicateSubIssueError, match="2 sub-issues"):
        await find_artifact_by_label(
            role="spec",
            root_uuid=ROOT_UUID,
            workspace=ctx.config.workspace_slug,
        )


# ---------------------------------------------------------------------------
# create_sub_issue — the central protected operation
# ---------------------------------------------------------------------------


@respx.mock
async def test_create_sub_issue_happy_path(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    base = f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/projects/{project_id}"
    # Pre-condition list (no existing spec sub)
    respx.get(f"{base}/issues/").mock(return_value=httpx.Response(200, json={"results": []}))
    # Get root
    respx.get(f"{base}/issues/{ROOT_UUID}/").mock(
        return_value=httpx.Response(
            200, json={"id": ROOT_UUID, "name": "Add user dashboard", "sequence_id": 42}
        )
    )
    # Create
    respx.post(f"{base}/issues/").mock(
        return_value=httpx.Response(
            201,
            json={"id": SPEC_SUB_UUID, "name": "SPEC: …", "labels": [LABEL_SPEC]},
        )
    )
    # Re-read for post-condition assert
    respx.get(f"{base}/issues/{SPEC_SUB_UUID}/").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": SPEC_SUB_UUID,
                "name": "SPEC: …",
                "labels": [LABEL_SPEC],
                "parent": ROOT_UUID,
                "sequence_id": 43,
            },
        )
    )

    result = await create_sub_issue(
        role="spec",
        root_uuid=ROOT_UUID,
        description_html="<p>initial</p>",
        nickname="sark",
        workspace=ctx.config.workspace_slug,
    )
    assert result["id"] == SPEC_SUB_UUID
    assert LABEL_SPEC in result["labels"]


@respx.mock
async def test_create_sub_issue_refuses_when_duplicate_exists(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    base = f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/projects/{project_id}"
    respx.get(f"{base}/issues/").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": SPEC_SUB_UUID, "parent": ROOT_UUID, "labels": [LABEL_SPEC]},
                ]
            },
        )
    )
    with pytest.raises(DuplicateSubIssueError, match="already exists"):
        await create_sub_issue(
            role="spec",
            root_uuid=ROOT_UUID,
            workspace=ctx.config.workspace_slug,
        )


@respx.mock
async def test_create_sub_issue_unlabelled_post_condition_fails_loudly(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    """If Plane silently drops labels=[…] (e.g. UUID typo) — fail loudly.

    This is the exact failure mode that produced unlabelled COIN-38 →
    duplicate COIN-48/50 in the Sark incident.
    """
    base = f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/projects/{project_id}"
    respx.get(f"{base}/issues/").mock(return_value=httpx.Response(200, json={"results": []}))
    respx.get(f"{base}/issues/{ROOT_UUID}/").mock(
        return_value=httpx.Response(
            200, json={"id": ROOT_UUID, "name": "Add user dashboard", "sequence_id": 42}
        )
    )
    respx.post(f"{base}/issues/").mock(
        return_value=httpx.Response(
            201,
            json={"id": SPEC_SUB_UUID, "name": "SPEC: …", "labels": []},
        )
    )
    # Re-read returns labels=[] — Plane silently dropped the labels
    respx.get(f"{base}/issues/{SPEC_SUB_UUID}/").mock(
        return_value=httpx.Response(
            200,
            json={"id": SPEC_SUB_UUID, "labels": [], "parent": ROOT_UUID},
        )
    )

    with pytest.raises(UnlabelledSubIssueError, match="silently dropped"):
        await create_sub_issue(
            role="spec",
            root_uuid=ROOT_UUID,
            workspace=ctx.config.workspace_slug,
        )


@respx.mock
async def test_create_sub_issue_unknown_role_raises(
    registry: TowerRegistry, ctx: WorkspaceContext
) -> None:
    with pytest.raises(LabelNotFoundError, match="unknown role"):
        await create_sub_issue(
            role="unicorn",
            root_uuid=ROOT_UUID,
            workspace=ctx.config.workspace_slug,
        )


# ---------------------------------------------------------------------------
# post_review — iteration counter + verdict validation
# ---------------------------------------------------------------------------


@respx.mock
async def test_post_review_increments_iter_from_existing(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    base = f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/projects/{project_id}"
    # find_artifact_by_label call
    respx.get(f"{base}/issues/").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": SPEC_SUB_UUID, "parent": ROOT_UUID, "labels": [LABEL_SPEC]},
                ]
            },
        )
    )
    # list comments — has prior REVIEW iter 2
    respx.get(f"{base}/issues/{SPEC_SUB_UUID}/comments/").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": "c1", "comment_html": "<p>REVIEW (iter 1) — CHANGES_REQUIRED</p>"},
                    {"id": "c2", "comment_html": "<p>REVIEW (iter 2) — CHANGES_REQUIRED</p>"},
                ]
            },
        )
    )
    posted: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        posted["body"] = request.content.decode()
        return httpx.Response(201, json={"id": "new-comment-id"})

    respx.post(f"{base}/issues/{SPEC_SUB_UUID}/comments/").mock(side_effect=_capture)

    result = await post_review(
        target="spec",
        verdict="approved",
        body_html="<p>fixed</p>",
        root_uuid=ROOT_UUID,
        workspace=ctx.config.workspace_slug,
    )
    assert result["iter"] == 3
    assert result["verdict"] == "APPROVED"
    assert "REVIEW (iter 3) — APPROVED" in posted["body"]


@respx.mock
async def test_post_review_first_iteration_when_no_prior(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    base = f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/projects/{project_id}"
    respx.get(f"{base}/issues/").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": SPEC_SUB_UUID, "parent": ROOT_UUID, "labels": [LABEL_SPEC]},
                ]
            },
        )
    )
    respx.get(f"{base}/issues/{SPEC_SUB_UUID}/comments/").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    respx.post(f"{base}/issues/{SPEC_SUB_UUID}/comments/").mock(
        return_value=httpx.Response(201, json={"id": "c-new"})
    )
    result = await post_review(
        target="spec",
        verdict="CHANGES_REQUIRED",
        body_html="<p>findings</p>",
        root_uuid=ROOT_UUID,
        workspace=ctx.config.workspace_slug,
    )
    assert result["iter"] == 1


async def test_post_review_invalid_verdict_raises(
    registry: TowerRegistry, ctx: WorkspaceContext
) -> None:
    with pytest.raises(TowerError, match="verdict must be"):
        await post_review(
            target="root",
            verdict="MAYBE",
            body_html="",
            root_uuid=ROOT_UUID,
            workspace=ctx.config.workspace_slug,
        )


# ---------------------------------------------------------------------------
# post_changes — OpenAPI defense
# ---------------------------------------------------------------------------


@respx.mock
async def test_post_changes_refuses_ready_without_openapi_when_views_touched(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    base = f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/projects/{project_id}"
    respx.get(f"{base}/issues/").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": BACKEND_SUB_UUID, "parent": ROOT_UUID, "labels": [LABEL_BACKEND]},
                ]
            },
        )
    )
    with pytest.raises(TowerError, match="API documentation defense"):
        await post_changes(
            target="backend",
            root_uuid=ROOT_UUID,
            summary="add tracking endpoint",
            files=[["apps/orders/views.py", "add OrderTrackingView"]],
            verification=[["./make.sh lint", "0 errors"], ["pytest", "all green"]],
            ready_for_review=True,
            workspace=ctx.config.workspace_slug,
        )


@respx.mock
async def test_post_changes_accepts_when_openapi_in_verification(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    base = f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/projects/{project_id}"
    respx.get(f"{base}/issues/").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": BACKEND_SUB_UUID, "parent": ROOT_UUID, "labels": [LABEL_BACKEND]},
                ]
            },
        )
    )
    respx.post(f"{base}/issues/{BACKEND_SUB_UUID}/comments/").mock(
        return_value=httpx.Response(201, json={"id": "c-new"})
    )
    result = await post_changes(
        target="backend",
        root_uuid=ROOT_UUID,
        summary="add tracking endpoint",
        files=[["apps/orders/views.py", "add OrderTrackingView"]],
        verification=[
            ["./make.sh lint", "0 errors"],
            ["/verify-openapi", "0 warnings, 0 errors"],
        ],
        ready_for_review=True,
        workspace=ctx.config.workspace_slug,
    )
    assert result["ready_for_review"] is True


@respx.mock
async def test_post_changes_skips_openapi_check_when_no_views_touched(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    base = f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/projects/{project_id}"
    respx.get(f"{base}/issues/").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": BACKEND_SUB_UUID, "parent": ROOT_UUID, "labels": [LABEL_BACKEND]},
                ]
            },
        )
    )
    respx.post(f"{base}/issues/{BACKEND_SUB_UUID}/comments/").mock(
        return_value=httpx.Response(201, json={"id": "c-new"})
    )
    # No views/serializers touched → openapi check not required
    result = await post_changes(
        target="backend",
        root_uuid=ROOT_UUID,
        summary="refactor models",
        files=[["apps/orders/models.py", "split Order into Order + OrderLine"]],
        verification=[["./make.sh lint", "0 errors"]],
        ready_for_review=True,
        workspace=ctx.config.workspace_slug,
    )
    assert result["ready_for_review"] is True


# ---------------------------------------------------------------------------
# mark_phase_complete — ordering
# ---------------------------------------------------------------------------


@respx.mock
async def test_mark_phase_complete_flips_checkbox(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    base = f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/projects/{project_id}"
    desc = (
        "## Phase status\n"
        "- [x] Phase 1: Context & Domain\n"
        "- [ ] Phase 2: Data Model\n"
        "- [ ] Phase 3: API Contract\n"
    )
    respx.get(f"{base}/issues/{SPEC_SUB_UUID}/").mock(
        return_value=httpx.Response(200, json={"id": SPEC_SUB_UUID, "description_html": desc})
    )
    captured: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"id": SPEC_SUB_UUID})

    respx.patch(f"{base}/issues/{SPEC_SUB_UUID}/").mock(side_effect=_capture)
    result = await mark_phase_complete(
        my_sub_uuid=SPEC_SUB_UUID,
        phase=2,
        workspace=ctx.config.workspace_slug,
    )
    assert result["phase"] == 2
    assert "[x] Phase 2" in captured["body"]
    assert "[ ] Phase 3" in captured["body"]  # phase 3 still open


@respx.mock
async def test_mark_phase_complete_refuses_to_skip_phases(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    base = f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/projects/{project_id}"
    desc = "- [ ] Phase 1: Context\n- [ ] Phase 2: Data Model\n"
    respx.get(f"{base}/issues/{SPEC_SUB_UUID}/").mock(
        return_value=httpx.Response(200, json={"id": SPEC_SUB_UUID, "description_html": desc})
    )
    with pytest.raises(TowerError, match="Phase 1 is still open"):
        await mark_phase_complete(
            my_sub_uuid=SPEC_SUB_UUID,
            phase=2,
            workspace=ctx.config.workspace_slug,
        )


# ---------------------------------------------------------------------------
# mark_spec_approved — refuses without prior APPROVED ARCH_REVIEW
# ---------------------------------------------------------------------------


@respx.mock
async def test_mark_spec_approved_refuses_without_arch_review(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    base = f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/projects/{project_id}"
    respx.get(f"{base}/issues/{SPEC_SUB_UUID}/comments/").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    with pytest.raises(TowerError, match="no prior ARCH_REVIEW"):
        await mark_spec_approved(
            spec_sub_uuid=SPEC_SUB_UUID,
            summary_html="<p>scope: backend only</p>",
            workspace=ctx.config.workspace_slug,
        )


@respx.mock
async def test_mark_spec_approved_refuses_when_changes_required(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    base = f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/projects/{project_id}"
    respx.get(f"{base}/issues/{SPEC_SUB_UUID}/comments/").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "c1",
                        "comment_html": "<p>ARCH_REVIEW (iter 2) — CHANGES_REQUIRED</p>",
                        "created_at": "2026-05-09T10:00:00Z",
                    },
                ]
            },
        )
    )
    with pytest.raises(TowerError, match="not APPROVED"):
        await mark_spec_approved(
            spec_sub_uuid=SPEC_SUB_UUID,
            summary_html="<p>scope</p>",
            workspace=ctx.config.workspace_slug,
        )


@respx.mock
async def test_mark_spec_approved_happy_path(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    """Latest ARCH_REVIEW marker says APPROVED → SPEC_APPROVED comment posts."""
    base = f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/projects/{project_id}"
    respx.get(f"{base}/issues/{SPEC_SUB_UUID}/comments/").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "c-old",
                        "comment_html": "<p>ARCH_REVIEW (iter 1) — CHANGES_REQUIRED</p>",
                        "created_at": "2026-05-08T10:00:00Z",
                    },
                    {
                        "id": "c-new",
                        "comment_html": "<p>ARCH_REVIEW (iter 2) — APPROVED.</p>",
                        "created_at": "2026-05-09T10:00:00Z",
                    },
                ]
            },
        )
    )
    respx.post(f"{base}/issues/{SPEC_SUB_UUID}/comments/").mock(
        return_value=httpx.Response(201, json={"id": "c-spec-approved"})
    )
    result = await mark_spec_approved(
        spec_sub_uuid=SPEC_SUB_UUID,
        summary_html="<p>scope: backend only</p>",
        workspace=ctx.config.workspace_slug,
    )
    assert result == {"comment_id": "c-spec-approved"}


@respx.mock
async def test_mark_spec_approved_rejects_approved_substring_in_changes_body(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    """Regression: a CHANGES_REQUIRED comment whose BODY quotes the word
    'APPROVED' (e.g. citing a previous review) must NOT be misread as a pass.
    Locks the structured-marker detection that replaced the old substring scan.
    """
    base = f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/projects/{project_id}"
    respx.get(f"{base}/issues/{SPEC_SUB_UUID}/comments/").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "c-trick",
                        "comment_html": (
                            "<p><strong>ARCH_REVIEW (iter 3) — CHANGES_REQUIRED.</strong></p>"
                            "<p>Iter 2 was previously APPROVED, but new findings require "
                            "another pass.</p>"
                        ),
                        "created_at": "2026-05-10T09:00:00Z",
                    },
                ]
            },
        )
    )
    with pytest.raises(TowerError, match="not APPROVED"):
        await mark_spec_approved(
            spec_sub_uuid=SPEC_SUB_UUID,
            summary_html="<p>scope</p>",
            workspace=ctx.config.workspace_slug,
        )


# ---------------------------------------------------------------------------
# escalate_upstream_gap
# ---------------------------------------------------------------------------


@respx.mock
async def test_escalate_upstream_gap_posts_comment(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    base = f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/projects/{project_id}"
    captured: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(201, json={"id": "c-new"})

    respx.post(f"{base}/issues/{BACKEND_SUB_UUID}/comments/").mock(side_effect=_capture)

    result = await escalate_upstream_gap(
        my_sub_uuid=BACKEND_SUB_UUID,
        affected="SPEC §3.2 API Contract",
        issue="missing pagination contract for /orders",
        proposed_resolution="re-trigger system-analyst to clarify §3",
        workspace=ctx.config.workspace_slug,
    )
    assert result["comment_id"] == "c-new"
    assert "BLOCKED — upstream gap" in captured["body"]
    assert "missing pagination" in captured["body"]
    assert str(ctx.config.initiator_uuid) in captured["body"]
