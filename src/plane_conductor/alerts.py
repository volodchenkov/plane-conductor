"""Alert comments to the initiator (QSALE-55 FR-2/4/5, NFR-5).

An alert is a row in ``alerts`` (an outbox) that the ``Watcher`` publishes to Plane
with retries. The row's ``dedupe_key`` is the guarantee "one cause, one comment":
``sha1(reason || group_id-or-trigger-id)``. Enqueue is ``INSERT OR IGNORE`` so it can
join the transaction that decides the outcome (all-or-nothing on a crash).
"""

from __future__ import annotations

import hashlib
import html
import json
import sqlite3
import time
from typing import Any
from uuid import UUID

MARKER_PREFIX = "conductor-alert:"

#: Human titles per reason (English, like every other conductor comment; A-2).
TITLES = {
    "start_failed": "could not start",
    "abnormal_exit": "ended abnormally after leaving a handoff",
    "no_handoff": "finished without a handoff or a message to the initiator",
    "handoff_undelivered": "was handed a task but was not started",
    "queue_timeout": "was queued too long and the trigger expired",
    "conductor_restart": "was running when the orchestrator restarted",
}

_MENTION = (
    '<mention-component entity_identifier="{uuid}" entity_name="user_mention"></mention-component>'
)


def dedupe_key(reason: str, subject: str) -> str:
    """Stable key: one reason about one subject (run group / trigger / handoff) = one alert."""
    return hashlib.sha1(f"{reason}|{subject}".encode(), usedforsecurity=False).hexdigest()


def enqueue(
    conn: sqlite3.Connection,
    *,
    reason: str,
    subject: str,
    workspace: str,
    plane_base_url: str,
    project_id: str,
    issue_id: str,
    facts: dict[str, Any],
    now: float | None = None,
) -> str:
    """Insert an alert row (idempotent by ``dedupe_key``). Returns the key."""
    now = time.time() if now is None else now
    key = dedupe_key(reason, subject)
    conn.execute(
        "INSERT OR IGNORE INTO alerts(dedupe_key, workspace, plane_base_url, project_id,"
        " issue_id, reason, facts, state, attempts, next_try_at, created_at)"
        " VALUES(?,?,?,?,?,?,?, 'new', 0, ?, ?)",
        (
            key,
            workspace,
            plane_base_url,
            project_id,
            issue_id,
            reason,
            json.dumps(facts, ensure_ascii=False, default=str),
            now,
            now,
        ),
    )
    return key


def render(*, reason: str, facts: dict[str, Any], key: str, initiator_uuid: UUID | str) -> str:
    """HTML of the comment: initiator mention first, title, facts, then the dedupe marker.

    The mention is first so the webhook (which skips initiator-addressed comments) never
    turns an alert back into a trigger. Facts are ids, counts, nicknames, exit codes —
    never secrets (NFR-3); every value is HTML-escaped.
    """
    nick = facts.get("nickname")
    who = f"@{nick} " if nick else ""
    title = f"{who}{TITLES.get(reason, reason)}"
    items = "".join(
        f"<li><code>{html.escape(str(k))}</code>: {html.escape(str(v))}</li>"
        for k, v in facts.items()
        if k != "nickname" and v not in (None, "")
    )
    return (
        f"<p>{_MENTION.format(uuid=initiator_uuid)} <strong>{html.escape(title)}.</strong></p>"
        f"<ul>{items}</ul>"
        f"<p>{MARKER_PREFIX}{key}</p>"
    )
