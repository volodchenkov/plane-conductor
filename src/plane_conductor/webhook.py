"""Plane webhook handler — multi-workspace.

Mounts `POST /<workspace_slug>/webhook` per configured workspace. Each route
verifies HMAC against that workspace's secret and dispatches against that
workspace's agent roster + Plane client.

End-to-end per request:
  1. Resolve workspace from URL path.
  2. HMAC-SHA256 verify the raw body with that workspace's secret.
  3. Parse JSON. Bail unless this is a comment-created/updated event.
  4. Pull `<mention-component entity_identifier="UUID">` UUIDs from comment_html.
  5. For each UUID: skip the workspace's initiator → look up email via
     workspace's Plane client → split nickname → check it's a configured
     agent for this workspace → spawn it.
  6. Return 200 with a small report. On transient Plane errors return 503.
"""

from __future__ import annotations

import hmac
import re
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, status

from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.config import Settings
from plane_conductor.exceptions import (
    AgentSpawnError,
    CapacityFullError,
    PlaneAPIError,
    SessionAlreadyRunningError,
)
from plane_conductor.logging_config import get_logger
from plane_conductor.plane_client import PlaneClient
from plane_conductor.runner import Runner

log = get_logger(__name__)

_MENTION_RE = re.compile(
    r"<mention-component\b[^>]*?entity_identifier=\"([0-9a-fA-F-]{36})\"",
    re.IGNORECASE,
)


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """Constant-time HMAC-SHA256 verification. Accepts plain hex or `sha256=<hex>`."""
    if not signature:
        return False
    if signature.startswith("sha256="):
        signature = signature[len("sha256=") :]
    expected = hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()
    return hmac.compare_digest(expected, signature.lower().strip())


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


def _email_of(member: dict[str, Any]) -> str | None:
    email = member.get("email")
    if isinstance(email, str) and email:
        return email
    inner = member.get("member")
    if isinstance(inner, dict):
        v = inner.get("email")
        if isinstance(v, str) and v:
            return v
    return None


def _is_nickname_allowed(
    nickname: str, workspace: WorkspaceConfig, agents_by_nick: dict[str, Any]
) -> bool:
    if nickname not in agents_by_nick:
        return False
    allow = workspace.allowed_nicknames_set
    if not allow:
        return True
    return nickname in allow


