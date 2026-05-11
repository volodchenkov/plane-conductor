"""Tests for plane-tower — virtual MCP layer.

These exercise the workspace registry, label cache, and the protocol
invariants that the agent-side prompts used to enforce by discipline:
one-sub-per-role, label-non-empty after create, phase ordering, etc.

We mock Plane REST via respx so each test can pretend a workspace is
already hydrated and inspect the tower's calls.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
import respx

from plane_conductor import mcp_tower
from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.mcp_tower import (
    DuplicateSubIssueError,
    LabelNotFoundError,
    MentionInBodyError,
    RoleNotFoundError,
    TowerError,
    TowerRegistry,
    UnlabelledSubIssueError,
    WorkspaceContext,
    WorkspaceNotResolvedError,
    _assert_no_mentions,
    _role_mention,
    create_root_issue,
    create_sub_issue,
    escalate_upstream_gap,
    find_artifact_by_label,
    list_comments,
    mark_phase_complete,
    mark_spec_approved,
    pickup_issue,
    post_changes,
    post_comment,
    post_review,
    request_handoff,
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
# create_root_issue — file a new top-level task (Tron's DELEGATE route)
# ---------------------------------------------------------------------------


PIPELINE_DOC_ONLY_LABEL = "dddddddd-dddd-dddd-dddd-dddddddddddd"


@respx.mock
async def test_create_root_issue_happy_path(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    ctx.label_by_name["pipeline:doc-only"] = PIPELINE_DOC_ONLY_LABEL
    base = f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/projects/{project_id}"
    new_uuid = "99999999-9999-9999-9999-999999999999"

    captured: dict[str, Any] = {}

    def _on_create(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured["body"] = _json.loads(request.content.decode())
        return httpx.Response(
            201,
            json={
                "id": new_uuid,
                "name": "Backend docs coverage ratchet",
                "sequence_id": 99,
                "labels": [PIPELINE_DOC_ONLY_LABEL],
                "parent": None,
            },
        )

    respx.post(f"{base}/issues/").mock(side_effect=_on_create)
    respx.get(f"{base}/issues/{new_uuid}/").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": new_uuid,
                "name": "Backend docs coverage ratchet",
                "sequence_id": 99,
                "labels": [PIPELINE_DOC_ONLY_LABEL],
                "parent": None,
            },
        )
    )

    result = await create_root_issue(
        name="Backend docs coverage ratchet",
        description_html="<p>baseline 28.1% → 100% in 5%-per-iteration steps</p>",
        labels=["pipeline:doc-only"],
        workspace=ctx.config.workspace_slug,
    )

    assert result["id"] == new_uuid
    assert result["sequence_id"] == 99
    assert result["identifier"] == "TEST-99"
    assert result["workspace_slug"] == ctx.config.workspace_slug
    assert PIPELINE_DOC_ONLY_LABEL in result["labels"]

    # Body sent to Plane: parent absent (root), labels resolved to UUID,
    # description_html present.
    assert "parent" not in captured["body"]
    assert captured["body"]["labels"] == [PIPELINE_DOC_ONLY_LABEL]
    assert "baseline 28.1%" in captured["body"]["description_html"]


@respx.mock
async def test_create_root_issue_unknown_label_raises(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    """Symbolic label that the workspace doesn't have → fail before any POST."""
    with pytest.raises(LabelNotFoundError, match="not in cache"):
        await create_root_issue(
            name="Backend docs coverage ratchet",
            labels=["pipeline:nonexistent"],
            workspace=ctx.config.workspace_slug,
        )


