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

        return {"ok": True, "workspace": slug, "spawned": spawned, "skipped": skipped}
