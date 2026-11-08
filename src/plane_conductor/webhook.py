"""Plane webhook receiver — multi-workspace, thin and durable-first (QSALE-55 ADR-6).

Mounts `POST /<workspace_slug>/webhook` per configured workspace. Each route
verifies HMAC against that workspace's secret.

Plane retries only *transport* failures of a delivery, never a non-2xx answer, so the
old "503 → Plane redelivers" contract is void. The receiver therefore does no Plane call
on the request path: it persists what it was told and answers 200 meaning "durably
accepted".

Per request:
  1. Resolve workspace from URL path; HMAC-SHA256 verify the raw body.
  2. Parse JSON. Bail unless this is a comment-created/updated event.
  3. Pull `<mention-component entity_identifier="UUID">` UUIDs from comment_html and hand
     gates / role handoffs to the workbench notifier (unchanged).
  4. For each mentioned member (the initiator is skipped) `INSERT OR IGNORE` one row into
     `triggers` — unique `(comment_id, member_uuid)`: a redelivered webhook or an edit that
     adds no addressee creates nothing, an edit that adds one creates one. A comment of the
     initiator without any mention becomes an `auto_resume` row.
  5. Wake the Dispatcher (`dispatcher.py`), which resolves member → nickname → root →
     source run and decides claim / merge / defer / queue.
A failed durable write answers 503 and logs `state_write_failed` at ERROR.
"""

from __future__ import annotations

import hmac
import json
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, status

from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.config import Settings
from plane_conductor.logging_config import get_logger
from plane_conductor.members import extract_mention_uuids
from plane_conductor.plane_client import PlaneClient
from plane_conductor.store import Store
from plane_conductor.workbench_client import Notifier, detect_gate, detect_handoff

log = get_logger(__name__)


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """Constant-time HMAC-SHA256 verification. Accepts plain hex or `sha256=<hex>`."""
    if not signature:
        return False
    if signature.startswith("sha256="):
        signature = signature[len("sha256=") :]
    expected = hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()
    return hmac.compare_digest(expected, signature.lower().strip())


def _publish_gate(
    notifier: Notifier | None,
    workspace: WorkspaceConfig,
    data: dict[str, Any],
    comment_html: str,
    mentions: list[UUID],
    issue_uuid: UUID,
) -> None:
    """Hand a gate (an initiator-addressed handoff) or a role handoff to the workbench spool.

    A local file write only; any failure is logged and the webhook is unaffected.
    """
    if notifier is None:
        return
    try:
        args = (
            data.get("id"),
            comment_html,
            mentions,
            data.get("actor"),
            workspace.initiator_uuid,
            workspace.workspace_slug,
            str(issue_uuid),
            workspace.plane_base_url,
            str(workspace.project_id),
        )
        event = detect_gate(*args) or detect_handoff(*args)
        if event is not None:
            # the root issue is resolved by the sender task, never here
            notifier.publish(
                event,
                {
                    "workspace": workspace.workspace_slug,
                    "project": str(workspace.project_id),
                    "issue": str(issue_uuid),
                },
            )
    except Exception as exc:  # never break the Plane webhook
        log.warning("gate_publish_failed", error=type(exc).__name__)


def build_router(
    settings: Settings,
    workspaces: dict[str, tuple[WorkspaceConfig, PlaneClient]],
    store: Store,
    kick: Callable[[], None],
    notifier: Notifier | None = None,
) -> APIRouter:
    """Build a FastAPI router with one /<slug>/webhook route per workspace.

    `workspaces` maps slug → (WorkspaceConfig, PlaneClient). The router closes
    over each entry so each endpoint uses its own secret. `kick` wakes the Dispatcher.
    """
    router = APIRouter()

    if not workspaces:
        log.warning("no_workspaces_loaded", conductor_dir=str(settings.conductor_dir))

    for slug, (workspace, _plane) in workspaces.items():
        _register_workspace_route(router, slug, workspace, store, kick, notifier)
    return router


_INSERT_TRIGGER = (
    "INSERT OR IGNORE INTO triggers(comment_id, member_uuid, workspace, plane_base_url,"
    " project_id, issue_id, kind, state, snapshot, received_at)"
    " VALUES(?,?,?,?,?,?,?, 'received', ?, ?)"
)


def _register_workspace_route(
    router: APIRouter,
    slug: str,
    workspace: WorkspaceConfig,
    store: Store,
    kick: Callable[[], None],
    notifier: Notifier | None = None,
) -> None:
    sig_header = workspace.webhook_signature_header
    initiator_uuid = workspace.initiator_uuid
    secret = workspace.webhook_secret.get_secret_value()

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
        comment_id = data.get("id")
        if not comment_id:
            return {"ok": True, "workspace": slug, "ignored": "no comment id"}

        mention_uuids = extract_mention_uuids(comment_html)
        _publish_gate(notifier, workspace, data, comment_html, mention_uuids, issue_uuid)

        actor_raw = data.get("actor")
        actor = str(actor_raw).lower() if actor_raw is not None else ""
        snapshot = json.dumps(
            {"actor": actor, "had_mention": bool(mention_uuids), "action": action}
        )
        addressees: list[tuple[str | None, str]] = []  # (member uuid | None, kind)
        auto_resume = False
        if not mention_uuids:
            if actor and actor == str(initiator_uuid).lower():
                # Initiator replied without an explicit @-mention: the Dispatcher finds the
                # agent that is waiting on them (needs a Plane read — never on this path).
                addressees.append((None, "auto_resume"))
                auto_resume = True
        else:
            addressees.extend((str(m), "webhook") for m in mention_uuids if m != initiator_uuid)

        accepted: list[str] = []
        skipped: list[dict[str, str]] = []
        now = time.time()
        try:
            for member, kind in addressees:
                cur = store.execute(
                    _INSERT_TRIGGER,
                    (
                        str(comment_id),
                        member,
                        slug,
                        workspace.plane_base_url,
                        str(workspace.project_id),
                        str(issue_uuid),
                        kind,
                        snapshot,
                        now,
                    ),
                )
                if cur.rowcount:
                    accepted.append(member or "auto_resume")
                else:
                    log.info(
                        "webhook_duplicate_skipped",
                        workspace=slug,
                        comment=str(comment_id),
                        member=member,
                    )
                    skipped.append({"member_id": member or "auto_resume", "reason": "duplicate"})
        except (sqlite3.Error, OSError) as exc:
            log.error("state_write_failed", workspace=slug, error=type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="state write failed"
            ) from exc

        if accepted:
            kick()
        result: dict[str, Any] = {
            "ok": True,
            "workspace": slug,
            "spawned": [],
            "accepted": accepted,
            "skipped": skipped,
            "ts": datetime.now(UTC).isoformat(),
        }
        if auto_resume and accepted:
            result["auto_resumed"] = "pending"
        return result
