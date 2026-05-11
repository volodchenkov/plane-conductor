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
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from functools import wraps
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


class RoleNotFoundError(TowerError):
    """A symbolic role (e.g. 'reviewer') has no member registered in this workspace."""


class MentionInBodyError(TowerError):
    """Free-form HTML contained a <mention-component> tag.

    Mentions are tower-managed to prevent self-mention bugs (the Beck/Sark
    incident class). Use `request_handoff(target_role=...)` for explicit
    handoffs, or pass `next_role=…` to post_changes / post_review /
    mark_phase_complete / mark_spec_approved.
    """


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

# Soft cap on artifact description size (Markdown chars after HTML-strip). Tower
# does not reject above this — it just stamps `description_size_warning: True`
# on every read / write so the author sees the cost. Chosen well below the
# observed MCP tool-result token cap so there's headroom for the comment
# slice that read_artifact returns alongside the description.
DESCRIPTION_SOFT_LIMIT_CHARS = 50_000


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
_MENTION_TEMPLATE = (
    '<mention-component entity_identifier="{uuid}" entity_name="user_mention"></mention-component>'
)
_MENTION_RE = re.compile(r"<mention-component\b[^>]*>", re.IGNORECASE)


def _initiator_mention(ctx: WorkspaceContext) -> str:
    return _MENTION_TEMPLATE.format(uuid=str(ctx.config.initiator_uuid))


def _role_mention(ctx: WorkspaceContext, role: str) -> str:
    """Resolve a pipeline role (e.g. 'reviewer', 'architect') to its member
    mention component for this workspace. Match is against `prompt_role` of
    the workspace's `agents` list, with any optional `<namespace>:` prefix
    stripped (so `sdlc-agents:reviewer` and `reviewer` both work).

    The special role `'initiator'` resolves to `ctx.config.initiator_uuid` —
    used by startup comments / blocking-question / final-summary handoffs
    where the agent wants to ping the human, not another bot.

    Raises RoleNotFoundError if no agent in this workspace has that role,
    or if the resolved nickname has no member UUID in the cache.
    """
    target = role.strip().rsplit(":", 1)[-1].lower()
    if not target:
        raise RoleNotFoundError("target_role is empty")
    if target == "initiator":
        return _initiator_mention(ctx)
    for agent in ctx.config.agents:
        bare = agent.prompt_role.rsplit(":", 1)[-1].lower()
        if bare == target:
            try:
                uuid = ctx.member_uuid(agent.nickname)
            except TowerError as exc:
                raise RoleNotFoundError(
                    f"workspace {ctx.config.workspace_slug!r}: role {role!r} maps "
                    f"to nickname {agent.nickname!r}, but that member is not in "
                    f"the project member cache. Re-run hydration or add the bot "
                    f"to the project."
                ) from exc
            return _MENTION_TEMPLATE.format(uuid=uuid)
    known = sorted({a.prompt_role.rsplit(":", 1)[-1] for a in ctx.config.agents})
    raise RoleNotFoundError(
        f"workspace {ctx.config.workspace_slug!r}: no agent registered for "
        f"role {role!r}. Known roles: {known}"
    )


def _assert_no_mentions(comment_html: str | None) -> None:
    """Refuse if caller-supplied HTML contains any <mention-component> tag.

    All mentions in tower-posted comments are constructed by the tower from
    structured params (`next_role`, `target_role`) — agents must not embed
    `<mention-component>` themselves, otherwise we lose the self-mention
    defense.
    """
    if comment_html and _MENTION_RE.search(comment_html):
        raise MentionInBodyError(
            "comment_html contains <mention-component>. Mentions are "
            "tower-managed: use `request_handoff(target_role=...)` for "
            "explicit handoffs, or pass `next_role=...` to post_changes / "
            "post_review / mark_phase_complete / mark_spec_approved."
        )


def _ensure_uuid(value: str | UUID, name: str) -> str:
    """Validate that `value` looks like a UUID; return its canonical str form."""
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError) as exc:
        raise TowerError(f"{name} is not a valid UUID: {value!r}") from exc


# One PlaneClient per workspace, keyed by slug, kept alive for the lifetime of
# the tower process. Each MCP tool used to create + close a fresh client per
# call → 30+ TCP/TLS handshakes per agent run to the same Plane endpoint.
# Reusing one client per workspace reuses httpx's keep-alive pool. Tower is a
# long-lived process (per agent spawn), so the client lifetime matches.
_SHARED_CLIENTS: dict[str, PlaneClient] = {}


