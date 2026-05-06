"""Attach the configured bot accounts to the project (idempotent).

Workspace-level invitation (see `create_users.invite_roster`) only puts the bots
in the workspace — they can authenticate, but cannot see or comment on a
specific project until they are added as project members. This step closes that
gap so a freshly bootstrapped workspace is ready for agent traffic without a
manual UI dance.
"""

from __future__ import annotations

from typing import Any

from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.exceptions import PlaneAPIError
from plane_conductor.logging_config import get_logger
from plane_conductor.plane_client import PlaneClient

log = get_logger(__name__)


def _email_to_member_id(members: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in members:
        email = m.get("email")
        member_id = m.get("id")
        if isinstance(email, str) and isinstance(member_id, str):
            out[email.lower()] = member_id
            continue
        inner = m.get("member")
        if isinstance(inner, dict):
            inner_email = inner.get("email")
            inner_id = inner.get("id")
            if isinstance(inner_email, str) and isinstance(inner_id, str):
                out[inner_email.lower()] = inner_id
    return out


def _existing_member_ids(project_members: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for m in project_members:
        mid = m.get("id")
        if isinstance(mid, str):
            out.add(mid)
        inner = m.get("member")
        if isinstance(inner, dict):
            inner_id = inner.get("id")
            if isinstance(inner_id, str):
                out.add(inner_id)
    return out


async def add_roster_to_project(
    plane: PlaneClient,
    workspace: WorkspaceConfig,
    *,
    dry_run: bool = False,
) -> dict[str, str]:
    """Attach every roster bot to `workspace.project_id`. Returns {nickname: status}.

    Status values: 'added', 'exists', 'pending_invite', 'failed'.

    'pending_invite' means the workspace invitation has not been accepted yet
    (the user is not in `list_workspace_members` output) — re-run after the bot
    accepts.
    """
    workspace_members = await plane.list_workspace_members()
    email_to_id = _email_to_member_id(workspace_members)

    project_members = await plane.list_project_members(workspace.project_id)
    already_in_project = _existing_member_ids(project_members)

    statuses: dict[str, str] = {}
    for agent in workspace.agents:
        email = f"{agent.nickname}@{workspace.email_domain}".lower()
        member_id = email_to_id.get(email)

        if member_id is None:
            log.info("project_member_pending_invite", nickname=agent.nickname, email=email)
            statuses[agent.nickname] = "pending_invite"
            continue

        if member_id in already_in_project:
            log.info("project_member_exists", nickname=agent.nickname, email=email)
            statuses[agent.nickname] = "exists"
            continue

        if dry_run:
            log.info("project_member_add_dry_run", nickname=agent.nickname, email=email)
            statuses[agent.nickname] = "added"
            continue

        try:
            await plane.add_project_member(workspace.project_id, member_id)
            log.info("project_member_added", nickname=agent.nickname, email=email)
            statuses[agent.nickname] = "added"
        except PlaneAPIError as exc:
            if exc.status_code in (400, 409):
                log.info("project_member_already_added", nickname=agent.nickname, email=email)
                statuses[agent.nickname] = "exists"
                continue
            log.error(
                "project_member_add_failed",
                nickname=agent.nickname,
                email=email,
                status=exc.status_code,
                error=exc.message,
            )
            statuses[agent.nickname] = "failed"

    return statuses
