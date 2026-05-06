"""Invite the configured bot accounts into the Plane workspace (idempotent)."""

from __future__ import annotations

from typing import Any

from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.exceptions import PlaneAPIError
from plane_conductor.logging_config import get_logger
from plane_conductor.plane_client import PlaneClient

log = get_logger(__name__)


def _existing_emails(members: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for m in members:
        email = m.get("email")
        if isinstance(email, str):
            out.add(email.lower())
            continue
        inner = m.get("member")
        if isinstance(inner, dict):
            inner_email = inner.get("email")
            if isinstance(inner_email, str):
                out.add(inner_email.lower())
    return out


async def invite_roster(
    plane: PlaneClient,
    workspace: WorkspaceConfig,
    *,
    dry_run: bool = False,
) -> dict[str, str]:
    """Invite every agent listed in `workspace.agents`. Returns {nickname: status}.

    Status values: 'invited', 'exists', 'failed'.
    """
    members = await plane.list_workspace_members()
    existing = _existing_emails(members)

    statuses: dict[str, str] = {}
    for agent in workspace.agents:
        email = f"{agent.nickname}@{workspace.email_domain}".lower()
        if email in existing:
            log.info("user_exists", nickname=agent.nickname, email=email)
            statuses[agent.nickname] = "exists"
            continue

        if dry_run:
            log.info("user_invite_dry_run", nickname=agent.nickname, email=email)
            statuses[agent.nickname] = "invited"
            continue

        try:
            await plane.invite_member(email)
            log.info("user_invited", nickname=agent.nickname, email=email)
            statuses[agent.nickname] = "invited"
        except PlaneAPIError as exc:
            if exc.status_code in (400, 409):
                log.info("user_invite_already_pending", nickname=agent.nickname, email=email)
                statuses[agent.nickname] = "exists"
                continue
            log.error(
                "user_invite_failed",
                nickname=agent.nickname,
                email=email,
                status=exc.status_code,
                error=exc.message,
            )
            statuses[agent.nickname] = "failed"

    return statuses
