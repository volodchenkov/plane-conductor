"""Reader of the claude ``stream-json`` output (QSALE-55 SPEC §3.1, ADR-1).

Conductor is the parent of every agent and starts ``claude --output-format stream-json
--verbose``; this module turns that NDJSON into durable facts, with **no channel between
tower and conductor**:

- ``system/init`` ``mcp_servers`` → is ``plane-tower`` up (FR-2 watchdog);
- ``assistant`` ``tool_use`` + ``user`` ``tool_result`` of tower tools → the ``handoffs``
  table (FR-4), the run's "message to initiator" (FR-5) and the ``comment_id`` that later
  identifies the *source run* of a webhook trigger (FR-3).

External contract: Claude Code CLI 2.1.317, captured in
``tests/fixtures/claude_stream_2.1.317.ndjson``. Unknown shapes are ignored, never fatal;
an unknown ``init`` status falls back to "wait for the first tower call".

Registration is idempotent by ``tool_use_id`` so the journal can be replayed after a crash.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.logging_config import get_logger
from plane_conductor.store import Store, loads

log = get_logger(__name__)

TOWER_SERVER = "plane-tower"
TOWER_PREFIX = f"mcp__{TOWER_SERVER}__"

#: tower tool → (issue argument, target-role argument). Every one of them writes a comment
#: that mentions the initiator (``request_handoff`` only when ``target_role='initiator'``).
HANDOFF_TOOLS: dict[str, tuple[str, str]] = {
    "request_handoff": ("sub_uuid", "target_role"),
    "escalate_upstream_gap": ("my_sub_uuid", "upstream_role"),
    "post_review": ("sub_uuid", "next_role"),
    "mark_spec_approved": ("spec_sub_uuid", "next_role"),
    "post_changes": ("sub_uuid", "next_role"),
    "post_bug_report": ("test_sub_uuid", "next_role"),
}

BAD_TOWER_STATUSES = frozenset({"failed", "needs-auth", "disconnected"})

#: Events the runner reacts to (returned by :meth:`StreamRegistrar.feed`).
EV_TOWER_UP = "tower_up"
EV_TOWER_BAD = "tower_bad"
EV_TOWER_CALL = "tower_call"


def role_to_nickname(ws: WorkspaceConfig, role: str) -> str | None:
    """Pipeline role → nickname (same match tower uses: ``prompt_role`` minus namespace)."""
    target = role.strip().rsplit(":", 1)[-1].lower()
    for agent in ws.agents:
        if agent.prompt_role.rsplit(":", 1)[-1].lower() == target:
            return agent.nickname
    return None


def _result_text(content: Any) -> str:
    """Text of a ``tool_result`` ``content`` (a string or a list of text blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(b.get("text", ""))
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


class StreamRegistrar:
    """Feeds one run's stream lines into the store. One instance per run attempt.

    ``dirty`` is set when a store write failed: the reader keeps draining, the runner
    replays the journal before the outcome is computed (SPEC §4.2 "registration failure").
    """

    def __init__(
        self,
        store: Store,
        run_id: str,
        workspaces: dict[str, WorkspaceConfig],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.run_id = run_id
        self.workspaces = workspaces
        self._clock = clock
        self.tower_seen = False
        self.tower_status: str | None = None
        self.dirty = False
        self.result: dict[str, Any] | None = None
        #: ``type[/subtype]`` of the last parsed line (for the human-readable run log).
        self.last_type = ""
        self._calls: dict[str, tuple[str, dict[str, Any]]] = {}
        row = store.one(
            "SELECT workspace, plane_base_url, project_id FROM runs WHERE run_id=?", (run_id,)
        )
        self._dest = (
            (row["workspace"], row["plane_base_url"], row["project_id"])
            if row is not None
            else None
        )

    # -- public ----------------------------------------------------------------

    def feed(self, raw: bytes | str) -> list[tuple[str, str]]:
        """Parse one NDJSON line, register what it says, return runner events."""
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        text = text.strip()
        self.last_type = ""
        if not text:
            return []
        try:
            event = json.loads(text)
        except ValueError:
            log.warning("stream_line_unparseable", run_id=self.run_id)
            return []
        if not isinstance(event, dict):
            return []
        out: list[tuple[str, str]] = []
        try:
            etype = event.get("type")
            sub = event.get("subtype")
            self.last_type = f"{etype}/{sub}" if sub else str(etype)
            if etype == "system" and event.get("subtype") == "init":
                out.extend(self._on_init(event))
            elif etype == "assistant":
                out.extend(self._on_blocks(event, "tool_use"))
            elif etype == "user":
                out.extend(self._on_blocks(event, "tool_result"))
            elif etype == "result":
                self.result = event
        except (sqlite3.Error, OSError) as exc:
            self.dirty = True
            log.error("stream_registration_failed", run_id=self.run_id, error=type(exc).__name__)
        return out

    # -- events ----------------------------------------------------------------

    def _on_init(self, event: dict[str, Any]) -> list[tuple[str, str]]:
        servers = event.get("mcp_servers")
        if not isinstance(servers, list):
            return []  # older CLI: fall back to the first tower call
        for srv in servers:
            if isinstance(srv, dict) and srv.get("name") == TOWER_SERVER:
                status = str(srv.get("status", ""))
                self.tower_status = status
                if status == "connected":
                    self._mark_tower_seen()
                    return [(EV_TOWER_UP, status)]
                if status in BAD_TOWER_STATUSES:
                    return [(EV_TOWER_BAD, status)]
                return []  # unknown status (e.g. pending): wait for the first tower call
        # tower missing from the list entirely: treat as not connected
        self.tower_status = "missing"
        return [(EV_TOWER_BAD, "missing")]

    def _on_blocks(self, event: dict[str, Any], kind: str) -> list[tuple[str, str]]:
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return []
        out: list[tuple[str, str]] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != kind:
                continue
            if kind == "tool_use":
                out.extend(self._on_tool_use(block))
            else:
                self._on_tool_result(block)
        return out

    def _mark_tower_seen(self) -> None:
        self.tower_seen = True
        self.store.execute("UPDATE runs SET tower_seen=1 WHERE run_id=?", (self.run_id,))

    def _on_tool_use(self, block: dict[str, Any]) -> list[tuple[str, str]]:
        name = str(block.get("name", ""))
        if not name.startswith(TOWER_PREFIX):
            return []
        tool = name[len(TOWER_PREFIX) :]
        tool_use_id = str(block.get("id", ""))
        raw_args = block.get("input")
        args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
        self._calls[tool_use_id] = (tool, args)
        self._mark_tower_seen()
        events = [(EV_TOWER_CALL, tool)]
        if tool in HANDOFF_TOOLS and tool_use_id:
            self._insert_call(tool, tool_use_id, args)
        return events

    def _insert_call(self, tool: str, tool_use_id: str, args: dict[str, Any]) -> None:
        if self._dest is None:
            return
        slug, base_url, project_id = self._dest
        override = args.get("workspace")
        if isinstance(override, str) and override:
            ws = self.workspaces.get(override)
            if ws is None:
                log.warning("handoff_workspace_unknown", run_id=self.run_id, workspace=override)
                return
            slug, base_url, project_id = ws.workspace_slug, ws.plane_base_url, str(ws.project_id)
        ws = self.workspaces.get(slug)
        issue_arg, role_arg = HANDOFF_TOOLS[tool]
        issue_id = args.get(issue_arg)
        role = args.get(role_arg)
        target: str | None = None
        if isinstance(role, str) and role.strip() and role.strip().lower() != "initiator":
            target = role_to_nickname(ws, role) if ws is not None else None
            if target is None:
                log.warning("handoff_role_unknown", run_id=self.run_id, tool=tool, role=role)
                return  # tower refused the call too; nothing to register
        self.store.execute(
            "INSERT OR IGNORE INTO handoffs(run_id, tool, tool_use_id, workspace, plane_base_url,"
            " project_id, issue_id, target_nickname, state, created_at)"
            " VALUES(?,?,?,?,?,?,?,?, 'called', ?)",
            (
                self.run_id,
                tool,
                tool_use_id,
                slug,
                base_url,
                project_id,
                str(issue_id) if issue_id else None,
                target,
                self._clock(),
            ),
        )

    def _on_tool_result(self, block: dict[str, Any]) -> None:
        tool_use_id = str(block.get("tool_use_id", ""))
        call = self._calls.get(tool_use_id)
        if call is None or call[0] not in HANDOFF_TOOLS:
            return
        text = _result_text(block.get("content"))
        if block.get("is_error"):
            with self.store.tx() as c:
                cur = c.execute(
                    "UPDATE handoffs SET state='failed' WHERE tool_use_id=? AND state='called'",
                    (tool_use_id,),
                )
                if cur.rowcount and "outcome_unknown" in text:
                    c.execute("UPDATE runs SET in_doubt=in_doubt+1 WHERE run_id=?", (self.run_id,))
            return
        data = loads(text, None)
        comment_id = data.get("comment_id") if isinstance(data, dict) else None
        if not comment_id:
            # A "successful" result without a comment id cannot be tracked: in doubt.
            return
        with self.store.tx() as c:
            cur = c.execute(
                "UPDATE handoffs SET state='posted', comment_id=?, created_at=?"
                " WHERE tool_use_id=? AND state='called'",
                (str(comment_id), self._clock(), tool_use_id),
            )
            if cur.rowcount:
                backfill_provenance(c, str(comment_id), self.run_id)
                log.info(
                    "handoff_registered",
                    run_id=self.run_id,
                    tool=call[0],
                    comment_id=str(comment_id),
                )


def backfill_provenance(conn: sqlite3.Connection, comment_id: str, run_id: str) -> None:
    """A tool result registered *after* its webhook arrived: give the trigger its source run.

    Unstarted triggers get ``source_run_id`` (the Dispatcher applies merge/defer rules on its
    next tick); an already started one adds the source to its slot's ``covered_sources``.
    """
    conn.execute(
        "UPDATE triggers SET source_run_id=? WHERE comment_id=? AND source_run_id IS NULL"
        " AND state IN ('received','new','deferred','queued')",
        (run_id, comment_id),
    )
    for row in conn.execute(
        "SELECT id, workspace, nickname, root_id FROM triggers"
        " WHERE comment_id=? AND source_run_id IS NULL AND state='running'",
        (comment_id,),
    ).fetchall():
        conn.execute("UPDATE triggers SET source_run_id=? WHERE id=?", (run_id, row["id"]))
        slot = conn.execute(
            "SELECT covered_sources FROM slots WHERE workspace=? AND nickname=? AND root_id=?",
            (row["workspace"], row["nickname"], row["root_id"]),
        ).fetchone()
        if slot is not None:
            covered = loads(slot["covered_sources"], [])
            if run_id not in covered:
                covered.append(run_id)
                conn.execute(
                    "UPDATE slots SET covered_sources=? WHERE workspace=? AND nickname=?"
                    " AND root_id=?",
                    (json.dumps(covered), row["workspace"], row["nickname"], row["root_id"]),
                )


def replay_journal(
    store: Store,
    run_id: str,
    workspaces: dict[str, WorkspaceConfig],
    journal: Path,
) -> StreamRegistrar:
    """Re-register everything the journal of ``run_id`` says (idempotent). Used after a crash
    and for runs whose live registration hit a store error."""
    reg = StreamRegistrar(store, run_id, workspaces)
    try:
        with journal.open("rb") as fh:
            for line in fh:
                reg.feed(line)
    except FileNotFoundError:
        pass
    return reg
