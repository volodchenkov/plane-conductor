"""plane-tower — virtual MCP layer over the per-workspace Plane MCPs.

The pipeline-protocol tools (create_sub_issue, post_review, post_changes, ...)
live here as actual MCP tools rather than as discipline rules in agent prompts.
Each tool resolves the workspace from the call context (env / identifier /
explicit), enforces invariants (one-sub-per-role, label-non-empty, post-create
asserts, iteration markers, etc.), and routes the underlying Plane REST call
with the right per-workspace API key.

Run via the `plane-conductor-tower` console script (stdio MCP transport).
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from mcp.server.fastmcp import FastMCP

from plane_conductor.conductor_config import (
    WorkspaceConfig,
    load_workspaces,
)
from plane_conductor.exceptions import PlaneAPIError
from plane_conductor.plane_client import PlaneClient

logger = logging.getLogger(__name__)


def _esc(value: Any) -> str:
    """HTML-escape a fragment of caller-controlled text before interpolating
    it into a Plane comment template. Use for paths, names, summaries, error
    messages — anything that came from the agent and is not itself a
    documented HTML field (body_html, description_html, comment_html,
    summary_html stay verbatim)."""
    return html.escape(str(value), quote=True)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TowerError(Exception):
    """Base for plane-tower invariant violations and routing failures."""


class WorkspaceNotResolvedError(TowerError):
    """The MCP could not figure out which workspace this call belongs to."""


class LabelNotFoundError(TowerError):
    """A symbolic label (e.g. 'spec') has no corresponding UUID in the workspace."""


class DuplicateSubIssueError(TowerError):
    """Hard violation of the one-sub-per-role-per-root invariant."""


class UnlabelledSubIssueError(TowerError):
    """Plane silently dropped the labels list during create — fail loudly."""


# ---------------------------------------------------------------------------
# Workspace registry — load conductor.d/*.yaml + Plane constants at boot
# ---------------------------------------------------------------------------


# Maps the role label suffix used by agents → the artifact label name in Plane.
ARTIFACT_LABEL_BY_ROLE: dict[str, str] = {
    "spec": "artifact:spec",
    "design": "artifact:design",
    "backend": "artifact:backend",
    "frontend": "artifact:frontend",
    "api-tests": "artifact:api-testing",
    "ux-tests": "artifact:ux-testing",
    "requirements": "artifact:requirements",
}

PIPELINE_DOC_ONLY_LABEL = "pipeline:doc-only"


@dataclass
class WorkspaceContext:
    """In-memory snapshot of a workspace's config + Plane constants."""

    config: WorkspaceConfig
    project_identifier: str  # e.g. "COIN" — the prefix in <IDENT>-<N> issue keys
    project_name: str
    label_by_name: dict[str, str] = field(default_factory=dict)
    state_by_group: dict[str, str] = field(default_factory=dict)  # backlog/cancelled/done
    state_by_name: dict[str, str] = field(default_factory=dict)
    member_by_email: dict[str, str] = field(default_factory=dict)
    member_by_nickname: dict[str, str] = field(default_factory=dict)

    def label_uuid(self, label_name: str) -> str:
        uuid = self.label_by_name.get(label_name)
        if not uuid:
            raise LabelNotFoundError(
                f"workspace {self.config.workspace_slug!r}: label "
                f"{label_name!r} not in cache. Either the label was never "
                f"created (run `plane-conductor setup`), or you mistyped the "
                f"role. Known artifact labels: "
                f"{sorted(n for n in self.label_by_name if n.startswith('artifact:'))}"
            )
        return uuid

    def artifact_label_uuid(self, role: str) -> str:
        label_name = ARTIFACT_LABEL_BY_ROLE.get(role)
        if not label_name:
            raise LabelNotFoundError(
                f"unknown role {role!r}; supported: {sorted(ARTIFACT_LABEL_BY_ROLE)}"
            )
        return self.label_uuid(label_name)

    def member_uuid(self, identifier: str) -> str:
        """Resolve a member by nickname (preferred) or email."""
        ident = identifier.strip().lower()
        if ident in self.member_by_nickname:
            return self.member_by_nickname[ident]
        if ident in self.member_by_email:
            return self.member_by_email[ident]
        raise TowerError(
            f"workspace {self.config.workspace_slug!r}: no member matches "
            f"{identifier!r} (nickname or email)"
        )


@dataclass
class TowerRegistry:
    """All workspaces this tower instance knows about."""

    by_slug: dict[str, WorkspaceContext] = field(default_factory=dict)
    by_project_id: dict[str, WorkspaceContext] = field(default_factory=dict)
    by_project_identifier: dict[str, WorkspaceContext] = field(default_factory=dict)

    def slugs(self) -> list[str]:
        return sorted(self.by_slug)

    def resolve(
        self,
        *,
        workspace: str | None = None,
        root_uuid: str | None = None,
        project_identifier: str | None = None,
    ) -> WorkspaceContext:
        """Determine the workspace for this call, in priority order."""
        if workspace:
            if workspace in self.by_slug:
                return self.by_slug[workspace]
            raise WorkspaceNotResolvedError(
                f"explicit workspace={workspace!r} not registered. known: {self.slugs()}"
            )
        env_slug = os.environ.get("WORKSPACE_SLUG", "").strip().lower()
        if env_slug:
            if env_slug in self.by_slug:
                return self.by_slug[env_slug]
            raise WorkspaceNotResolvedError(
                f"WORKSPACE_SLUG env={env_slug!r} not registered. known: {self.slugs()}"
            )
        if project_identifier:
            ctx = self.by_project_identifier.get(project_identifier.upper())
            if ctx:
                return ctx
        # Note: a UUID-based fallback would require an API probe per workspace;
        # we don't auto-resolve from root_uuid alone — caller passes workspace
        # explicitly or via WORKSPACE_SLUG env.
        raise WorkspaceNotResolvedError(
            "cannot determine workspace: pass workspace=… explicitly, "
            "or set WORKSPACE_SLUG env, or include a known PROJECT_IDENTIFIER "
            f"prefix. Registered: {self.slugs()}"
        )


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


