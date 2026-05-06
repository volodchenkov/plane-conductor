"""Async client for the Plane REST API.

Only the endpoints Plane Conductor actually uses are wrapped here. Agents speak
to Plane through the official MCP server; this client is for orchestrator
concerns (member lookup, label/state setup, failure notifications).

The exact endpoint shape is taken from the Plane self-hosted v1 public API. If
your Plane build differs, override `Settings.plane_base_url` and adjust paths
in `_BASE_PATH` style helpers below.
"""

from __future__ import annotations

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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.workspace_slug = workspace_slug
        self._owned_client = client is None
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
        await self.aclose()

    async def aclose(self) -> None:
        if self._owned_client:
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
        try:
            resp = await self._client.request(method, path, json=json, params=params)
        except httpx.HTTPError as exc:
            raise PlaneAPIError(0, f"transport error: {exc}", url=path) from exc

        if resp.status_code >= 400:
            text = resp.text
            raise PlaneAPIError(resp.status_code, text, url=path)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

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