@respx.mock
async def test_create_root_issue_unlabelled_post_condition_fails(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    """Same defense as create_sub_issue: if Plane silently drops labels,
    fail loudly so the issue isn't silently filed without its routing label."""
    ctx.label_by_name["pipeline:doc-only"] = PIPELINE_DOC_ONLY_LABEL
    base = f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/projects/{project_id}"
    new_uuid = "88888888-8888-8888-8888-888888888888"

    respx.post(f"{base}/issues/").mock(
        return_value=httpx.Response(201, json={"id": new_uuid, "labels": []})
    )
    respx.get(f"{base}/issues/{new_uuid}/").mock(
        return_value=httpx.Response(
            200, json={"id": new_uuid, "labels": [], "parent": None, "sequence_id": 100}
        )
    )

    with pytest.raises(UnlabelledSubIssueError, match="silently dropped"):
        await create_root_issue(
            name="X",
            labels=["pipeline:doc-only"],
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


@respx.mock
async def test_create_sub_issue_concurrent_calls_serialize(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    """Two concurrent create_sub_issue calls for the same (root, role) must
    NOT both create. The per-(workspace, root, role) lock serializes the
    list+create span so the second caller sees the first one's sub-issue and
    raises DuplicateSubIssueError instead of producing a second sub.
    """
    base = f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/projects/{project_id}"

    # State machine that mirrors real Plane: list reflects whatever has been
    # POSTed so far. Without a serializing lock, two concurrent calls would
    # both list (see empty) and both POST → a real duplicate.
    state: dict[str, list[dict[str, Any]]] = {"created": []}
    create_calls = {"n": 0}

    def _list(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": list(state["created"])})

    def _create(_: httpx.Request) -> httpx.Response:
        create_calls["n"] += 1
        state["created"].append({"id": SPEC_SUB_UUID, "parent": ROOT_UUID, "labels": [LABEL_SPEC]})
        return httpx.Response(
            201,
            json={"id": SPEC_SUB_UUID, "name": "SPEC: …", "labels": [LABEL_SPEC]},
        )

    respx.get(f"{base}/issues/").mock(side_effect=_list)
    respx.get(f"{base}/issues/{ROOT_UUID}/").mock(
        return_value=httpx.Response(
            200, json={"id": ROOT_UUID, "name": "Add user dashboard", "sequence_id": 42}
        )
    )

    respx.post(f"{base}/issues/").mock(side_effect=_create)
    respx.get(f"{base}/issues/{SPEC_SUB_UUID}/").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": SPEC_SUB_UUID,
                "labels": [LABEL_SPEC],
                "parent": ROOT_UUID,
                "sequence_id": 43,
            },
        )
    )

    async def _attempt() -> dict[str, Any] | TowerError:
        try:
            return await create_sub_issue(
                role="spec",
                root_uuid=ROOT_UUID,
                workspace=ctx.config.workspace_slug,
            )
        except TowerError as exc:
            return exc

    results = await asyncio.gather(_attempt(), _attempt())
    successes = [r for r in results if isinstance(r, dict)]
    failures = [r for r in results if isinstance(r, DuplicateSubIssueError)]
    assert len(successes) == 1, f"expected exactly one create, got {results}"
    assert len(failures) == 1
    assert create_calls["n"] == 1, f"lock failed: POST /issues/ called {create_calls['n']} times"


# ---------------------------------------------------------------------------
# post_review — iteration counter + verdict validation
# ---------------------------------------------------------------------------


@respx.mock
async def test_post_review_stamps_iter_n_passed_by_caller(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    """Caller passes iter_n (derived from comments they already read via
    read_artifact) — tower does not walk all comments to auto-detect."""
    base = f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/projects/{project_id}"
    posted: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        posted["body"] = request.content.decode()
        return httpx.Response(201, json={"id": "new-comment-id"})

    respx.post(f"{base}/issues/{SPEC_SUB_UUID}/comments/").mock(side_effect=_capture)

    result = await post_review(
        sub_uuid=SPEC_SUB_UUID,
        verdict="approved",
        body_html="<p>fixed</p>",
        iter_n=3,
        workspace=ctx.config.workspace_slug,
    )
    assert result["iter"] == 3
    assert result["verdict"] == "APPROVED"
    assert result["sub_uuid"] == SPEC_SUB_UUID
    assert "REVIEW (iter 3) — APPROVED" in posted["body"]


@respx.mock
async def test_post_review_defaults_to_iter_1(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    base = f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/projects/{project_id}"
    respx.post(f"{base}/issues/{SPEC_SUB_UUID}/comments/").mock(
        return_value=httpx.Response(201, json={"id": "c-new"})
    )
    result = await post_review(
        sub_uuid=SPEC_SUB_UUID,
        verdict="CHANGES_REQUIRED",
        body_html="<p>findings</p>",
        workspace=ctx.config.workspace_slug,
    )
    assert result["iter"] == 1


async def test_post_review_invalid_verdict_raises(
    registry: TowerRegistry, ctx: WorkspaceContext
) -> None:
    with pytest.raises(TowerError, match="verdict must be"):
        await post_review(
            sub_uuid=SPEC_SUB_UUID,
            verdict="MAYBE",
            body_html="",
            workspace=ctx.config.workspace_slug,
        )


async def test_post_review_invalid_iter_raises(
    registry: TowerRegistry, ctx: WorkspaceContext
) -> None:
    with pytest.raises(TowerError, match="iter_n must be"):
        await post_review(
            sub_uuid=SPEC_SUB_UUID,
            verdict="APPROVED",
            body_html="",
            iter_n=0,
            workspace=ctx.config.workspace_slug,
        )


# ---------------------------------------------------------------------------
# post_changes — OpenAPI defense
# ---------------------------------------------------------------------------


async def test_post_changes_refuses_ready_without_openapi_when_views_touched(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    """Defense fires BEFORE any HTTP — no Plane mock needed."""
    with pytest.raises(TowerError, match="API documentation defense"):
        await post_changes(
            sub_uuid=BACKEND_SUB_UUID,
            target="backend",
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
    respx.post(f"{base}/issues/{BACKEND_SUB_UUID}/comments/").mock(
        return_value=httpx.Response(201, json={"id": "c-new"})
    )
    result = await post_changes(
        sub_uuid=BACKEND_SUB_UUID,
        target="backend",
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
    respx.post(f"{base}/issues/{BACKEND_SUB_UUID}/comments/").mock(
        return_value=httpx.Response(201, json={"id": "c-new"})
    )
    # No views/serializers touched → openapi check not required
    result = await post_changes(
        sub_uuid=BACKEND_SUB_UUID,
        target="backend",
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
# mark_spec_approved — posts SPEC_APPROVED marker; caller attests the prior
# REVIEW was APPROVED (tower no longer walks comments to verify — that was
# a hang source). The architect is who calls this, right after they posted
# the APPROVED review themselves.
# ---------------------------------------------------------------------------


@respx.mock
async def test_mark_spec_approved_posts_marker(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    base = f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/projects/{project_id}"
    captured: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(201, json={"id": "c-spec-approved"})

    respx.post(f"{base}/issues/{SPEC_SUB_UUID}/comments/").mock(side_effect=_capture)

    result = await mark_spec_approved(
        spec_sub_uuid=SPEC_SUB_UUID,
        summary_html="<p>scope: backend only</p>",
        workspace=ctx.config.workspace_slug,
    )
    assert result == {"comment_id": "c-spec-approved"}
    assert "SPEC_APPROVED" in captured["body"]


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


# ---------------------------------------------------------------------------
# Mention routing — role resolver, body-scan defense, request_handoff
# ---------------------------------------------------------------------------


RINZLER_MEMBER = "55555555-5555-5555-5555-555555555555"
GEM_MEMBER = "66666666-6666-6666-6666-666666666666"


@pytest.fixture
def ctx_with_all_members(ctx: WorkspaceContext) -> WorkspaceContext:
    """Extend the default ctx with rinzler+gem so role resolution tests
    cover all three agents in the workspace_config fixture roster."""
    ctx.member_by_nickname["rinzler"] = RINZLER_MEMBER
    ctx.member_by_nickname["gem"] = GEM_MEMBER
    ctx.member_by_email["rinzler@example.io"] = RINZLER_MEMBER
    ctx.member_by_email["gem@example.io"] = GEM_MEMBER
    return ctx


def test_role_mention_resolves_by_prompt_role(ctx_with_all_members: WorkspaceContext) -> None:
    """system-analyst → sark in the test fixture roster → SARK_MEMBER UUID."""
    html = _role_mention(ctx_with_all_members, "system-analyst")
    assert SARK_MEMBER in html
    assert "<mention-component" in html


def test_role_mention_strips_namespace_prefix(ctx_with_all_members: WorkspaceContext) -> None:
    """`sdlc-agents:python-developer` and `python-developer` should both resolve."""
    bare = _role_mention(ctx_with_all_members, "python-developer")
    namespaced = _role_mention(ctx_with_all_members, "sdlc-agents:python-developer")
    assert RINZLER_MEMBER in bare
    assert RINZLER_MEMBER in namespaced


def test_role_mention_unknown_role_raises(ctx_with_all_members: WorkspaceContext) -> None:
    """Role with no agent in the workspace roster fails fast."""
    with pytest.raises(RoleNotFoundError, match="no agent registered"):
        _role_mention(ctx_with_all_members, "architect")


def test_role_mention_role_with_uncached_member_raises(ctx: WorkspaceContext) -> None:
    """Role exists in roster but its nickname is not in the member cache."""
    # ctx (without ctx_with_all_members extension) has no rinzler in member_by_nickname.
    with pytest.raises(RoleNotFoundError, match="not in the project member cache"):
        _role_mention(ctx, "python-developer")


def test_assert_no_mentions_blocks_html() -> None:
    with pytest.raises(MentionInBodyError, match="tower-managed"):
        _assert_no_mentions(
            '<p>I am done. <mention-component entity_identifier="abc" '
            'entity_name="user_mention"/></p>'
        )


def test_assert_no_mentions_passes_clean_html() -> None:
    _assert_no_mentions("<p>nothing to see here</p>")
    _assert_no_mentions("")
    _assert_no_mentions(None)


@respx.mock
async def test_post_comment_refuses_embedded_mention(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    """Defense-in-depth: free-form post_comment cannot smuggle a mention."""
    with pytest.raises(MentionInBodyError):
        await post_comment(
            work_item_uuid=ROOT_UUID,
            comment_html=(
                '<p>review please <mention-component entity_identifier="x" '
                'entity_name="user_mention"/></p>'
            ),
            workspace=ctx.config.workspace_slug,
        )


@respx.mock
async def test_request_handoff_happy_path(
    registry: TowerRegistry, ctx_with_all_members: WorkspaceContext, project_id: str
) -> None:
    base = f"https://plane.test/api/v1/workspaces/{ctx_with_all_members.config.workspace_slug}/projects/{project_id}"
    captured: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(201, json={"id": "c-handoff"})

    respx.post(f"{base}/issues/{BACKEND_SUB_UUID}/comments/").mock(side_effect=_capture)

    result = await request_handoff(
        sub_uuid=BACKEND_SUB_UUID,
        target_role="system-analyst",
        message_html="ready for SPEC review",
        workspace=ctx_with_all_members.config.workspace_slug,
    )
    assert result["comment_id"] == "c-handoff"
    assert result["target_role"] == "system-analyst"
    assert result["target_uuid"] == SARK_MEMBER
    assert SARK_MEMBER in captured["body"]
    assert "ready for SPEC review" in captured["body"]


@respx.mock
async def test_request_handoff_unknown_role_raises(
    registry: TowerRegistry, ctx: WorkspaceContext
) -> None:
    """Unknown target_role refuses BEFORE any POST."""
    with pytest.raises(RoleNotFoundError, match="no agent registered"):
        await request_handoff(
            sub_uuid=BACKEND_SUB_UUID,
            target_role="reviewer",  # not in the test workspace roster
            workspace=ctx.config.workspace_slug,
        )


@respx.mock
async def test_request_handoff_initiator_special_case(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    """target_role='initiator' resolves to ctx.config.initiator_uuid — used
    by startup comments / blocking-question / summary handoffs where the
    agent pings the human, not another bot."""
    base = f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/projects/{project_id}"
    captured: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(201, json={"id": "c-startup"})

    respx.post(f"{base}/issues/{BACKEND_SUB_UUID}/comments/").mock(side_effect=_capture)

    result = await request_handoff(
        sub_uuid=BACKEND_SUB_UUID,
        target_role="initiator",
        message_html="picked up. reading SPEC.",
        workspace=ctx.config.workspace_slug,
    )
    assert result["target_role"] == "initiator"
    assert result["target_uuid"] == str(ctx.config.initiator_uuid)
    assert str(ctx.config.initiator_uuid) in captured["body"]


async def test_request_handoff_refuses_mention_in_message(
    registry: TowerRegistry, ctx_with_all_members: WorkspaceContext
) -> None:
    """Even the structured tool refuses a hand-typed mention in message_html."""
    with pytest.raises(MentionInBodyError):
        await request_handoff(
            sub_uuid=BACKEND_SUB_UUID,
            target_role="system-analyst",
            message_html=(
                'thanks <mention-component entity_identifier="x" entity_name="user_mention"/>'
            ),
            workspace=ctx_with_all_members.config.workspace_slug,
        )


@respx.mock
async def test_post_changes_with_next_role_stamps_mention(
    registry: TowerRegistry, ctx_with_all_members: WorkspaceContext, project_id: str
) -> None:
    """post_changes(next_role=...) injects the next-role mention alongside initiator."""
    base = f"https://plane.test/api/v1/workspaces/{ctx_with_all_members.config.workspace_slug}/projects/{project_id}"
    captured: dict[str, Any] = {}

    def _on_post(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(201, json={"id": "c-changes"})

    respx.post(f"{base}/issues/{BACKEND_SUB_UUID}/comments/").mock(side_effect=_on_post)

    await post_changes(
        sub_uuid=BACKEND_SUB_UUID,
        target="backend",
        summary="all done",
        files=[["src/foo.py", "added bar"]],
        verification=[["pytest", "passed"]],
        ready_for_review=True,
        next_role="ui-tester",
        workspace=ctx_with_all_members.config.workspace_slug,
    )
    assert GEM_MEMBER in captured["body"]
    assert str(ctx_with_all_members.config.initiator_uuid) in captured["body"]


# ---------------------------------------------------------------------------
# list_comments — comments-only pagination (no description re-fetch)
# ---------------------------------------------------------------------------


@respx.mock
async def test_list_comments_returns_newest_first_with_pagination(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    """list_comments: newest-first, sliced by limit/offset, no description GET."""
    base = f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/projects/{project_id}"
    comments_route = respx.get(f"{base}/issues/{SPEC_SUB_UUID}/comments/").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": f"c{i}",
                        "comment_html": f"<p>n{i}</p>",
                        "created_at": f"2026-05-10T00:{i:02d}:00Z",
                        "created_by": "u1",
                    }
                    for i in range(10)
                ]
            },
        )
    )

    result = await list_comments(
        sub_uuid=SPEC_SUB_UUID, limit=3, workspace=ctx.config.workspace_slug
    )

    assert result["total"] == 10
    assert result["returned"] == 3
    assert result["offset"] == 0
    assert result["has_more"] is True
    assert result["order"] == "desc"
    # newest first: c9, c8, c7
    assert [c["id"] for c in result["comments"]] == ["c9", "c8", "c7"]
    # No GET on the issue itself — only the comments endpoint was hit.
    assert comments_route.called


@respx.mock
async def test_list_comments_offset_walks_to_eof(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    base = f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/projects/{project_id}"
    respx.get(f"{base}/issues/{SPEC_SUB_UUID}/comments/").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": f"c{i}", "comment_html": "", "created_at": f"2026-05-10T00:{i:02d}:00Z"}
                    for i in range(5)
                ]
            },
        )
    )
    result = await list_comments(
        sub_uuid=SPEC_SUB_UUID, limit=10, offset=3, workspace=ctx.config.workspace_slug
    )
    assert result["total"] == 5
    assert result["returned"] == 2  # 5 - 3
    assert result["has_more"] is False


async def test_list_comments_rejects_negative_pagination(
    registry: TowerRegistry, ctx: WorkspaceContext
) -> None:
    with pytest.raises(TowerError, match="non-negative"):
        await list_comments(sub_uuid=SPEC_SUB_UUID, offset=-1, workspace=ctx.config.workspace_slug)


# ---------------------------------------------------------------------------
# read_artifact + html_to_markdown
# ---------------------------------------------------------------------------


def test_html_to_markdown_strips_to_compact_text() -> None:
    """Plane editor HTML has class/style noise on every tag — the markdown
    output should drop the markup but keep heading/list/emphasis structure,
    so the agent's reasoning pass still sees the document outline."""
    from plane_conductor.mcp_tower import html_to_markdown

    html_input = (
        '<h2 class="x" style="margin:0">Phase 1</h2>'
        '<p class="x">Scope: <strong>customers-app</strong> with '
        '<a href="https://example.io">spec</a>.</p>'
        '<ul class="x"><li>Item one</li><li>Item two</li></ul>'
        "<pre><code>def f(): pass</code></pre>"
    )
    md = html_to_markdown(html_input)

    assert "## Phase 1" in md
    assert "**customers-app**" in md
    assert "[spec](https://example.io)" in md
    assert "- Item one" in md
    assert "- Item two" in md
    assert "```\ndef f(): pass\n```" in md
    # No stray HTML tags or attributes
    assert "<" not in md and "class=" not in md and "style=" not in md
    # Markdown is materially smaller than the input
    assert len(md) < len(html_input)


def test_html_to_markdown_empty_input() -> None:
    from plane_conductor.mcp_tower import html_to_markdown

    assert html_to_markdown("") == ""
    assert html_to_markdown(None) == ""  # type: ignore[arg-type]


def _read_artifact_setup(
    ctx: WorkspaceContext,
    project_id: str,
    *,
    description_html: str,
    comments: list[dict[str, Any]],
) -> None:
    base = f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/projects/{project_id}"
    respx.get(f"{base}/issues/{SPEC_SUB_UUID}/").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": SPEC_SUB_UUID,
                "name": "SPEC sub",
                "description_html": description_html,
                "labels": [LABEL_SPEC],
                "state": "in-progress",
                "updated_at": "2026-05-10T00:00:00Z",
            },
        )
    )
    respx.get(f"{base}/issues/{SPEC_SUB_UUID}/comments/").mock(
        return_value=httpx.Response(200, json={"results": comments})
    )


@respx.mock
async def test_read_artifact_default_returns_markdown(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    """Default `description_format='markdown'` — agents read here, so the
    big SPECs no longer blow the MCP tool-result token cap."""
    from plane_conductor.mcp_tower import read_artifact

    _read_artifact_setup(
        ctx,
        project_id,
        description_html="<h1>SPEC</h1><p>body with <strong>bold</strong></p>",
        comments=[],
    )

    result = await read_artifact(SPEC_SUB_UUID, workspace=ctx.config.workspace_slug)

    assert result["description_format"] == "markdown"
    assert "# SPEC" in result["description"]
    assert "**bold**" in result["description"]
    assert "<" not in result["description"]
    assert result["description_size_chars"] == len(result["description"])
    # No pagination requested → whole document, no more chunks.
    assert result["description_offset"] == 0
    assert result["description_returned_chars"] == len(result["description"])
    assert result["description_has_more"] is False
    # Old key removed — callers must adapt; document the breaking change.
    assert "description_html" not in result


@respx.mock
async def test_read_artifact_html_format_returns_raw(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    """`description_format='html'` is the escape hatch — full markup, for
    callers that genuinely need the HTML (e.g. relaying it back to Plane)."""
    from plane_conductor.mcp_tower import read_artifact

    raw = '<p class="x">verbatim</p>'
    _read_artifact_setup(ctx, project_id, description_html=raw, comments=[])

    result = await read_artifact(
        SPEC_SUB_UUID,
        description_format="html",
        workspace=ctx.config.workspace_slug,
    )

    assert result["description_format"] == "html"
    assert result["description"] == raw


@respx.mock
async def test_read_artifact_comments_default_last_5_newest_first(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    """Comments come back newest-first; default limit=5 picks the last 5,
    which is the re-entry slice — older comments are heartbeat noise that
    agents don't need to scan linearly (structured tools handle history)."""
    from plane_conductor.mcp_tower import read_artifact

    comments = [
        {
            "id": f"c{i}",
            "comment_html": f"<p>n{i}</p>",
            "created_at": f"2026-05-10T00:{i:02d}:00Z",
            "created_by": "u1",
        }
        for i in range(25)
    ]
    _read_artifact_setup(ctx, project_id, description_html="", comments=comments)

    result = await read_artifact(SPEC_SUB_UUID, workspace=ctx.config.workspace_slug)

    assert result["total_comments"] == 25
    assert result["comments_returned"] == 5
    assert result["comments_offset"] == 0
    assert result["has_more_comments"] is True
    assert result["comments_order"] == "desc"
    # Newest first → c24, c23, c22, c21, c20
    assert [c["id"] for c in result["comments"]] == ["c24", "c23", "c22", "c21", "c20"]


@respx.mock
async def test_read_artifact_comments_offset_paginates_into_older(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    """offset=20 with default limit=20 returns the next-older slice."""
    from plane_conductor.mcp_tower import read_artifact

    comments = [
        {
            "id": f"c{i}",
            "comment_html": f"<p>n{i}</p>",
            "created_at": f"2026-05-10T00:{i:02d}:00Z",
        }
        for i in range(25)
    ]
    _read_artifact_setup(ctx, project_id, description_html="", comments=comments)

    result = await read_artifact(
        SPEC_SUB_UUID,
        comments_offset=20,
        workspace=ctx.config.workspace_slug,
    )

    assert result["comments_returned"] == 5
    assert result["has_more_comments"] is False
    # Oldest 5 in desc order → c4, c3, c2, c1, c0
    assert [c["id"] for c in result["comments"]] == ["c4", "c3", "c2", "c1", "c0"]


@respx.mock
async def test_read_artifact_comments_limit_zero_returns_empty(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    """Reading just the description without paying for comments."""
    from plane_conductor.mcp_tower import read_artifact

    comments = [{"id": "c0", "comment_html": "<p>x</p>", "created_at": "2026-05-10T00:00:00Z"}]
    _read_artifact_setup(ctx, project_id, description_html="<p>body</p>", comments=comments)

    result = await read_artifact(
        SPEC_SUB_UUID, comments_limit=0, workspace=ctx.config.workspace_slug
    )

    assert result["comments"] == []
    assert result["total_comments"] == 1
    assert result["has_more_comments"] is True


@respx.mock
async def test_read_artifact_rejects_bad_format(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    from plane_conductor.mcp_tower import read_artifact

    with pytest.raises(TowerError, match="description_format"):
        await read_artifact(
            SPEC_SUB_UUID,
            description_format="plaintext",
            workspace=ctx.config.workspace_slug,
        )


@respx.mock
async def test_read_artifact_rejects_negative_pagination(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    from plane_conductor.mcp_tower import read_artifact

    with pytest.raises(TowerError, match="non-negative"):
        await read_artifact(SPEC_SUB_UUID, comments_offset=-1, workspace=ctx.config.workspace_slug)


@respx.mock
async def test_read_artifact_description_limit_returns_head_chunk(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    """`description_limit` slices the rendered Markdown by chars. `has_more`
    fires when the cap is below the full size — this is the walk-a-huge-SPEC
    escape hatch."""
    from plane_conductor.mcp_tower import read_artifact

    # html_to_markdown of <p>xxxx…</p> strips the tags, leaving the body chars.
    body = "x" * 300
    _read_artifact_setup(ctx, project_id, description_html=f"<p>{body}</p>", comments=[])

    result = await read_artifact(
        SPEC_SUB_UUID, description_limit=100, workspace=ctx.config.workspace_slug
    )

    assert result["description_size_chars"] == 300
    assert result["description_offset"] == 0
    assert result["description_returned_chars"] == 100
    assert result["description"] == "x" * 100
    assert result["description_has_more"] is True


@respx.mock
async def test_read_artifact_description_offset_walks_to_eof(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    """Caller bumps offset by `description_returned_chars` and re-calls until
    `description_has_more` is False — that's the documented walk pattern."""
    from plane_conductor.mcp_tower import read_artifact

    body = "x" * 300
    _read_artifact_setup(ctx, project_id, description_html=f"<p>{body}</p>", comments=[])

    chunks: list[str] = []
    offset = 0
    while True:
        result = await read_artifact(
            SPEC_SUB_UUID,
            description_offset=offset,
            description_limit=100,
            workspace=ctx.config.workspace_slug,
        )
        chunks.append(result["description"])
        if not result["description_has_more"]:
            break
        offset += result["description_returned_chars"]
        assert len(chunks) < 10, "loop should terminate well before 10 iters"

    assert "".join(chunks) == "x" * 300
    assert offset + len(chunks[-1]) == 300


@respx.mock
async def test_read_artifact_description_offset_past_end_returns_empty(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    """Offset beyond document size → empty string, `has_more=False`. The
    caller's loop already exits before this, but the tool must not raise."""
    from plane_conductor.mcp_tower import read_artifact

    _read_artifact_setup(ctx, project_id, description_html="<p>short</p>", comments=[])

    result = await read_artifact(
        SPEC_SUB_UUID,
        description_offset=10_000,
        workspace=ctx.config.workspace_slug,
    )

    assert result["description"] == ""
    assert result["description_returned_chars"] == 0
    assert result["description_has_more"] is False
    # Full size is still reported — it's the document size, not the slice size.
    assert result["description_size_chars"] == len("short")


@respx.mock
async def test_read_artifact_rejects_negative_description_pagination(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    from plane_conductor.mcp_tower import read_artifact

    with pytest.raises(TowerError, match="description_offset"):
        await read_artifact(
            SPEC_SUB_UUID, description_offset=-1, workspace=ctx.config.workspace_slug
        )
    with pytest.raises(TowerError, match="description_limit"):
        await read_artifact(
            SPEC_SUB_UUID, description_limit=-1, workspace=ctx.config.workspace_slug
        )


# ---------------------------------------------------------------------------
# description size feedback (soft limit signal)
# ---------------------------------------------------------------------------


@respx.mock
async def test_update_sub_issue_description_reports_size_under_limit(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    """Author sees size on every write — small body, no warning."""
    from plane_conductor.mcp_tower import (
        DESCRIPTION_SOFT_LIMIT_CHARS,
        update_sub_issue_description,
    )

    base = f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/projects/{project_id}"
    respx.patch(f"{base}/issues/{SPEC_SUB_UUID}/").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": SPEC_SUB_UUID,
                "sequence_id": 38,
                "name": "SPEC",
                "parent": ROOT_UUID,
                "labels": [LABEL_SPEC],
                "state": "in-progress",
            },
        )
    )

    result = await update_sub_issue_description(
        SPEC_SUB_UUID,
        "<p>short body</p>",
        workspace=ctx.config.workspace_slug,
    )

    assert result["description_size_chars"] == len("short body")
    assert result["description_soft_limit_chars"] == DESCRIPTION_SOFT_LIMIT_CHARS
    assert result["description_size_warning"] is False


@respx.mock
async def test_update_sub_issue_description_warns_over_soft_limit(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    """The conciseness signal: write past soft cap → flag flips. Tower does not
    reject — the author still gets to write, but every subsequent read also
    surfaces the warning so it cannot be missed."""
    from plane_conductor.mcp_tower import (
        DESCRIPTION_SOFT_LIMIT_CHARS,
        update_sub_issue_description,
    )

    base = f"https://plane.test/api/v1/workspaces/{ctx.config.workspace_slug}/projects/{project_id}"
    respx.patch(f"{base}/issues/{SPEC_SUB_UUID}/").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": SPEC_SUB_UUID,
                "sequence_id": 38,
                "name": "SPEC",
                "parent": ROOT_UUID,
                "labels": [LABEL_SPEC],
                "state": "in-progress",
            },
        )
    )

    fat = "<p>" + ("x" * (DESCRIPTION_SOFT_LIMIT_CHARS + 100)) + "</p>"
    result = await update_sub_issue_description(
        SPEC_SUB_UUID, fat, workspace=ctx.config.workspace_slug
    )

    assert result["description_size_chars"] > DESCRIPTION_SOFT_LIMIT_CHARS
    assert result["description_size_warning"] is True


@respx.mock
async def test_read_artifact_surfaces_size_warning(
    registry: TowerRegistry, ctx: WorkspaceContext, project_id: str
) -> None:
    """Read path carries the same signal — reviewer / architect sees a
    bloated SPEC's size on every read, not only at write time."""
    from plane_conductor.mcp_tower import DESCRIPTION_SOFT_LIMIT_CHARS, read_artifact

    fat_html = "<p>" + ("x" * (DESCRIPTION_SOFT_LIMIT_CHARS + 100)) + "</p>"
    _read_artifact_setup(ctx, project_id, description_html=fat_html, comments=[])

    result = await read_artifact(SPEC_SUB_UUID, workspace=ctx.config.workspace_slug)

    assert result["description_size_chars"] > DESCRIPTION_SOFT_LIMIT_CHARS
    assert result["description_soft_limit_chars"] == DESCRIPTION_SOFT_LIMIT_CHARS
    assert result["description_size_warning"] is True