async def _hydrate_workspace(config: WorkspaceConfig) -> WorkspaceContext:
    """Fetch labels / states / members for one workspace and snapshot in memory."""
    async with PlaneClient(
        config.plane_base_url,
        config.plane_api_key,
        config.workspace_slug,
    ) as plane:
        project, labels, states, members = await asyncio.gather(
            plane.get_project(config.project_id),
            plane.list_labels(config.project_id),
            plane.list_states(config.project_id),
            plane.list_project_members(config.project_id),
        )

    ctx = WorkspaceContext(
        config=config,
        project_identifier=str(project.get("identifier") or "").upper(),
        project_name=str(project.get("name") or ""),
    )
    for lbl in labels:
        name = str(lbl.get("name") or "").strip()
        uuid = str(lbl.get("id") or "")
        if name and uuid:
            ctx.label_by_name[name] = uuid
    for state in states:
        name = str(state.get("name") or "")
        group = str(state.get("group") or "")
        uuid = str(state.get("id") or "")
        if uuid:
            if name:
                ctx.state_by_name[name] = uuid
            if group and group not in ctx.state_by_group:
                ctx.state_by_group[group] = uuid  # first state in group wins
    for m in members:
        inner = m.get("member") if isinstance(m.get("member"), dict) else m
        uuid = str(inner.get("id") or "")
        email = str(inner.get("email") or "").strip().lower()
        if uuid and email:
            ctx.member_by_email[email] = uuid
            local = email.split("@", 1)[0]
            ctx.member_by_nickname[local] = uuid

    return ctx


async def build_registry(conductor_dir: Path | str) -> TowerRegistry:
    """Load every conductor.d/*.yaml and snapshot all Plane constants.

    Hydration runs per-workspace in parallel with `return_exceptions=True`:
    one bad workspace (unreachable Plane, revoked key, transient 5xx) is
    logged and skipped, not allowed to take down the whole tower for every
    other tenant. Raises only if EVERY workspace failed to hydrate.
    """
    workspaces = load_workspaces(Path(conductor_dir))
    registry = TowerRegistry()
    ws_list = list(workspaces.values())
    if not ws_list:
        return registry
    results = await asyncio.gather(
        *(_hydrate_workspace(ws) for ws in ws_list),
        return_exceptions=True,
    )
    failures: list[tuple[str, BaseException]] = []
    for ws, result in zip(ws_list, results, strict=True):
        if isinstance(result, BaseException):
            failures.append((ws.workspace_slug, result))
            logger.error(
                "plane-tower: failed to hydrate workspace %r: %s",
                ws.workspace_slug,
                result,
            )
            continue
        ctx = result
        registry.by_slug[ctx.config.workspace_slug] = ctx
        registry.by_project_id[str(ctx.config.project_id)] = ctx
        if ctx.project_identifier:
            registry.by_project_identifier[ctx.project_identifier] = ctx
    if not registry.by_slug and failures:
        # Every workspace failed — propagate the first error so the operator
        # sees a real traceback instead of an empty registry that 500s every
        # subsequent tool call.
        slug, exc = failures[0]
        raise TowerError(
            f"plane-tower: every workspace failed to hydrate "
            f"({len(failures)} total); first failure was {slug!r}: {exc}"
        ) from exc
    return registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_REVIEW_MARKER_RE = re.compile(
    # Matches the canonical post_review template:
    #   `<strong>{marker} (iter {N}) — {VERDICT}.</strong>`
    # Verdict group is optional so the regex still matches legacy comments
    # without a verdict suffix; it captures from a controlled enum, so
    # substring noise like "previously APPROVED" inside body_html cannot
    # produce a false positive.
    r"(?P<kind>REVIEW|ARCH_REVIEW)\s*\(iter\s+(?P<iter>\d+)\)"
    r"(?:\s*[—-]\s*(?P<verdict>APPROVED|CHANGES_REQUIRED|BLOCKED))?",
    re.IGNORECASE,
)
_INITIATOR_MENTION_TEMPLATE = (
    '<mention-component entity_identifier="{uuid}" entity_name="user_mention"></mention-component>'
)


def _initiator_mention(ctx: WorkspaceContext) -> str:
    return _INITIATOR_MENTION_TEMPLATE.format(uuid=str(ctx.config.initiator_uuid))


def _ensure_uuid(value: str | UUID, name: str) -> str:
    """Validate that `value` looks like a UUID; return its canonical str form."""
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError) as exc:
        raise TowerError(f"{name} is not a valid UUID: {value!r}") from exc


async def _client_for(ctx: WorkspaceContext) -> PlaneClient:
    return PlaneClient(
        ctx.config.plane_base_url,
        ctx.config.plane_api_key,
        ctx.config.workspace_slug,
    )


# ---------------------------------------------------------------------------
# FastMCP server + tools
# ---------------------------------------------------------------------------


mcp = FastMCP("plane-tower")
_REGISTRY: TowerRegistry | None = None


def _registry() -> TowerRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        raise TowerError("plane-tower registry is not initialised; call main()")
    return _REGISTRY


