"""Async client for the Plane REST API.

Only the endpoints Plane Conductor actually uses are wrapped here. Agents speak
to Plane through the official MCP server; this client is for orchestrator
concerns (member lookup, label/state setup, failure notifications).

The exact endpoint shape is taken from the Plane self-hosted v1 public API. If
your Plane build differs, override `Settings.plane_base_url` and adjust paths
in `_BASE_PATH` style helpers below.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Any, cast
from uuid import UUID

import httpx

from plane_conductor.exceptions import PlaneAPIError


class PlaneClient:
    """Thin async wrapper around the subset of Plane v1 API we need."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        workspace_slug: str,
        *,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        shared: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.workspace_slug = workspace_slug
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep: Callable[[float], Awaitable[None]] = sleep or asyncio.sleep
        self._owned_client = client is None
        # `shared=True` opts out of `async with`/`aclose()` closing the
        # underlying httpx.AsyncClient. Used by tower's per-workspace
        # singleton — see `_SHARED_CLIENTS` in mcp_tower.py — so the keep-alive
        # pool survives across MCP tool calls. Existing per-call callsites
        # (and tests) keep `shared=False` and close as before.
        self._shared = shared
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={
                "X-Api-Key": api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    async def __aenter__(self) -> PlaneClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self._shared:
            await self.aclose()

    async def aclose(self) -> None:
        if self._owned_client and not self._shared:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Issue one request, retrying on HTTP 429 up to `max_retries` times.

        Retry-After is honoured when the server provides it (Plane sends a
        whole number of seconds via DRF's `ApiKeyRateThrottle`). Without
        Retry-After, falls back to exponential backoff `backoff_base * 2**attempt`.
        Other 4xx/5xx pass through to the existing PlaneAPIError path.

        Retries are safe because 429 is raised by the throttle layer **before**
        the request handler runs — the side-effecting handler is never entered
        on a throttled call, so retrying a POST does not duplicate work.
        """
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._client.request(method, path, json=json, params=params)
            except httpx.HTTPError as exc:
                raise PlaneAPIError(0, f"transport error: {exc}", url=path) from exc

            if resp.status_code == 429 and attempt < self.max_retries:
                delay = self._parse_retry_after(resp.headers.get("Retry-After"), attempt)
                await self._sleep(delay)
                continue
            break

        if resp.status_code >= 400:
            text = resp.text
            raise PlaneAPIError(resp.status_code, text, url=path)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def _parse_retry_after(self, header: str | None, attempt: int) -> float:
        """Parse a Retry-After response header. Returns seconds to sleep.

        Spec (RFC 7231 §7.1.3) allows either an integer (delta-seconds) or
        an HTTP-date. Plane sends integers, so we only handle that form
        plus an exponential-backoff fallback for missing/malformed values.
        """
        if header is not None:
            stripped = header.strip()
            if stripped.isdigit():
                return float(stripped)
        return float(self.backoff_base * (2**attempt))

    @staticmethod
    def _results(payload: Any) -> list[dict[str, Any]]:
        """Plane endpoints sometimes return a paginated dict, sometimes a list."""
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and "results" in payload:
            results = payload["results"]
            if isinstance(results, list):
                return results
        return []

    # ------------------------------------------------------------------
    # workspace / members
    # ------------------------------------------------------------------
    #
    # NOTE: Plane v1 API key auth is per-workspace and exposes only the routes
    # under `/api/v1/workspaces/<slug>/{projects,members,invitations,...}`. The
    # bare `/api/v1/workspaces/<slug>/` endpoint is session-only (web frontend)
    # and returns 401 for API keys — don't use it.

    async def ping(self) -> list[dict[str, Any]]:
        """Smoke-check connectivity & auth. Returns the project list (cheapest
        whitelisted endpoint that proves the key works for this workspace)."""
        payload = await self._request("GET", f"/api/v1/workspaces/{self.workspace_slug}/projects/")
        return self._results(payload)

    async def list_workspace_members(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", f"/api/v1/workspaces/{self.workspace_slug}/members/")
        return self._results(payload)

    async def get_member(self, member_id: str | UUID) -> dict[str, Any]:
        """Return one workspace member record by UUID.

        Plane's single-member GET (`/members/<id>/`) returns 404 for API keys;
        only the list endpoint works. We fetch the list and filter locally.
        """
        member_id_str = str(member_id)
        for m in await self.list_workspace_members():
            if str(m.get("id")) == member_id_str:
                return m
            member_obj = m.get("member") if isinstance(m.get("member"), dict) else None
            if member_obj and str(member_obj.get("id")) == member_id_str:
                return m
        raise PlaneAPIError(404, f"member {member_id_str} not found in workspace")

    async def invite_member(
        self,
        email: str,
        *,
        role: int = 15,
    ) -> dict[str, Any] | None:
        """Send a workspace invitation. `role` follows Plane's role ints (15 = Member).

        Plane self-hosted v1 expects a single object, not an array — even though
        the workspace settings UI lets you paste multiple emails, those become
        N separate POSTs.
        """
        payload = {"email": email, "role": role}
        return cast(
            "dict[str, Any] | None",
            await self._request(
                "POST",
                f"/api/v1/workspaces/{self.workspace_slug}/invitations/",
                json=payload,
            ),
        )

    # ------------------------------------------------------------------
    # project membership
    # ------------------------------------------------------------------

    async def list_project_members(self, project_id: str | UUID) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET",
            f"/api/v1/workspaces/{self.workspace_slug}/projects/{project_id}/members/",
        )
        return self._results(payload)

    async def add_project_member(
        self,
        project_id: str | UUID,
        member_id: str | UUID,
        *,
        role: int = 15,
    ) -> dict[str, Any]:
        """Attach an existing workspace member to a project. `role` uses Plane's role ints
        (15 = Member, 20 = Admin). The member must already exist in the workspace —
        invite via `invite_member` first if not.

        Plane's project-members endpoint takes `member` (singular UUID), not the bulk
        `members:[...]` shape its label/state cousins use.
        """
        payload = {"member": str(member_id), "role": role}
        return cast(
            "dict[str, Any]",
            await self._request(
                "POST",
                f"/api/v1/workspaces/{self.workspace_slug}/projects/{project_id}/members/",
                json=payload,
            ),
        )

    # ------------------------------------------------------------------
    # labels
    # ------------------------------------------------------------------

    async def list_labels(self, project_id: str | UUID) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET",
            f"/api/v1/workspaces/{self.workspace_slug}/projects/{project_id}/labels/",
        )
        return self._results(payload)

    async def create_label(
        self,
        project_id: str | UUID,
        name: str,
        *,
        color: str | None = None,
        description: str = "",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name, "description": description}
        if color:
            body["color"] = color
        return cast(
            "dict[str, Any]",
            await self._request(
                "POST",
                f"/api/v1/workspaces/{self.workspace_slug}/projects/{project_id}/labels/",
                json=body,
            ),
        )

    # ------------------------------------------------------------------
    # states
    # ------------------------------------------------------------------

    async def list_states(self, project_id: str | UUID) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET",
            f"/api/v1/workspaces/{self.workspace_slug}/projects/{project_id}/states/",
        )
        return self._results(payload)

    async def create_state(
        self,
        project_id: str | UUID,
        name: str,
        *,
        group: str,
        color: str = "#cccccc",
    ) -> dict[str, Any]:
        body = {"name": name, "group": group, "color": color}
        return cast(
            "dict[str, Any]",
            await self._request(
                "POST",
                f"/api/v1/workspaces/{self.workspace_slug}/projects/{project_id}/states/",
                json=body,
            ),
        )

    # ------------------------------------------------------------------
    # issues + comments
    # ------------------------------------------------------------------

    async def get_issue(self, project_id: str | UUID, issue_id: str | UUID) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            await self._request(
                "GET",
                f"/api/v1/workspaces/{self.workspace_slug}/projects/{project_id}/issues/{issue_id}/",
            ),
        )

    #: Default field whitelist for `list_issues`. Plane v1's project-issues
    #: endpoint returns `description_html` on every record by default, which
    #: turns a 51-issue project with one bloated SPEC into a ~700 KB JSON
    #: response — well past Claude Code's MCP tool-result token cap, which
    #: causes every agent that calls `find_artifact_by_label` (i.e. ALL of
    #: them, on entry) to hang silently in `--print` mode. Tower callers
    #: only ever filter by `parent`/`labels` and project these metadata
    #: fields onward, so the listing payload can be trimmed at the API
    #: boundary. `?fields=` is honoured by Plane; `?parent=` is silently
    #: ignored (verified against plane.suze.io).
    LIST_ISSUES_DEFAULT_FIELDS = "id,name,sequence_id,parent,labels,state,created_at,updated_at"

    async def list_issues(
        self,
        project_id: str | UUID,
        *,
        per_page: int = 100,
        fields: str | None = LIST_ISSUES_DEFAULT_FIELDS,
    ) -> list[dict[str, Any]]:
        """List ALL work items in a project, following pagination cursors.

        Plane MCP has no `parent` filter, callers post-filter by
        `parent == <root_uuid>` themselves. The duplicate-detection invariants
        in the tower depend on seeing the full set, so we walk every page.

        By default a thin field whitelist is sent so `description_html` is
        not pulled per row (see `LIST_ISSUES_DEFAULT_FIELDS`). Pass
        `fields=None` to request the full record — required only when a
        caller genuinely needs the body, which tower paths don't.
        """
        path = f"/api/v1/workspaces/{self.workspace_slug}/projects/{project_id}/issues/"
        params: dict[str, Any] = {"per_page": per_page}
        if fields:
            params["fields"] = fields
        out: list[dict[str, Any]] = []
        seen_cursors: set[str] = set()
        while True:
            payload = await self._request("GET", path, params=params)
            out.extend(self._results(payload))
            if not isinstance(payload, dict):
                break
            cursor = payload.get("next_cursor") or payload.get("next")
            if not cursor or not isinstance(cursor, str) or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
            params = {"per_page": per_page, "cursor": cursor}
            if fields:
                params["fields"] = fields
        return out

    async def get_project(self, project_id: str | UUID) -> dict[str, Any]:
        """Fetch a single project (identifier, name, ...). Wrap the GET so the
        tower doesn't reach into `_request` directly."""
        return cast(
            "dict[str, Any]",
            await self._request(
                "GET",
                f"/api/v1/workspaces/{self.workspace_slug}/projects/{project_id}/",
            ),
        )

    async def create_issue_link(
        self,
        project_id: str | UUID,
        issue_id: str | UUID,
        *,
        url: str,
        title: str = "",
    ) -> dict[str, Any] | None:
        """Attach an external URL to an issue (POST .../issues/<id>/links/).
        Used by the tower to back-link bug reports to coder sub-issues."""
        return cast(
            "dict[str, Any] | None",
            await self._request(
                "POST",
                f"/api/v1/workspaces/{self.workspace_slug}/projects/{project_id}/issues/{issue_id}/links/",
                json={"url": url, "title": title},
            ),
        )

    async def create_issue(
        self,
        project_id: str | UUID,
        *,
        name: str,
        parent: str | UUID | None = None,
        description_html: str | None = None,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name}
        if parent is not None:
            body["parent"] = str(parent)
        if description_html is not None:
            body["description_html"] = description_html
        if labels:
            body["labels"] = [str(lbl) for lbl in labels]
        if assignees:
            body["assignees"] = [str(a) for a in assignees]
        return cast(
            "dict[str, Any]",
            await self._request(
                "POST",
                f"/api/v1/workspaces/{self.workspace_slug}/projects/{project_id}/issues/",
                json=body,
            ),
        )

    async def update_issue(
        self,
        project_id: str | UUID,
        issue_id: str | UUID,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        """PATCH an issue. Pass an explicit `fields` dict (caller serialises
        UUIDs to str). Explicit dict (not `**fields`) so caller-side typos like
        `description_htm=` raise at the call site instead of silently no-op'ing.
        """
        return cast(
            "dict[str, Any]",
            await self._request(
                "PATCH",
                f"/api/v1/workspaces/{self.workspace_slug}/projects/{project_id}/issues/{issue_id}/",
                json=fields,
            ),
        )

    async def list_issue_comments(
        self,
        project_id: str | UUID,
        issue_id: str | UUID,
    ) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET",
            f"/api/v1/workspaces/{self.workspace_slug}/projects/{project_id}/issues/{issue_id}/comments/",
        )
        return self._results(payload)

    async def create_issue_comment(
        self,
        project_id: str | UUID,
        issue_id: str | UUID,
        comment_html: str,
    ) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            await self._request(
                "POST",
                f"/api/v1/workspaces/{self.workspace_slug}/projects/{project_id}/issues/{issue_id}/comments/",
                json={"comment_html": comment_html},
            ),
        )

    async def update_issue_comment(
        self,
        project_id: str | UUID,
        issue_id: str | UUID,
        comment_id: str | UUID,
        comment_html: str,
    ) -> dict[str, Any] | None:
        return cast(
            "dict[str, Any] | None",
            await self._request(
                "PATCH",
                f"/api/v1/workspaces/{self.workspace_slug}/projects/{project_id}/issues/{issue_id}/comments/{comment_id}/",
                json={"comment_html": comment_html},
            ),
        )