async def _client_for(ctx: WorkspaceContext) -> PlaneClient:
    slug = ctx.config.workspace_slug
    client = _SHARED_CLIENTS.get(slug)
    if client is None:
        client = PlaneClient(
            ctx.config.plane_base_url,
            ctx.config.plane_api_key,
            slug,
            shared=True,
        )
        _SHARED_CLIENTS[slug] = client
    return client


# ---------------------------------------------------------------------------
# FastMCP server + tools
# ---------------------------------------------------------------------------


mcp = FastMCP("plane-tower")
_REGISTRY: TowerRegistry | None = None


# Call log: one JSON line per tool invocation, written to
# $LOG_DIR/tower-<pid>.jsonl. Enabled by env `TOWER_CALL_LOG=1`. Off by
# default. Used to diagnose «agent silently hung» — claude --print discards
# child MCP server stderr, so without a sidecar log we can't tell which tool
# call was in-flight at the time of a hang.
if os.environ.get("TOWER_CALL_LOG", "").strip().lower() in {"1", "true", "yes", "on"}:
    _log_path = (
        Path(os.environ.get("LOG_DIR", "/var/log/plane-conductor")) / f"tower-{os.getpid()}.jsonl"
    )
    _log_path.parent.mkdir(parents=True, exist_ok=True)
    _log_fh = open(_log_path, "a", buffering=1, encoding="utf-8")  # noqa: SIM115  (long-lived handle for process lifetime)

    def _emit(**event: Any) -> None:
        with contextlib.suppress(Exception):
            _log_fh.write(json.dumps(event, default=str) + "\n")

    _orig_tool = mcp.tool

    def _logged_tool(*a: Any, **kw: Any) -> Any:
        register = _orig_tool(*a, **kw)

        def wrap(fn: Any) -> Any:
            @wraps(fn)
            async def inner(*args: Any, **kwargs: Any) -> Any:
                t0 = time.monotonic()
                _emit(event="start", tool=fn.__name__, args=sorted(kwargs))
                try:
                    result = await fn(*args, **kwargs)
                except Exception as exc:
                    _emit(
                        event="error",
                        tool=fn.__name__,
                        ms=int((time.monotonic() - t0) * 1000),
                        exc=type(exc).__name__,
                        msg=str(exc)[:300],
                    )
                    raise
                size = (
                    len(json.dumps(result, default=str)) if isinstance(result, dict | list) else 0
                )
                _emit(
                    event="end",
                    tool=fn.__name__,
                    ms=int((time.monotonic() - t0) * 1000),
                    size=size,
                )
                return result

            return register(inner)

        return wrap

    mcp.tool = _logged_tool  # type: ignore[method-assign]


def _registry() -> TowerRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        raise TowerError("plane-tower registry is not initialised; call main()")
    return _REGISTRY


# ----- pickup_issue --------------------------------------------------------