# ----- pickup_issue --------------------------------------------------------


@mcp.tool()
async def pickup_issue(
    issue_uuid: str | None = None,
    *,
    project_identifier: str | None = None,
    sequence_id: int | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Resolve an issue by UUID (preferred) or by `<PROJECT_IDENTIFIER>-<N>`.

    Returns the full work item record plus the resolved workspace slug. Pass
    `issue_uuid` when Plane Conductor's spawn prompt gave you the UUID
    directly (the common case). Use `project_identifier` + `sequence_id`
    when only a human identifier like `COIN-37` is at hand.
    """
    ctx = _registry().resolve(workspace=workspace, project_identifier=project_identifier)
    async with await _client_for(ctx) as plane:
        if issue_uuid:
            issue = await plane.get_issue(ctx.config.project_id, issue_uuid)
        elif sequence_id is not None:
            found = await plane.get_issue_by_sequence_id(ctx.config.project_id, sequence_id)
            if not found:
                raise TowerError(
                    f"no issue with sequence_id={sequence_id} in "
                    f"workspace {ctx.config.workspace_slug!r}"
                )
            issue = found
        else:
            raise TowerError("pass issue_uuid or sequence_id (with project_identifier)")
    return {
        "id": issue.get("id"),
        "name": issue.get("name"),
        "sequence_id": issue.get("sequence_id"),
        "project_id": str(ctx.config.project_id),
        "project_identifier": ctx.project_identifier,
        "workspace_slug": ctx.config.workspace_slug,
        "url": f"{ctx.config.plane_base_url}/{ctx.config.workspace_slug}/projects/"
        f"{ctx.config.project_id}/issues/{issue.get('id')}/",
        "parent": issue.get("parent"),
        "labels": issue.get("labels") or [],
        "state": issue.get("state"),
    }


# ----- find_artifact_by_label / list_sub_issues ----------------------------


@mcp.tool()
async def find_artifact_by_label(
    role: str,
    root_uuid: str,
    *,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Find the sub-issue with `artifact:<role>` label among root's children.

    Enforces the one-sub-per-role-per-root invariant: returns
    `{found: 1, sub_issue: {...}}` on a unique match, `{found: 0, sub_issue: null}`
    when the upstream artifact does not exist yet, and **raises** on duplicates.
    """
    ctx = _registry().resolve(workspace=workspace)
    label_uuid = ctx.artifact_label_uuid(role)
    root_uuid = _ensure_uuid(root_uuid, "root_uuid")
    async with await _client_for(ctx) as plane:
        items = await plane.list_issues(ctx.config.project_id)
    matched = [
        i
        for i in items
        if str(i.get("parent") or "") == root_uuid and label_uuid in (i.get("labels") or [])
    ]
    if len(matched) > 1:
        uuids = [str(i.get("id")) for i in matched]
        raise DuplicateSubIssueError(
            f"workspace {ctx.config.workspace_slug!r}: {len(matched)} sub-issues "
            f"under root {root_uuid} have label artifact:{role}. "
            f"Fatal consistency violation. UUIDs: {uuids}. "
            f"Initiator must merge manually before re-triggering."
        )
    sub = matched[0] if matched else None
    return {
        "found": len(matched),
        "sub_issue": _summarize_issue(sub) if sub else None,
    }


@mcp.tool()
async def list_sub_issues(
    root_uuid: str,
    *,
    workspace: str | None = None,
) -> list[dict[str, Any]]:
    """Return every direct child of root, regardless of label."""
    ctx = _registry().resolve(workspace=workspace)
    root_uuid = _ensure_uuid(root_uuid, "root_uuid")
    async with await _client_for(ctx) as plane:
        items = await plane.list_issues(ctx.config.project_id)
    children = [i for i in items if str(i.get("parent") or "") == root_uuid]
    children.sort(key=lambda i: i.get("sequence_id") or 0)
    return [_summarize_issue(c) for c in children]


def _summarize_issue(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "sequence_id": item.get("sequence_id"),
        "name": item.get("name"),
        "parent": item.get("parent"),
        "labels": item.get("labels") or [],
        "state": item.get("state"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


# ----- create_root_issue ---------------------------------------------------


@mcp.tool()
async def create_root_issue(
    name: str,
    *,
    description_html: str = "",
    labels: list[str] | None = None,
    assignee_nicknames: list[str] | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Create a top-level (parent-less) issue in the workspace's project.

    Counterpart to `create_sub_issue` — for filing brand-new work that the
    SDLC pipeline will then sub-issue into. Used by the project-manager
    (Tron) DELEGATE route to file new tasks without leaving the chat.

    Args:
      name: issue title.
      description_html: optional initial description (HTML allowed).
      labels: list of symbolic label names to apply (e.g.
        `["pipeline:doc-only"]`). Resolved against the workspace label
        cache; raises `LabelNotFoundError` if any name is unknown.
      assignee_nicknames: list of member nicknames (or emails). Resolved
        against the workspace member cache; raises `TowerError` if any is
        unknown.
      workspace: explicit workspace slug; otherwise resolved from the
        usual signals (env / project_identifier).

    Post-condition: re-reads the created issue and fails loudly if Plane
    silently dropped the labels list (same defense as create_sub_issue).

    Returns `{id, sequence_id, identifier, name, labels, ...}` where
    `identifier` is the canonical `<PROJECT_IDENTIFIER>-<N>` form.
    """
    ctx = _registry().resolve(workspace=workspace)
    label_uuids = [ctx.label_uuid(lbl) for lbl in (labels or [])]
    assignee_uuids = [ctx.member_uuid(n) for n in (assignee_nicknames or [])]

    async with await _client_for(ctx) as plane:
        created = await plane.create_issue(
            ctx.config.project_id,
            name=name,
            description_html=description_html or None,
            labels=label_uuids or None,
            assignees=assignee_uuids or None,
        )
        new_id = str(created.get("id") or "")
        if not new_id:
            raise TowerError("Plane returned no id for the created root issue")
        # Post-condition: re-read and verify labels stuck (same defense
        # as create_sub_issue — Plane has been observed silently dropping
        # labels arrays on bad UUIDs).
        verified = await plane.get_issue(ctx.config.project_id, new_id)
        if label_uuids:
            got = verified.get("labels") or []
            missing = [u for u in label_uuids if u not in got]
            if missing:
                raise UnlabelledSubIssueError(
                    f"created root issue {new_id} has labels={got} after "
                    f"create — expected {label_uuids}. Plane silently "
                    f"dropped one or more labels (likely a UUID typo). "
                    f"Manual intervention required."
                )

    seq = verified.get("sequence_id")
    identifier = f"{ctx.project_identifier}-{seq}" if ctx.project_identifier and seq else None
    return {
        **_summarize_issue(verified),
        "identifier": identifier,
        "workspace_slug": ctx.config.workspace_slug,
    }


# ----- create_sub_issue ----------------------------------------------------


# Per-(workspace, root, role) locks to serialize the list+create span within
# one tower process. Plane has no server-side unique constraint on
# (parent, label), so two concurrent `create_sub_issue` calls for the same
# (root, role) would otherwise both pass the duplicate-check and both create
# a sub-issue. The lock closes the TOCTOU window for the deployment shape we
# actually run (one tower process per host); cross-process safety would
# require server-side enforcement, which is outside our control.
_create_sub_issue_locks: dict[tuple[str, str, str], asyncio.Lock] = {}


def _create_lock_for(workspace_slug: str, root_uuid: str, role: str) -> asyncio.Lock:
    key = (workspace_slug, root_uuid, role)
    lock = _create_sub_issue_locks.get(key)
    if lock is None:
        # asyncio.Lock() construction is sync and dict.setdefault is atomic
        # under the GIL → safe without an outer guard. The first concurrent
        # creator wins; losers reuse its lock.
        lock = _create_sub_issue_locks.setdefault(key, asyncio.Lock())
    return lock


@mcp.tool()
async def create_sub_issue(
    role: str,
    root_uuid: str,
    *,
    description_html: str = "",
    nickname: str | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Create the role's sub-issue under root with the right label + assignee.

    Enforces:
      - one-sub-per-role: refuses if a sub-issue with the same artifact label
        already exists under this root (use `find_artifact_by_label` /
        re-entry update path instead). Serialized within this process by a
        per-(workspace, root, role) asyncio lock so two concurrent calls
        cannot both pass the duplicate-check and both create.
      - label-non-empty: resolves the artifact label UUID at call time;
        refuses if the workspace has no such label.
      - post-create assert: re-reads the freshly-created sub-issue and fails
        loudly if Plane silently dropped the labels list (UUID typo etc.).
      - title shape: `<Role>: <root_name> (<PROJECT_IDENTIFIER>-<N>)` per
        plane-api.md §6.5.

    Returns `{id, name, labels, ...}` of the new sub-issue.
    """
    ctx = _registry().resolve(workspace=workspace)
    label_uuid = ctx.artifact_label_uuid(role)
    root_uuid = _ensure_uuid(root_uuid, "root_uuid")

    lock = _create_lock_for(ctx.config.workspace_slug, root_uuid, role)
    async with lock, await _client_for(ctx) as plane:
        # Pre-condition: no duplicate
        items = await plane.list_issues(ctx.config.project_id)
        existing = [
            i
            for i in items
            if str(i.get("parent") or "") == root_uuid and label_uuid in (i.get("labels") or [])
        ]
        if existing:
            uuids = [str(i.get("id")) for i in existing]
            raise DuplicateSubIssueError(
                f"workspace {ctx.config.workspace_slug!r}: a sub-issue with "
                f"label artifact:{role} already exists under root {root_uuid} "
                f"({uuids}). Use re-entry path: read the existing artifact and "
                f"update its description, do not create a second one."
            )

        # Resolve root for title + name
        root = await plane.get_issue(ctx.config.project_id, root_uuid)
        root_name = str(root.get("name") or "").strip()
        seq = root.get("sequence_id")
        identifier = (
            f"{ctx.project_identifier}-{seq}" if ctx.project_identifier and seq else f"{seq}"
        )
        title = f"{_role_display(role)}: {root_name} ({identifier})"

        # Resolve assignee (the bot for this role/nickname).
        assignees: list[str] = []
        if nickname:
            assignees.append(ctx.member_uuid(nickname))

        created = await plane.create_issue(
            ctx.config.project_id,
            name=title,
            parent=root_uuid,
            description_html=description_html,
            labels=[label_uuid],
            assignees=assignees,
        )
        # Post-condition: re-read and verify the label stuck.
        sub_id = str(created.get("id") or "")
        if not sub_id:
            raise TowerError("Plane returned no id for the created sub-issue")
        verified = await plane.get_issue(ctx.config.project_id, sub_id)
        labels = verified.get("labels") or []
        if label_uuid not in labels:
            raise UnlabelledSubIssueError(
                f"created sub-issue {sub_id} has labels={labels} after create — "
                f"expected {label_uuid}. Plane silently dropped the labels "
                f"list (likely a UUID typo). The sub-issue exists but is "
                f"unlabelled and will break find_artifact_by_label re-entry. "
                f"Manual intervention required."
            )

    return _summarize_issue(verified)


# Display name per role for sub-issue titles (mirrors plane-api.md §6.5).
_ROLE_DISPLAY = {
    "spec": "SPEC",
    "design": "Design",
    "backend": "Backend",
    "frontend": "Frontend",
    "frontend-react": "Frontend (React)",
    "api-tests": "API Tests",
    "ux-tests": "UX Tests",
}


def _role_display(role: str) -> str:
    return _ROLE_DISPLAY.get(role, role.replace("-", " ").title())


# ----- read_artifact -------------------------------------------------------


@mcp.tool()
async def read_artifact(
    sub_uuid: str,
    *,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Return description + comments of a sub-issue."""
    ctx = _registry().resolve(workspace=workspace)
    sub_uuid = _ensure_uuid(sub_uuid, "sub_uuid")
    async with await _client_for(ctx) as plane:
        sub, comments = await asyncio.gather(
            plane.get_issue(ctx.config.project_id, sub_uuid),
            plane.list_issue_comments(ctx.config.project_id, sub_uuid),
        )
    return {
        "id": sub.get("id"),
        "name": sub.get("name"),
        "description_html": sub.get("description_html") or "",
        "labels": sub.get("labels") or [],
        "state": sub.get("state"),
        "updated_at": sub.get("updated_at"),
        "comments": [
            {
                "id": c.get("id"),
                "comment_html": c.get("comment_html") or "",
                "actor": c.get("actor") or c.get("created_by"),
                "created_at": c.get("created_at"),
                "updated_at": c.get("updated_at"),
            }
            for c in comments
        ],
    }


# ----- update_sub_issue_description ----------------------------------------


@mcp.tool()
async def update_sub_issue_description(
    sub_uuid: str,
    description_html: str,
    *,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Replace the sub-issue's `description_html`. Used for re-entry / rework."""
    ctx = _registry().resolve(workspace=workspace)
    sub_uuid = _ensure_uuid(sub_uuid, "sub_uuid")
    async with await _client_for(ctx) as plane:
        updated = await plane.update_issue(
            ctx.config.project_id,
            sub_uuid,
            {"description_html": description_html},
        )
    return _summarize_issue(updated)


# ----- post_review (architect + reviewer) ----------------------------------


@mcp.tool()
async def post_review(
    target: str,
    verdict: str,
    body_html: str,
    *,
    root_uuid: str | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Post a REVIEW comment on the artifact under review.

    `target` is one of:
      - `spec` / `design` / `backend` / `frontend` / `api-tests` /
        `ux-tests` → the comment goes on that artifact's sub-issue
      - `root` → the comment goes on the root issue (cross-cutting verdict)

    `verdict` is `APPROVED` / `CHANGES_REQUIRED` / `BLOCKED`. The marker is
    `ARCH_REVIEW` for the architect (when target='spec') and `REVIEW`
    otherwise — the tower stamps it from `os.environ['AGENT_NICKNAME']` when
    it equals 'architect', otherwise REVIEW.

    Iteration N is auto-detected by scanning prior review markers on the
    target. Returns `{comment_id, iter, target_uuid}`.

    Pass `root_uuid` when target is a sub-issue role; the tower resolves the
    sub via find_artifact_by_label. For target='root', `root_uuid` is the
    target itself.
    """
    verdict_clean = verdict.strip().upper()
    if verdict_clean not in {"APPROVED", "CHANGES_REQUIRED", "BLOCKED"}:
        raise TowerError(f"verdict must be APPROVED|CHANGES_REQUIRED|BLOCKED, got {verdict!r}")
    ctx = _registry().resolve(workspace=workspace)
    if target == "root":
        if not root_uuid:
            raise TowerError("target='root' requires root_uuid")
        target_uuid = _ensure_uuid(root_uuid, "root_uuid")
    else:
        if not root_uuid:
            raise TowerError(f"target={target!r} requires root_uuid")
        root_uuid = _ensure_uuid(root_uuid, "root_uuid")
        # find sub
        label_uuid = ctx.artifact_label_uuid(target)
        async with await _client_for(ctx) as plane:
            items = await plane.list_issues(ctx.config.project_id)
        matched = [
            i
            for i in items
            if str(i.get("parent") or "") == root_uuid and label_uuid in (i.get("labels") or [])
        ]
        if not matched:
            raise TowerError(f"no sub-issue with artifact:{target} under root {root_uuid}")
        if len(matched) > 1:
            raise DuplicateSubIssueError(
                f"{len(matched)} sub-issues with artifact:{target} under root "
                f"{root_uuid}; cannot post review until duplicates are merged"
            )
        target_uuid = str(matched[0].get("id") or "")

    marker = (
        "ARCH_REVIEW"
        if (
            os.environ.get("AGENT_NICKNAME") == "architect"
            or (target == "spec" and os.environ.get("AGENT_NICKNAME", "").startswith("flynn"))
        )
        else "REVIEW"
    )

    # detect iteration
    async with await _client_for(ctx) as plane:
        comments = await plane.list_issue_comments(ctx.config.project_id, target_uuid)
        iter_n = 1
        for c in comments:
            for m in _REVIEW_MARKER_RE.finditer(c.get("comment_html") or ""):
                if m.group("kind").upper() == marker:
                    with contextlib.suppress(ValueError):
                        iter_n = max(iter_n, int(m.group("iter")) + 1)

        comment_html = (
            f"<p><strong>{marker} (iter {iter_n}) — {verdict_clean}.</strong></p>"
            f"{body_html}"
            f"<p>{_initiator_mention(ctx)}</p>"
        )
        comment = await plane.create_issue_comment(ctx.config.project_id, target_uuid, comment_html)
    return {
        "comment_id": comment.get("id"),
        "iter": iter_n,
        "marker": marker,
        "target_uuid": target_uuid,
        "verdict": verdict_clean,
    }


# ----- mark_spec_approved (architect only) ---------------------------------


@mcp.tool()
async def mark_spec_approved(
    spec_sub_uuid: str,
    summary_html: str,
    *,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Post the SPEC_APPROVED marker comment after the architect's APPROVED
    review. Refuses if the most recent ARCH_REVIEW on this sub is not APPROVED.
    """
    ctx = _registry().resolve(workspace=workspace)
    spec_sub_uuid = _ensure_uuid(spec_sub_uuid, "spec_sub_uuid")
    async with await _client_for(ctx) as plane:
        comments = await plane.list_issue_comments(ctx.config.project_id, spec_sub_uuid)
        # Find the most recent ARCH_REVIEW marker by created_at, then read its
        # verdict from the canonical regex group. Substring scans of the whole
        # comment_html are unsafe — a CHANGES_REQUIRED comment's body can
        # legitimately quote the word "APPROVED" (e.g. "previously APPROVED,
        # now needs changes") and pass a naive `"APPROVED" in html` check.
        latest_match: re.Match[str] | None = None
        latest_at: str = ""
        for c in comments:
            body = c.get("comment_html") or ""
            m = _REVIEW_MARKER_RE.search(body)
            if not m or m.group("kind").upper() != "ARCH_REVIEW":
                continue
            created_at = str(c.get("created_at") or "")
            if created_at >= latest_at:  # ISO-8601 strings sort lexicographically
                latest_at = created_at
                latest_match = m
        if latest_match is None:
            raise TowerError(
                "no prior ARCH_REVIEW comment on this SPEC sub-issue; post APPROVED review first"
            )
        verdict = (latest_match.group("verdict") or "").upper()
        if verdict != "APPROVED":
            raise TowerError(
                f"latest ARCH_REVIEW is not APPROVED (got {verdict or 'no verdict'!r}); "
                f"cannot mark SPEC_APPROVED"
            )
        comment_html = (
            f"<p><strong>SPEC_APPROVED</strong></p>{summary_html}<p>{_initiator_mention(ctx)}</p>"
        )
        comment = await plane.create_issue_comment(
            ctx.config.project_id, spec_sub_uuid, comment_html
        )
    return {"comment_id": comment.get("id")}


# ----- post_changes (coders) -----------------------------------------------


@mcp.tool()
async def post_changes(
    target: str,
    root_uuid: str,
    summary: str,
    files: list[list[str]],
    verification: list[list[str]],
    *,
    migrations: list[list[str]] | None = None,
    perf: dict[str, Any] | None = None,
    deviations_from_plan: list[str] | None = None,
    not_implemented: list[str] | None = None,
    ready_for_review: bool = False,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Post the canonical CHANGES comment on the role's sub-issue.

    `target` is `backend` or `frontend`. `files`/`verification` are pairs of
    `[path_or_command, oneline_summary_or_result]`. The op renders the
    template and refuses `ready_for_review=True` when:

    - `verification` is empty
    - `target='backend'` and any path in `files` matches `views.py` /
      `serializers.py` / `schemas.py` AND `verification` does not contain a
      passing line for `/verify-openapi` (i.e. a string mentioning
      `verify-openapi` or `spectacular`).
    """
    if target not in {"backend", "frontend"}:
        raise TowerError(f"post_changes target must be backend|frontend, got {target!r}")
    ctx = _registry().resolve(workspace=workspace)
    label_uuid = ctx.artifact_label_uuid(target)
    root_uuid = _ensure_uuid(root_uuid, "root_uuid")

    async with await _client_for(ctx) as plane:
        items = await plane.list_issues(ctx.config.project_id)
        matched = [
            i
            for i in items
            if str(i.get("parent") or "") == root_uuid and label_uuid in (i.get("labels") or [])
        ]
        if not matched:
            raise TowerError(
                f"no sub-issue with artifact:{target} under root {root_uuid}; "
                f"create_sub_issue first"
            )
        if len(matched) > 1:
            raise DuplicateSubIssueError(
                f"{len(matched)} sub-issues with artifact:{target} under root "
                f"{root_uuid}; cannot post changes"
            )
        sub_uuid = str(matched[0].get("id"))

        # Defenses on ready_for_review
        if ready_for_review:
            if not verification:
                raise TowerError("ready_for_review=True requires non-empty verification")
            if target == "backend":
                touches_api = any(
                    re.search(r"(views|serializers|schemas)\.py$", path)
                    for path, _ in (files or [])
                )
                if touches_api:
                    has_openapi = any(
                        ("verify-openapi" in cmd or "spectacular" in cmd) for cmd, _ in verification
                    )
                    if not has_openapi:
                        raise TowerError(
                            "API documentation defense (plane-api.md §6.7d): "
                            "you modified views/serializers/schemas but "
                            "verification does not include /verify-openapi (or "
                            "spectacular --validate --fail-on-warn). Run the "
                            "project's OpenAPI verifier before claiming ready_for_review."
                        )

        # Render. All caller-controlled fragments are HTML-escaped (paths,
        # summaries, command output, etc.). Plane's rich-text renderer trusts
        # comment_html, so an unescaped angle-bracket in a path could break
        # the comment shape or smuggle markup.
        files_html = "".join(
            f"<li><code>{_esc(path)}</code> — {_esc(summary_line)}</li>"
            for path, summary_line in (files or [])
        )
        verification_html = "".join(
            f"<li><code>{_esc(cmd)}</code> — {_esc(result)}</li>"
            for cmd, result in (verification or [])
        )
        migrations_html = ""
        if migrations:
            migrations_rows = "".join(
                f"<li><code>{_esc(name)}</code> — {_esc(descr)}</li>" for name, descr in migrations
            )
            migrations_html = f"<h2>Migrations</h2><ul>{migrations_rows}</ul>"
        perf_html = ""
        if perf:
            perf_html = "<h2>Performance</h2><pre>" + _esc(perf) + "</pre>"
        dev_html = ""
        if deviations_from_plan:
            rows = "".join(f"<li>{_esc(d)}</li>" for d in deviations_from_plan)
            dev_html = f"<h2>Deviations from PLAN</h2><ul>{rows}</ul>"
        ni_html = ""
        if not_implemented:
            rows = "".join(f"<li>{_esc(d)}</li>" for d in not_implemented)
            ni_html = f"<h2>Not implemented (deferred)</h2><ul>{rows}</ul>"
        ready_html = "READY FOR REVIEW: yes" if ready_for_review else "READY FOR REVIEW: no"

        comment_html = (
            f"<h1>{_esc(_role_display(target))} CHANGES</h1>"
            f"<p>{_esc(summary)}</p>"
            f"<h2>Files modified</h2><ul>{files_html}</ul>"
            f"{migrations_html}"
            f"<h2>Verification</h2><ul>{verification_html}</ul>"
            f"{perf_html}{dev_html}{ni_html}"
            f"<p><strong>{ready_html}</strong></p>"
            f"<p>{_initiator_mention(ctx)}</p>"
        )
        comment = await plane.create_issue_comment(ctx.config.project_id, sub_uuid, comment_html)
    return {
        "comment_id": comment.get("id"),
        "sub_uuid": sub_uuid,
        "ready_for_review": ready_for_review,
    }


# ----- post_bug_report (testers) -------------------------------------------


@mcp.tool()
async def post_bug_report(
    target: str,
    affected_role: str,
    severity: str,
    title: str,
    repro_steps: list[str],
    actual: str,
    expected: str,
    root_uuid: str,
    *,
    failing_tc: str = "",
    environment: dict[str, Any] | None = None,
    fix_hint: str = "",
    screenshots: list[str] | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Post an ISTQB bug report on the test sub-issue + back-link the
    affected coder's sub-issue. `target` ∈ {api-tests, ux-tests};
    `affected_role` ∈ {backend, frontend}.
    """
    if target not in {"api-tests", "ux-tests"}:
        raise TowerError("post_bug_report target must be api-tests|ux-tests")
    if affected_role not in {"backend", "frontend"}:
        raise TowerError("affected_role must be backend|frontend")
    severity = severity.strip().lower()
    if severity not in {"blocker", "major", "minor", "cosmetic"}:
        raise TowerError("severity must be blocker|major|minor|cosmetic")

    ctx = _registry().resolve(workspace=workspace)
    test_label = ctx.artifact_label_uuid(target)
    affected_label = ctx.artifact_label_uuid(affected_role)
    root_uuid = _ensure_uuid(root_uuid, "root_uuid")

    async with await _client_for(ctx) as plane:
        items = await plane.list_issues(ctx.config.project_id)
        test_sub = next(
            (
                i
                for i in items
                if str(i.get("parent") or "") == root_uuid and test_label in (i.get("labels") or [])
            ),
            None,
        )
        if not test_sub:
            raise TowerError(f"no sub-issue with artifact:{target} under root {root_uuid}")
        affected_sub = next(
            (
                i
                for i in items
                if str(i.get("parent") or "") == root_uuid
                and affected_label in (i.get("labels") or [])
            ),
            None,
        )
        # affected_sub may be None — still post the bug, just no back-link.

        steps_html = "".join(f"<li>{_esc(s)}</li>" for s in repro_steps)
        env_html = ""
        if environment:
            rows = "".join(
                f"<li><strong>{_esc(k)}:</strong> {_esc(v)}</li>" for k, v in environment.items()
            )
            env_html = f"<h2>Environment</h2><ul>{rows}</ul>"
        screenshots_html = ""
        if screenshots:
            shots = "".join(f'<li><a href="{_esc(u)}">{_esc(u)}</a></li>' for u in screenshots)
            screenshots_html = f"<h2>Attachments</h2><ul>{shots}</ul>"
        fix_html = f"<p><em>Fix hint:</em> {_esc(fix_hint)}</p>" if fix_hint else ""

        affected_id_disp = str(affected_sub.get("id")) if affected_sub else "?"
        comment_html = (
            f"<h1>Bug ({_esc(severity)}): {_esc(title)}</h1>"
            f"<p><strong>Severity:</strong> {_esc(severity)}<br>"
            f"<strong>Priority:</strong> TBD by initiator<br>"
            f"<strong>Failing TC:</strong> {_esc(failing_tc or 'n/a')}</p>"
            f"<h2>Steps to reproduce</h2><ol>{steps_html}</ol>"
            f"<h2>Actual</h2><p>{_esc(actual)}</p>"
            f"<h2>Expected</h2><p>{_esc(expected)}</p>"
            f"{env_html}{screenshots_html}{fix_html}"
            f"<p>Affected sub-issue: <code>{_esc(affected_id_disp)}</code></p>"
            f"<p>{_initiator_mention(ctx)}</p>"
        )
        comment = await plane.create_issue_comment(
            ctx.config.project_id, str(test_sub.get("id")), comment_html
        )

        # Back-link: a work-item link from the affected sub to the bug comment.
        if affected_sub:
            try:
                link_url = (
                    f"{ctx.config.plane_base_url}/{ctx.config.workspace_slug}/"
                    f"projects/{ctx.config.project_id}/issues/"
                    f"{test_sub.get('id')}/#comment-{comment.get('id')}"
                )
                await plane.create_issue_link(
                    ctx.config.project_id,
                    str(affected_sub.get("id")),
                    url=link_url,
                    title=f"[{severity}] {title}",
                )
            except PlaneAPIError:
                # Best-effort — the bug comment is the source of truth.
                pass

    return {
        "comment_id": comment.get("id"),
        "test_sub_uuid": str(test_sub.get("id")),
        "affected_sub_uuid": str(affected_sub.get("id")) if affected_sub else None,
        "severity": severity,
    }


# ----- escalate_upstream_gap -----------------------------------------------


@mcp.tool()
async def escalate_upstream_gap(
    my_sub_uuid: str,
    affected: str,
    issue: str,
    proposed_resolution: str,
    *,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Post a `BLOCKED — upstream gap` comment in your own sub-issue. Used
    when a downstream agent (coder/tester/designer/reviewer) finds a defect
    that belongs upstream — missing FR, ambiguous AC, broken design contract.
    Do not patch locally; the upstream role updates its existing artifact.
    """
    ctx = _registry().resolve(workspace=workspace)
    my_sub_uuid = _ensure_uuid(my_sub_uuid, "my_sub_uuid")
    comment_html = (
        "<p><strong>BLOCKED — upstream gap.</strong></p>"
        f"<p>Affected: <code>{_esc(affected)}</code></p>"
        f"<p>Issue: {_esc(issue)}</p>"
        f"<p>Proposed resolution: {_esc(proposed_resolution)}</p>"
        f"<p>{_initiator_mention(ctx)}</p>"
    )
    async with await _client_for(ctx) as plane:
        comment = await plane.create_issue_comment(ctx.config.project_id, my_sub_uuid, comment_html)
    return {"comment_id": comment.get("id")}


# ----- mark_phase_complete (BA, SA) ----------------------------------------


@mcp.tool()
async def mark_phase_complete(
    my_sub_uuid: str,
    phase: int,
    *,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Flip `- [ ] Phase N: …` → `- [x] Phase N: …` in your sub-issue's
    description. Refuses to close phase N if any earlier phase (1..N-1) is
    still open — agents must not skip phases.
    """
    ctx = _registry().resolve(workspace=workspace)
    my_sub_uuid = _ensure_uuid(my_sub_uuid, "my_sub_uuid")
    async with await _client_for(ctx) as plane:
        sub = await plane.get_issue(ctx.config.project_id, my_sub_uuid)
        desc = sub.get("description_html") or ""
        # Validate ordering
        for n in range(1, phase):
            if re.search(rf"\[ \]\s*Phase\s*{n}\b", desc):
                raise TowerError(
                    f"cannot close Phase {phase}: Phase {n} is still open. "
                    f"Phases must close in order."
                )
        pattern = re.compile(rf"\[ \](\s*Phase\s*{phase}\b)", re.IGNORECASE)
        if not pattern.search(desc):
            raise TowerError(f"no `[ ] Phase {phase}` checkbox found in description")
        new_desc = pattern.sub(r"[x]\1", desc, count=1)
        updated = await plane.update_issue(
            ctx.config.project_id, my_sub_uuid, {"description_html": new_desc}
        )
    return {"id": updated.get("id"), "phase": phase, "status": "closed"}


# ----- post_artifact_comment / post_startup_comment / update_startup --------


@mcp.tool()
async def post_comment(
    work_item_uuid: str,
    comment_html: str,
    *,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Generic comment on any work item (sub-issue or root). Lower-level than
    post_review / post_changes / post_bug_report — use those when they fit.
    """
    ctx = _registry().resolve(workspace=workspace)
    work_item_uuid = _ensure_uuid(work_item_uuid, "work_item_uuid")
    async with await _client_for(ctx) as plane:
        comment = await plane.create_issue_comment(
            ctx.config.project_id, work_item_uuid, comment_html
        )
    return {"id": comment.get("id")}


@mcp.tool()
async def update_comment(
    work_item_uuid: str,
    comment_id: str,
    comment_html: str,
    *,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Update an existing comment (used for promoting startup-comment to summary
    at end-of-run).
    """
    ctx = _registry().resolve(workspace=workspace)
    work_item_uuid = _ensure_uuid(work_item_uuid, "work_item_uuid")
    comment_id = _ensure_uuid(comment_id, "comment_id")
    async with await _client_for(ctx) as plane:
        await plane.update_issue_comment(
            ctx.config.project_id, work_item_uuid, comment_id, comment_html
        )
    return {"id": comment_id}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _conductor_dir() -> Path:
    return Path(os.environ.get("CONDUCTOR_DIR", "/etc/plane-conductor/conductor.d"))


async def _async_main() -> None:
    global _REGISTRY
    _REGISTRY = await build_registry(_conductor_dir())
    if not _REGISTRY.by_slug:
        raise TowerError(
            f"plane-tower: no workspaces found in {_conductor_dir()}; "
            f"populate conductor.d/*.yaml first"
        )


def main() -> None:
    """Console-script entry point: hydrate registry then run the MCP server."""
    asyncio.run(_async_main())
    mcp.run()


if __name__ == "__main__":
    main()