async def _resolve_pending_agent_member(
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
        email = _email_of(member)
        if not email:
            continue
        nickname = email.split("@", 1)[0].lower()
        if not _is_nickname_allowed(nickname, workspace, agents_by_nick):
            continue
        return actor_uuid
    return None


def build_router(
    settings: Settings,
    workspaces: dict[str, tuple[WorkspaceConfig, PlaneClient]],
    runner: Runner,
) -> APIRouter:
    """Build a FastAPI router with one /<slug>/webhook route per workspace.

    `workspaces` maps slug → (WorkspaceConfig, PlaneClient). The router closes
    over each entry so each endpoint uses its own secret + plane client.
    """
    router = APIRouter()

    if not workspaces:
        log.warning("no_workspaces_loaded", conductor_dir=str(settings.conductor_dir))

    for slug, (workspace, plane) in workspaces.items():
        _register_workspace_route(router, slug, workspace, plane, runner)
    return router


def _register_workspace_route(
    router: APIRouter,
    slug: str,
    workspace: WorkspaceConfig,
    plane: PlaneClient,
    runner: Runner,
) -> None:
    sig_header = workspace.webhook_signature_header
    agents_by_nick = workspace.agents_by_nickname()
    initiator_uuid = workspace.initiator_uuid
    secret = workspace.webhook_secret

    @router.post(f"/{slug}/webhook", name=f"webhook_{slug}")
    async def receive(  # type: ignore[no-untyped-def]
        request: Request,
        x_signature: str | None = Header(default=None, alias=sig_header),
    ):
        body = await request.body()
        if not verify_signature(secret, body, x_signature):
            log.warning("webhook_signature_mismatch", workspace=slug, header=sig_header)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad signature")

        try:
            payload = await request.json()
        except ValueError as exc:
            log.warning("webhook_invalid_json", workspace=slug, error=str(exc))
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="invalid json"
            ) from exc

        event = payload.get("event")
        action = payload.get("action")
        if event not in {"issue_comment", "comment"} or action not in {"created", "updated"}:
            return {"ok": True, "workspace": slug, "ignored": event}

        data = payload.get("data") or {}
        comment_html = data.get("comment_html") or ""
        issue_raw = data.get("issue")
        if not issue_raw:
            return {"ok": True, "workspace": slug, "ignored": "no issue id"}
        try:
            issue_uuid = UUID(str(issue_raw))
        except ValueError:
            return {"ok": True, "workspace": slug, "ignored": "bad issue uuid"}

        mention_uuids = extract_mention_uuids(comment_html)
        auto_resumed: str | None = None
        if not mention_uuids:
            actor_raw = data.get("actor")
            if actor_raw is not None and str(actor_raw).lower() == str(initiator_uuid).lower():
                # Initiator replied without an explicit @-mention. Plane's
                # mention component is rejected by tower in outbound agent
                # comments, so agents end runs via `request_handoff(
                # target_role='initiator', …)` — which stamps the initiator
                # mention at the top of the agent's last comment. Use that
                # signal to identify which agent is waiting and respawn it.
                try:
                    resumed_member = await _resolve_pending_agent_member(
                        plane=plane,
                        workspace=workspace,
                        initiator_uuid=initiator_uuid,
                        issue_uuid=issue_uuid,
                        agents_by_nick=agents_by_nick,
                    )
                except PlaneAPIError as exc:
                    # Helper re-raises only on transient (5xx). Match the
                    # mention-driven path's behaviour: ask Plane to retry.
                    log.warning(
                        "webhook_returning_503_for_retry",
                        workspace=slug,
                        reason="auto_resume_probe",
                        status=exc.status_code,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="plane api transient error; retry",
                    ) from exc
                if resumed_member is not None:
                    mention_uuids = [resumed_member]
                    auto_resumed = str(resumed_member)
                    log.info(
                        "auto_resume_triggered",
                        workspace=slug,
                        issue=str(issue_uuid),
                        member=auto_resumed,
                    )
        if not mention_uuids:
            return {
                "ok": True,
                "workspace": slug,
                "spawned": [],
                "ts": datetime.now(UTC).isoformat(),
            }

        spawned: list[str] = []
        skipped: list[dict[str, str]] = []
        for member_id in mention_uuids:
            if member_id == initiator_uuid:
                continue

            try:
                member = await plane.get_member(member_id)
            except PlaneAPIError as exc:
                if exc.is_transient:
                    log.warning(
                        "webhook_returning_503_for_retry",
                        workspace=slug,
                        reason="member_lookup",
                        member_id=str(member_id),
                        status=exc.status_code,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="plane api transient error; retry",
                    ) from exc
                log.warning(
                    "member_lookup_failed",
                    workspace=slug,
                    member_id=str(member_id),
                    status=exc.status_code,
                )
                skipped.append({"member_id": str(member_id), "reason": "lookup failed"})
                continue

            email = _email_of(member)
            if not email:
                skipped.append({"member_id": str(member_id), "reason": "no email"})
                continue
            nickname = email.split("@", 1)[0].lower()

            if not _is_nickname_allowed(nickname, workspace, agents_by_nick):
                log.warning("nickname_not_allowed", workspace=slug, nickname=nickname, email=email)
                skipped.append({"nickname": nickname, "reason": "not allowed"})
                continue

            try:
                await runner.spawn(
                    workspace=workspace,
                    plane=plane,
                    nickname=nickname,
                    issue_uuid=issue_uuid,
                    triggered_by_email=email,
                )
                spawned.append(nickname)
            except SessionAlreadyRunningError:
                log.info(
                    "duplicate_trigger",
                    workspace=slug,
                    nickname=nickname,
                    issue=str(issue_uuid),
                )
                skipped.append({"nickname": nickname, "reason": "already running"})
            except CapacityFullError:
                log.warning("capacity_full", workspace=slug, nickname=nickname)
                skipped.append({"nickname": nickname, "reason": "capacity full"})
            except AgentSpawnError as exc:
                log.error("spawn_failed", workspace=slug, nickname=nickname, error=str(exc))
                skipped.append({"nickname": nickname, "reason": "spawn failed"})

        result: dict[str, Any] = {
            "ok": True,
            "workspace": slug,
            "spawned": spawned,
            "skipped": skipped,
        }
        if auto_resumed is not None:
            result["auto_resumed"] = auto_resumed
        return result