@mcp.tool()
async def pickup_issue(
    issue_uuid: str,
    *,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Resolve an issue by UUID. Conductor always passes UUID in the spawn
    prompt, so the lookup is a single GET.

    Returns the full work item record plus the resolved workspace slug.
    """
    ctx = _registry().resolve(workspace=workspace)
    async with await _client_for(ctx) as plane:
        issue = await plane.get_issue(ctx.config.project_id, issue_uuid)
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

    Trusts Plane's create-response: if `created["labels"]` is missing the
    requested UUIDs, raises `UnlabelledSubIssueError` without a re-read.
    Plane returns the full record on POST.

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
    if label_uuids:
        got = created.get("labels") or []
        missing = [u for u in label_uuids if u not in got]
        if missing:
            raise UnlabelledSubIssueError(
                f"created root issue {new_id} has labels={got} on POST "
                f"response — expected {label_uuids}. Plane silently dropped "
                f"one or more labels (likely a UUID typo). Manual intervention "
                f"required."
            )

    seq = created.get("sequence_id")
    identifier = f"{ctx.project_identifier}-{seq}" if ctx.project_identifier and seq else None
    return {
        **_summarize_issue(created),
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
      - post-create assert: checks `created["labels"]` from the POST response
        and fails loudly if Plane silently dropped the labels list (UUID typo).
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
    # Trust Plane's create-response: it returns the full record with labels.
    sub_id = str(created.get("id") or "")
    if not sub_id:
        raise TowerError("Plane returned no id for the created sub-issue")
    labels = created.get("labels") or []
    if label_uuid not in labels:
        raise UnlabelledSubIssueError(
            f"created sub-issue {sub_id} has labels={labels} on POST "
            f"response — expected {label_uuid}. Plane silently dropped the "
            f"labels list (likely a UUID typo). The sub-issue exists but is "
            f"unlabelled and will break find_artifact_by_label re-entry. "
            f"Manual intervention required."
        )

    return _summarize_issue(created)


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

# Plane stores description/comments as `description_html` / `comment_html`. The
# editor (BlockNote/TipTap) emits valid but markup-heavy HTML: every <p> carries
# class/style attributes, lists are nested deep, etc. For a real SPEC this means
# the raw HTML is roughly 30 % larger than the underlying text — and on a
# 100 KB-class document that pushes the MCP tool-result over Claude Code's
# token cap, which is the failure mode the next two helpers exist to defuse.

_MD_HEADING_RE = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1>", re.IGNORECASE | re.DOTALL)
_MD_BOLD_RE = re.compile(r"<(?:strong|b)\b[^>]*>(.*?)</(?:strong|b)>", re.IGNORECASE | re.DOTALL)
_MD_EM_RE = re.compile(r"<(?:em|i)\b[^>]*>(.*?)</(?:em|i)>", re.IGNORECASE | re.DOTALL)
_MD_PRE_RE = re.compile(
    r"<pre\b[^>]*>\s*<code\b[^>]*>(.*?)</code>\s*</pre>", re.IGNORECASE | re.DOTALL
)
_MD_CODE_RE = re.compile(r"<code\b[^>]*>(.*?)</code>", re.IGNORECASE | re.DOTALL)
_MD_LINK_RE = re.compile(r'<a\b[^>]*href="([^"]*)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_MD_LI_RE = re.compile(r"<li\b[^>]*>(.*?)</li>", re.IGNORECASE | re.DOTALL)
_MD_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_MD_BLOCK_END_RE = re.compile(
    r"</(?:p|div|h[1-6]|li|tr|pre|blockquote|ul|ol|table|thead|tbody)>", re.IGNORECASE
)
_MD_STRIP_TAGS_RE = re.compile(r"<[^>]+>")


def _strip_inline_tags(s: str) -> str:
    return _MD_STRIP_TAGS_RE.sub("", s).strip()


def html_to_markdown(html_text: str) -> str:
    """Convert Plane's editor HTML to a compact Markdown-ish string.

    Goal is token reduction without losing structure: headings stay as `#`,
    lists as `- `, code as fenced/inline, links as `[text](url)`, emphasis as
    `**`/`*`. This is NOT a general-purpose HTML-to-Markdown — it is sized for
    the subset Plane emits (BlockNote/TipTap output: p, h1-h6, strong/b, em/i,
    code, pre>code, a, ul/ol/li, br, simple tables). Adequate fidelity for an
    agent's reasoning pass.

    Empty input returns "" (callers can rely on bool-checks).
    """
    if not html_text:
        return ""
    s = html_text
    s = _MD_PRE_RE.sub(
        lambda m: "\n```\n" + html.unescape(_strip_inline_tags(m.group(1))) + "\n```\n", s
    )
    s = _MD_CODE_RE.sub(lambda m: "`" + html.unescape(_strip_inline_tags(m.group(1))) + "`", s)
    s = _MD_HEADING_RE.sub(
        lambda m: "\n" + "#" * int(m.group(1)) + " " + _strip_inline_tags(m.group(2)) + "\n", s
    )
    s = _MD_BOLD_RE.sub(lambda m: "**" + _strip_inline_tags(m.group(1)) + "**", s)
    s = _MD_EM_RE.sub(lambda m: "*" + _strip_inline_tags(m.group(1)) + "*", s)
    s = _MD_LINK_RE.sub(lambda m: "[" + _strip_inline_tags(m.group(2)) + "](" + m.group(1) + ")", s)
    s = _MD_LI_RE.sub(lambda m: "- " + _strip_inline_tags(m.group(1)) + "\n", s)
    s = _MD_BR_RE.sub("\n", s)
    s = _MD_BLOCK_END_RE.sub("\n", s)
    s = _MD_STRIP_TAGS_RE.sub("", s)
    s = html.unescape(s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n[ \t]+", "\n", s)
    return s.strip()


@mcp.tool()
async def read_artifact(
    sub_uuid: str,
    *,
    description_format: str = "markdown",
    description_offset: int = 0,
    description_limit: int | None = None,
    comments_limit: int = 5,
    comments_offset: int = 0,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Return a sub-issue's description + a slice of its comments.

    `description_format`:
      - `'markdown'` (default) — HTML stripped to Markdown; typically ~70 % of
        the raw size. **This is what you want for reading SPEC/REQUIREMENTS/etc.**
      - `'html'` — raw `description_html` from Plane, full markup. Only use if
        you genuinely need the HTML (e.g. relaying it back into Plane verbatim).
        Risk: a bloated SPEC's HTML can exceed Claude Code's MCP tool-result
        token cap and silently hang the agent.

    Description pagination (`description_offset` / `description_limit`):
      Slice of the (Markdown- or HTML-) rendered description in CHARACTERS,
      counted on the converted body — i.e. align with `description_size_chars`,
      not with the raw HTML. Default `description_limit=None` returns the
      whole document (back-compat with pre-pagination callers). When a SPEC
      exceeds the soft cap (`description_size_warning=True`) the caller can
      walk it in chunks: pass `description_limit=40_000`, then re-call with
      `description_offset += description_returned_chars` while
      `description_has_more` is `True`. Chunk boundaries are NOT structural —
      a chunk can split a heading/table mid-line; that is the caller's
      problem to reassemble.

    Comments are returned newest-first (`comments_order='desc'`), sliced
    `[comments_offset : comments_offset + comments_limit]`. `comments_limit=0`
    means «no comments»; pass a large number to fetch all. The default of
    5 is sized for re-entry / latest-verdict detection — most pipeline comment
    history is heartbeat noise («@nick picking up», «exited 143»). Agents
    that need older context paginate via `comments_offset`; agents that need
    structured verdict parsing should use the dedicated tools (`mark_spec_approved`,
    `post_review`'s prior-comment scan) which already filter internally.

    Response carries `total_comments`, `comments_returned`, `comments_offset`,
    `has_more_comments` so the caller can decide whether to page back, plus
    `description_size_chars` (full size of the rendered description, NOT the
    returned slice), `description_offset`, `description_returned_chars`,
    `description_has_more`, `description_soft_limit_chars`,
    `description_size_warning` (cf. `DESCRIPTION_SOFT_LIMIT_CHARS`) — the
    same conciseness signal `update_sub_issue_description` returns.
    """
    if description_format not in {"markdown", "html"}:
        raise TowerError(
            f"description_format must be 'markdown' or 'html', got {description_format!r}"
        )
    if comments_limit < 0 or comments_offset < 0:
        raise TowerError("comments_limit and comments_offset must be non-negative")
    if description_offset < 0:
        raise TowerError("description_offset must be non-negative")
    if description_limit is not None and description_limit < 0:
        raise TowerError("description_limit must be non-negative or None")

    ctx = _registry().resolve(workspace=workspace)
    sub_uuid = _ensure_uuid(sub_uuid, "sub_uuid")
    async with await _client_for(ctx) as plane:
        sub, comments = await asyncio.gather(
            plane.get_issue(ctx.config.project_id, sub_uuid),
            plane.list_issue_comments(ctx.config.project_id, sub_uuid),
        )

    raw_description = sub.get("description_html") or ""
    if description_format == "markdown":
        full_description = html_to_markdown(raw_description)
    else:
        full_description = raw_description

    desc_size = len(full_description)
    if description_limit is None:
        description = full_description[description_offset:]
    else:
        description = full_description[description_offset : description_offset + description_limit]
    description_has_more = description_offset + len(description) < desc_size

    comments_sorted = sorted(comments, key=lambda c: c.get("created_at") or "", reverse=True)
    total = len(comments_sorted)
    sliced = comments_sorted[comments_offset : comments_offset + comments_limit]
    has_more = comments_offset + len(sliced) < total

    def _comment_text(c: dict[str, Any]) -> str:
        raw = c.get("comment_html") or ""
        return html_to_markdown(raw) if description_format == "markdown" else raw

    return {
        "id": sub.get("id"),
        "name": sub.get("name"),
        "description": description,
        "description_format": description_format,
        "description_size_chars": desc_size,
        "description_offset": description_offset,
        "description_returned_chars": len(description),
        "description_has_more": description_has_more,
        "description_soft_limit_chars": DESCRIPTION_SOFT_LIMIT_CHARS,
        "description_size_warning": desc_size > DESCRIPTION_SOFT_LIMIT_CHARS,
        "labels": sub.get("labels") or [],
        "state": sub.get("state"),
        "updated_at": sub.get("updated_at"),
        "comments": [
            {
                "id": c.get("id"),
                "comment": _comment_text(c),
                "actor": c.get("actor") or c.get("created_by"),
                "created_at": c.get("created_at"),
                "updated_at": c.get("updated_at"),
            }
            for c in sliced
        ],
        "comments_order": "desc",
        "comments_offset": comments_offset,
        "comments_returned": len(sliced),
        "total_comments": total,
        "has_more_comments": has_more,
    }


# ----- list_comments -------------------------------------------------------


@mcp.tool()
async def list_comments(
    sub_uuid: str,
    *,
    limit: int = 50,
    offset: int = 0,
    description_format: str = "markdown",
    workspace: str | None = None,
) -> dict[str, Any]:
    """Return a sub-issue's comments without re-fetching its (possibly huge)
    description.

    Pagination shape mirrors `read_artifact`: newest-first
    (`order='desc'`), sliced `[offset : offset + limit]`. Default
    `limit=50` covers most iter_n-counting / re-entry-scan use cases in
    one call; bump higher for full audits.

    `description_format` controls whether comments come back as Markdown
    (stripped via `html_to_markdown`, default) or raw HTML.

    Use this when you've already called `read_artifact` once for the
    description and now need older comments — re-calling `read_artifact`
    with a larger `comments_limit` re-pulls the description (often 50 KB+).
    """
    if limit < 0 or offset < 0:
        raise TowerError("limit and offset must be non-negative")
    if description_format not in {"markdown", "html"}:
        raise TowerError(
            f"description_format must be 'markdown' or 'html', got {description_format!r}"
        )
    ctx = _registry().resolve(workspace=workspace)
    sub_uuid = _ensure_uuid(sub_uuid, "sub_uuid")
    async with await _client_for(ctx) as plane:
        comments = await plane.list_issue_comments(ctx.config.project_id, sub_uuid)

    comments_sorted = sorted(comments, key=lambda c: c.get("created_at") or "", reverse=True)
    total = len(comments_sorted)
    sliced = comments_sorted[offset : offset + limit]
    has_more = offset + len(sliced) < total

    def _comment_text(c: dict[str, Any]) -> str:
        raw = c.get("comment_html") or ""
        return html_to_markdown(raw) if description_format == "markdown" else raw

    return {
        "comments": [
            {
                "id": c.get("id"),
                "comment": _comment_text(c),
                "actor": c.get("actor") or c.get("created_by"),
                "created_at": c.get("created_at"),
                "updated_at": c.get("updated_at"),
            }
            for c in sliced
        ],
        "order": "desc",
        "offset": offset,
        "returned": len(sliced),
        "total": total,
        "has_more": has_more,
    }


# ----- update_sub_issue_description ----------------------------------------


@mcp.tool()
async def update_sub_issue_description(
    sub_uuid: str,
    description_html: str,
    *,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Replace the sub-issue's `description_html`. Used for re-entry / rework.

    Response includes `description_size_chars` (after Markdown stripping) and
    `description_size_warning: True` when the new body crosses
    `DESCRIPTION_SOFT_LIMIT_CHARS` — the author sees the cost on every write
    and can apply the conciseness rules from artifact-templates (revisions
    in place, ADR-thin, reference-don't-quote, soft section budget).
    """
    ctx = _registry().resolve(workspace=workspace)
    sub_uuid = _ensure_uuid(sub_uuid, "sub_uuid")
    async with await _client_for(ctx) as plane:
        updated = await plane.update_issue(
            ctx.config.project_id,
            sub_uuid,
            {"description_html": description_html},
        )
    md_size = len(html_to_markdown(description_html))
    summary = _summarize_issue(updated)
    summary["description_size_chars"] = md_size
    summary["description_soft_limit_chars"] = DESCRIPTION_SOFT_LIMIT_CHARS
    summary["description_size_warning"] = md_size > DESCRIPTION_SOFT_LIMIT_CHARS
    return summary


# ----- post_review (architect + reviewer) ----------------------------------


@mcp.tool()
async def post_review(
    sub_uuid: str,
    verdict: str,
    body_html: str,
    *,
    iter_n: int = 1,
    next_role: str | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Post a REVIEW comment on the given sub-issue.

    `sub_uuid` is the sub-issue receiving the comment. The agent already
    has this — for the architect and coders it equals the spawn
    `issue_uuid`; for the final reviewer iterating over multiple artifacts
    it is the per-artifact sub_uuid resolved via `find_artifact_by_label`
    or `list_sub_issues`.

    `verdict` is `APPROVED` / `CHANGES_REQUIRED` / `BLOCKED`.

    `iter_n` is the iteration counter (1-based). Agents derive it from
    prior comments they already read via `read_artifact` — tower no longer
    walks all comments for auto-detection, that was a hang source.

    `next_role` (optional) — pipeline role to mention alongside the
    initiator. Tower resolves the role to its member UUID. `body_html`
    must NOT contain `<mention-component>` — those are tower-managed.

    One HTTP call (`create_issue_comment`). No `list_issues` /
    `list_issue_comments` underneath.
    """
    _assert_no_mentions(body_html)
    verdict_clean = verdict.strip().upper()
    if verdict_clean not in {"APPROVED", "CHANGES_REQUIRED", "BLOCKED"}:
        raise TowerError(f"verdict must be APPROVED|CHANGES_REQUIRED|BLOCKED, got {verdict!r}")
    if iter_n < 1:
        raise TowerError(f"iter_n must be >= 1, got {iter_n}")
    ctx = _registry().resolve(workspace=workspace)
    next_mention = _role_mention(ctx, next_role) if next_role else ""
    sub_uuid = _ensure_uuid(sub_uuid, "sub_uuid")
    comment_html = (
        f"<p><strong>REVIEW (iter {iter_n}) — {verdict_clean}.</strong></p>"
        f"{body_html}"
        f"<p>{_initiator_mention(ctx)}{next_mention}</p>"
    )
    async with await _client_for(ctx) as plane:
        comment = await plane.create_issue_comment(ctx.config.project_id, sub_uuid, comment_html)
    return {
        "comment_id": comment.get("id"),
        "iter": iter_n,
        "sub_uuid": sub_uuid,
        "verdict": verdict_clean,
    }


# ----- mark_spec_approved (architect only) ---------------------------------


@mcp.tool()
async def mark_spec_approved(
    spec_sub_uuid: str,
    summary_html: str,
    *,
    next_role: str | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Post the SPEC_APPROVED marker comment.

    Caller is responsible for having posted an APPROVED REVIEW first —
    tower no longer walks comments to verify (that was a hang source on
    long pipelines). The architect is who calls this, and they just
    posted the APPROVED review themselves one step earlier.

    `next_role` (optional) — pipeline role to mention alongside the
    initiator (typically the next coder, e.g. `django-developer`).
    `summary_html` must NOT contain `<mention-component>` — tower-managed.
    """
    _assert_no_mentions(summary_html)
    ctx = _registry().resolve(workspace=workspace)
    next_mention = _role_mention(ctx, next_role) if next_role else ""
    spec_sub_uuid = _ensure_uuid(spec_sub_uuid, "spec_sub_uuid")
    comment_html = (
        f"<p><strong>SPEC_APPROVED</strong></p>{summary_html}"
        f"<p>{_initiator_mention(ctx)}{next_mention}</p>"
    )
    async with await _client_for(ctx) as plane:
        comment = await plane.create_issue_comment(
            ctx.config.project_id, spec_sub_uuid, comment_html
        )
    return {"comment_id": comment.get("id")}


# ----- post_changes (coders) -----------------------------------------------


@mcp.tool()
async def post_changes(
    sub_uuid: str,
    target: str,
    summary: str,
    files: list[list[str]],
    verification: list[list[str]],
    *,
    migrations: list[list[str]] | None = None,
    perf: dict[str, Any] | None = None,
    deviations_from_plan: list[str] | None = None,
    not_implemented: list[str] | None = None,
    ready_for_review: bool = False,
    next_role: str | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Post the canonical CHANGES comment on the coder's sub-issue.

    `sub_uuid` is the coder's sub-issue (equals the spawn `issue_uuid`).
    `target` is `backend` or `frontend` — used only for the display label
    and the OpenAPI verifier defense check.

    `files`/`verification` are pairs of `[path_or_command,
    oneline_summary_or_result]`. The op renders the template and refuses
    `ready_for_review=True` when:

    - `verification` is empty
    - `target='backend'` and any path in `files` matches `views.py` /
      `serializers.py` / `schemas.py` AND `verification` does not contain a
      passing line for `/verify-openapi` (i.e. a string mentioning
      `verify-openapi` or `spectacular`).

    `next_role` (optional) — pipeline role to mention after the initiator
    (typically `reviewer` once `ready_for_review=True`). Tower resolves
    the role to its member UUID and stamps the mention.

    One HTTP call (`create_issue_comment`). No `list_issues` underneath.
    """
    if target not in {"backend", "frontend"}:
        raise TowerError(f"post_changes target must be backend|frontend, got {target!r}")
    ctx = _registry().resolve(workspace=workspace)
    next_mention = _role_mention(ctx, next_role) if next_role else ""
    sub_uuid = _ensure_uuid(sub_uuid, "sub_uuid")

    # Defenses on ready_for_review (no Plane API needed — run before HTTP)
    if ready_for_review:
        if not verification:
            raise TowerError("ready_for_review=True requires non-empty verification")
        if target == "backend":
            touches_api = any(
                re.search(r"(views|serializers|schemas)\.py$", path) for path, _ in (files or [])
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
        f"<p>{_initiator_mention(ctx)}{next_mention}</p>"
    )
    async with await _client_for(ctx) as plane:
        comment = await plane.create_issue_comment(ctx.config.project_id, sub_uuid, comment_html)
    return {
        "comment_id": comment.get("id"),
        "sub_uuid": sub_uuid,
        "ready_for_review": ready_for_review,
    }


# ----- post_bug_report (testers) -------------------------------------------


@mcp.tool()
async def post_bug_report(
    test_sub_uuid: str,
    severity: str,
    title: str,
    repro_steps: list[str],
    actual: str,
    expected: str,
    *,
    affected_sub_uuid: str | None = None,
    failing_tc: str = "",
    environment: dict[str, Any] | None = None,
    fix_hint: str = "",
    screenshots: list[str] | None = None,
    next_role: str | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Post an ISTQB bug report on the tester's sub-issue and (optionally)
    back-link the affected coder's sub-issue.

    `test_sub_uuid` is the tester's sub-issue receiving the bug comment
    (equals the spawn `issue_uuid`). `affected_sub_uuid` is the coder's
    sub-issue that gets the back-link; pass `None` to skip the back-link.

    `severity` ∈ {blocker, major, minor, cosmetic}.

    `next_role` (optional) — pipeline role to mention alongside the
    initiator (typically the coder responsible for the affected role).

    One HTTP call for the comment, one optional for the back-link. No
    `list_issues` underneath.
    """
    severity = severity.strip().lower()
    if severity not in {"blocker", "major", "minor", "cosmetic"}:
        raise TowerError("severity must be blocker|major|minor|cosmetic")

    ctx = _registry().resolve(workspace=workspace)
    next_mention = _role_mention(ctx, next_role) if next_role else ""
    test_sub_uuid = _ensure_uuid(test_sub_uuid, "test_sub_uuid")
    if affected_sub_uuid:
        affected_sub_uuid = _ensure_uuid(affected_sub_uuid, "affected_sub_uuid")

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

    affected_id_disp = affected_sub_uuid or "?"
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
        f"<p>{_initiator_mention(ctx)}{next_mention}</p>"
    )

    async with await _client_for(ctx) as plane:
        comment = await plane.create_issue_comment(
            ctx.config.project_id, test_sub_uuid, comment_html
        )
        # Back-link: a work-item link from the affected sub to the bug comment.
        if affected_sub_uuid:
            try:
                link_url = (
                    f"{ctx.config.plane_base_url}/{ctx.config.workspace_slug}/"
                    f"projects/{ctx.config.project_id}/issues/"
                    f"{test_sub_uuid}/#comment-{comment.get('id')}"
                )
                await plane.create_issue_link(
                    ctx.config.project_id,
                    affected_sub_uuid,
                    url=link_url,
                    title=f"[{severity}] {title}",
                )
            except PlaneAPIError:
                # Best-effort — the bug comment is the source of truth.
                pass

    return {
        "comment_id": comment.get("id"),
        "test_sub_uuid": test_sub_uuid,
        "affected_sub_uuid": affected_sub_uuid,
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
    upstream_role: str | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Post a `BLOCKED — upstream gap` comment in your own sub-issue. Used
    when a downstream agent (coder/tester/designer/reviewer) finds a defect
    that belongs upstream — missing FR, ambiguous AC, broken design contract.
    Do not patch locally; the upstream role updates its existing artifact.

    `upstream_role` (optional) — the role that owns the gap (e.g.
    `system-analyst` for a SPEC defect, `business-analyst` for a missing
    FR). Tower mentions both initiator and upstream; agent never types a
    UUID.
    """
    ctx = _registry().resolve(workspace=workspace)
    upstream_mention = _role_mention(ctx, upstream_role) if upstream_role else ""
    my_sub_uuid = _ensure_uuid(my_sub_uuid, "my_sub_uuid")
    comment_html = (
        "<p><strong>BLOCKED — upstream gap.</strong></p>"
        f"<p>Affected: <code>{_esc(affected)}</code></p>"
        f"<p>Issue: {_esc(issue)}</p>"
        f"<p>Proposed resolution: {_esc(proposed_resolution)}</p>"
        f"<p>{_initiator_mention(ctx)}{upstream_mention}</p>"
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


# ----- request_handoff (replaces hand-rolled mention HTML) -----------------


@mcp.tool()
async def request_handoff(
    sub_uuid: str,
    target_role: str,
    *,
    message_html: str = "",
    workspace: str | None = None,
) -> dict[str, Any]:
    """Post a handoff comment on `sub_uuid` mentioning the next pipeline role.

    Use this when an agent has finished its artifact and wants the next role
    to pick up — instead of constructing a free-form comment with a hand-typed
    `<mention-component>` (the Beck/Sark self-mention failure mode).

    `target_role` is matched against the workspace's `agents` roster by
    `prompt_role` bare name (so `architect`, `business-analyst`, `reviewer`,
    `django-developer`, `react-developer`, `system-analyst`, `designer`,
    `api-tester`, `ui-tester`, etc. — whatever your conductor.d/*.yaml
    declares). Tower resolves the nickname → member UUID and stamps the
    mention itself. The agent never types a UUID.

    `message_html` is optional context (e.g. one-line gist). It must NOT
    contain `<mention-component>` tags — those are tower-managed.

    Returns `{comment_id, target_role, target_uuid, sub_uuid}`.
    """
    _assert_no_mentions(message_html)
    ctx = _registry().resolve(workspace=workspace)
    sub_uuid = _ensure_uuid(sub_uuid, "sub_uuid")
    target_mention = _role_mention(ctx, target_role)
    # _role_mention returns the full HTML; extract the UUID for the response.
    m = re.search(r'entity_identifier="([^"]+)"', target_mention)
    target_uuid = m.group(1) if m else ""
    body = f"<p>{message_html}</p>" if message_html else ""
    comment_html = f"{body}<p>{target_mention}</p>"
    async with await _client_for(ctx) as plane:
        comment = await plane.create_issue_comment(ctx.config.project_id, sub_uuid, comment_html)
    return {
        "comment_id": comment.get("id"),
        "target_role": target_role,
        "target_uuid": target_uuid,
        "sub_uuid": sub_uuid,
    }


# ----- post_comment / update_comment (free-form, mention-blocked) ----------


@mcp.tool()
async def post_comment(
    work_item_uuid: str,
    comment_html: str,
    *,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Generic comment on any work item (sub-issue or root). Lower-level than
    post_review / post_changes / post_bug_report — use those when they fit.

    Refuses any `<mention-component>` in `comment_html`: mentions are
    tower-managed (use `request_handoff` for handoffs, or `next_role=…` on
    a structured tool). This blocks the self-mention failure mode at the
    tool boundary.
    """
    _assert_no_mentions(comment_html)
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

    Same mention restriction as `post_comment` — refuses `<mention-component>`.
    """
    _assert_no_mentions(comment_html)
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
