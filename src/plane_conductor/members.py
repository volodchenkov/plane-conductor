"""Mention / member helpers shared by the webhook receiver and the Dispatcher.

Pure functions over Plane payloads and a ``PlaneClient``: extracting mentioned member UUIDs
from a comment, turning a member record into a nickname, the per-workspace allow-list, and the
auto-resume probe ("which agent is waiting on the initiator on this issue?"). They live here so
the service layer (``dispatcher.py``) does not import from the HTTP handler (``webhook.py``).
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.exceptions import PlaneAPIError
from plane_conductor.logging_config import get_logger
from plane_conductor.plane_client import PlaneClient

log = get_logger(__name__)

_MENTION_RE = re.compile(
    r"<mention-component\b[^>]*?entity_identifier=\"([0-9a-fA-F-]{36})\"",
    re.IGNORECASE,
)


def extract_mention_uuids(comment_html: str) -> list[UUID]:
    """Extract entity_identifier UUIDs in document order, deduplicated."""
    if not comment_html:
        return []
    seen: set[str] = set()
    out: list[UUID] = []
    for m in _MENTION_RE.finditer(comment_html):
        raw = m.group(1).lower()
        if raw in seen:
            continue
        seen.add(raw)
        try:
            out.append(UUID(raw))
        except ValueError:
            continue
    return out


def email_of(member: dict[str, Any]) -> str | None:
    email = member.get("email")
    if isinstance(email, str) and email:
        return email
    inner = member.get("member")
    if isinstance(inner, dict):
        v = inner.get("email")
        if isinstance(v, str) and v:
            return v
    return None


def is_nickname_allowed(
    nickname: str, workspace: WorkspaceConfig, agents_by_nick: dict[str, Any]
) -> bool:
    if nickname not in agents_by_nick:
        return False
    allow = workspace.allowed_nicknames_set
    if not allow:
        return True
    return nickname in allow


async def resolve_pending_agent_member(
    plane: PlaneClient,
    workspace: WorkspaceConfig,
    initiator_uuid: UUID,
    issue_uuid: UUID,
    agents_by_nick: dict[str, Any],
) -> UUID | None:
    """Find the agent (if any) currently waiting on the initiator for `issue_uuid`.

    Returns the member UUID of the latest agent whose most recent comment on
    the issue opens with an initiator `<mention-component>` (the auto-stamp
    written by tower's `request_handoff(target_role='initiator', …)`). That's
    the structured «I'm awaiting your input» signal agents already emit
    today, so we read it instead of inventing a new state-tracking surface.

    Returns None when there's no such comment, when the comment author isn't
    a registered agent for this workspace, or when a 4xx Plane lookup fails.
    The caller treats a None as «no auto-resume, return the original no-op».

    **Transient (5xx) Plane errors re-raise** — webhook handler turns them
    into a 503 so Plane retries the webhook delivery, same contract as the
    mention-driven spawn path.
    """
    try:
        comments = await plane.list_issue_comments(workspace.project_id, issue_uuid)
    except PlaneAPIError as exc:
        if exc.is_transient:
            raise
        log.info(
            "auto_resume_comments_lookup_failed",
            workspace=workspace.workspace_slug,
            issue=str(issue_uuid),
            status=exc.status_code,
        )
        return None

    initiator_uuid_obj = initiator_uuid
    comments_sorted = sorted(comments, key=lambda c: c.get("created_at", ""), reverse=True)
    for comment in comments_sorted:
        actor_raw = comment.get("actor")
        if not actor_raw:
            continue
        actor_str = str(actor_raw).lower()
        if actor_str == str(initiator_uuid_obj).lower():
            continue
        # Require the structured `<mention-component entity_identifier="<initiator>">`
        # tag as the first mention in the body — NOT just a substring match on the
        # raw UUID. The tag is what tower stamps on `request_handoff`; raw UUID text
        # in someone's prose is conversational noise and must not trigger a resume.
        mentions = extract_mention_uuids(comment.get("comment_html") or "")
        if not mentions or mentions[0] != initiator_uuid_obj:
            continue
        try:
            actor_uuid = UUID(actor_str)
        except ValueError:
            continue
        try:
            member = await plane.get_member(actor_uuid)
        except PlaneAPIError as exc:
            if exc.is_transient:
                raise
            continue
        email = email_of(member)
        if not email:
            continue
        nickname = email.split("@", 1)[0].lower()
        if not is_nickname_allowed(nickname, workspace, agents_by_nick):
            continue
        return actor_uuid
    return None
