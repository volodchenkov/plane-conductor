"""Dispatcher — the single task that resolves, claims, merges, defers, queues and starts.

QSALE-55 SPEC §3.3 / §4.1. The webhook only *accepts* a trigger (durable row, state
``received``). Everything that needs Plane or a decision happens here, one task, state machine
in SQL (shape follows the ``Notifier`` sender loop in ``workbench_client.py``):

``received`` → (member → nickname, root, source run) → ``new`` →
``merged`` | ``deferred`` | ``queued`` | ``running`` → ``done``; also ``expired`` and ``void``.

Decision per trigger, in **one transaction** (the ``slots`` PK is the atomic claim, FR-3):

0. a run of the same ``(workspace, nickname, issue)`` is active under another root → defer;
1. the trigger's *source run* is already covered by the slot (or by an earlier trigger of the
   same effect key) → ``merged`` — the COINEX-184 double start;
2. a slot exists and the trigger comes from anyone else (another run, a human, the slot's own
   run = self-handoff D→D, unknown source) → ``deferred``: exactly one rerun after the run;
3. no slot but the host is at ``max_concurrent_sessions`` → ``queued`` (durable);
4. else claim the slot, insert the run, start the process after the commit.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from typing import Any
from uuid import UUID

from plane_conductor import alerts
from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.config import Settings
from plane_conductor.exceptions import PlaneAPIError, RootUnresolvedError
from plane_conductor.logging_config import get_logger
from plane_conductor.members import email_of, is_nickname_allowed, resolve_pending_agent_member
from plane_conductor.plane_client import PlaneClient
from plane_conductor.roots import RootCache, is_gone
from plane_conductor.runner import Runner
from plane_conductor.store import Store, loads

log = get_logger(__name__)

MEMBERS_CACHE_TTL = 30.0


class Dispatcher:
    """See module docstring. ``tick()`` is the unit tests drive; ``run()`` is the loop."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        workspaces: dict[str, tuple[WorkspaceConfig, PlaneClient]],
        runner: Runner,
        roots: RootCache,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.store = store
        self.workspaces = workspaces
        self.runner = runner
        self.roots = roots
        self._clock = clock
        self._wake = asyncio.Event()
        self._stop = False
        self._task: asyncio.Task[None] | None = None
        self._members: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    # -- lifecycle ---------------------------------------------------------------

    def kick(self) -> None:
        """Wake the loop (called by the webhook after accept and by the runner on release)."""
        self._wake.set()

    def start(self) -> None:
        if self._task is None:
            self._stop = False
            self._task = asyncio.create_task(self.run(), name="dispatcher")

    async def stop(self) -> None:
        self._stop = True
        self._wake.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def run(self) -> None:
        while not self._stop:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("dispatcher_tick_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self.settings.dispatcher_tick_seconds
                )
            self._wake.clear()

    async def tick(self) -> None:
        await self._resolve_received()
        self._expire()
        await self._dispatch()

    # -- 1. received → new -------------------------------------------------------

    async def _resolve_received(self) -> None:
        rows = self.store.all("SELECT * FROM triggers WHERE state='received' ORDER BY id")
        for row in rows:
            entry = self.workspaces.get(str(row["workspace"]))
            if entry is None:
                self._void(row["id"], "workspace_unknown")
                log.warning("workspace_unknown", workspace=row["workspace"], trigger=row["id"])
                continue
            try:
                await self._resolve_one(row, *entry)
            except PlaneAPIError as exc:
                if is_gone(exc):
                    self._void(row["id"], f"plane_{exc.status_code}")
                else:  # transient (5xx, transport, exhausted 429): the row stays, next tick
                    log.warning(
                        "trigger_resolve_transient",
                        trigger=row["id"],
                        status=exc.status_code,
                    )
            except RootUnresolvedError as exc:
                log.warning("root_unresolved", trigger=row["id"], reason=str(exc))
            except sqlite3.IntegrityError:
                self._void(row["id"], "duplicate")

    async def _resolve_one(self, row: sqlite3.Row, ws: WorkspaceConfig, plane: PlaneClient) -> None:
        tid = row["id"]
        snapshot = loads(row["snapshot"], {})
        member_uuid = row["member_uuid"]
        nickname = row["nickname"]
        email = row["triggered_by_email"]
        agents = ws.agents_by_nickname()

        if member_uuid is None and nickname is None and row["kind"] == "auto_resume":
            resumed = await resolve_pending_agent_member(
                plane=plane,
                workspace=ws,
                initiator_uuid=ws.initiator_uuid,
                issue_uuid=UUID(str(row["issue_id"])),
                agents_by_nick=agents,
            )
            if resumed is None:
                self._void(tid, "no_pending_agent")
                return
            member_uuid = str(resumed)
            log.info("auto_resume_triggered", workspace=ws.workspace_slug, issue=row["issue_id"])

        if nickname is None:
            member = await plane.get_member(UUID(str(member_uuid)))
            email = email_of(member)
            if not email:
                self._void(tid, "no_email")
                return
            nickname = email.split("@", 1)[0].lower()
        if not is_nickname_allowed(str(nickname), ws, agents):
            log.warning("nickname_not_allowed", workspace=ws.workspace_slug, nickname=nickname)
            self._void(tid, "not_allowed")
            return

        root = await self.roots.root_of(ws.workspace_slug, str(ws.project_id), str(row["issue_id"]))

        source = row["source_run_id"]
        if source is None and row["kind"] == "webhook":
            source, wait = self._find_source(row, snapshot, ws)
            if wait:
                return  # an agent run is active and its tool result may not be registered yet

        now = self._clock()
        self.store.execute(
            "UPDATE triggers SET state='new', member_uuid=?, nickname=?, triggered_by_email=?,"
            " root_id=?, source_run_id=?, enqueued_at=COALESCE(enqueued_at, ?) WHERE id=?",
            (member_uuid, nickname, email, root, source, row["received_at"] or now, tid),
        )
        if source:
            log.info("trigger_source_resolved", trigger=tid, source_run=source)

    def _find_source(
        self, row: sqlite3.Row, snapshot: dict[str, Any], ws: WorkspaceConfig
    ) -> tuple[str | None, bool]:
        """``(source_run_id, must_wait)`` for a webhook trigger.

        The source is the run whose tower call created the comment (stream ``comment_id``).
        The tool result normally lands first; if an agent run is active and the comment is not
        registered yet, wait up to ``source_wait_seconds`` then continue with ``None`` (deferred,
        never silently merged). A human comment has no source and never waits.
        """
        h = self.store.one(
            "SELECT run_id FROM handoffs WHERE comment_id=? AND state='posted' ORDER BY id LIMIT 1",
            (row["comment_id"],),
        )
        if h is not None:
            return str(h["run_id"]), False
        actor = str(snapshot.get("actor") or "").lower()
        if actor and actor == str(ws.initiator_uuid).lower():
            return None, False
        active = self.store.one("SELECT 1 FROM runs WHERE state IN ('starting','running') LIMIT 1")
        if active is None:
            return None, False
        waited = self._clock() - float(row["received_at"])
        if waited < self.settings.source_wait_seconds:
            return None, True
        return None, False

    def _void(self, tid: int, reason: str) -> None:
        self.store.execute(
            "UPDATE triggers SET state='void', reason=?, finished_at=? WHERE id=?",
            (reason, self._clock(), tid),
        )

    # -- 2. expiry -----------------------------------------------------------------

    def _expire(self) -> None:
        """Waiting triggers older than ``queue_max_age_seconds``: one alert, no start."""
        cutoff = self._clock() - self.settings.queue_max_age_seconds
        rows = self.store.all(
            "SELECT * FROM triggers WHERE state IN ('received','new','deferred','queued')"
            " AND COALESCE(enqueued_at, received_at) < ?",
            (cutoff,),
        )
        for row in rows:
            now = self._clock()
            with self.store.tx() as c:
                cur = c.execute(
                    "UPDATE triggers SET state='expired', reason='queue_timeout', finished_at=?"
                    " WHERE id=? AND state IN ('received','new','deferred','queued')",
                    (now, row["id"]),
                )
                if not cur.rowcount:
                    continue
                alerts.enqueue(
                    c,
                    reason="queue_timeout",
                    subject=f"trigger-{row['id']}",
                    workspace=row["workspace"],
                    plane_base_url=row["plane_base_url"],
                    project_id=row["project_id"],
                    issue_id=row["issue_id"],
                    facts={
                        "nickname": row["nickname"],
                        "issue": row["issue_id"],
                        "waited_seconds": int(now - float(row["received_at"])),
                        "was_state": row["state"],
                    },
                    now=now,
                )
            log.warning("trigger_expired", trigger=row["id"], was=row["state"])

    # -- 3. place new / deferred / queued ----------------------------------------------

    async def _dispatch(self) -> None:
        rows = self.store.all(
            "SELECT * FROM triggers WHERE state IN ('new','deferred','queued')"
            " ORDER BY COALESCE(enqueued_at, received_at), id"
        )
        for row in rows:
            entry = self.workspaces.get(str(row["workspace"]))
            if entry is None:
                self._void(row["id"], "workspace_unknown")
                continue
            ws, plane = entry
            if self._verdict(row)[0] == "start":
                if not await self._revalidate(row, ws, plane):
                    continue
                if not await self._root_still_current(row, ws):
                    continue
            run_id = self._apply(row)
            if run_id:
                self.runner.launch(run_id)

    def _verdict(self, row: sqlite3.Row) -> tuple[str, str]:
        """Pure read: what would happen to this trigger now. ``(verdict, reason)``."""
        if self.runner.stopping:
            return "keep", "stopping"
        ws, nick, root = row["workspace"], row["nickname"], row["root_id"]
        source = row["source_run_id"]
        if source:
            twin = self.store.one(
                "SELECT id FROM triggers WHERE source_run_id=? AND nickname=? AND root_id=?"
                " AND id<? AND state IN ('deferred','queued','running','done') LIMIT 1",
                (source, nick, root, row["id"]),
            )
            if twin is not None:
                return "merged", "same_source_effect"
        slot = self.store.one(
            "SELECT covered_sources FROM slots WHERE workspace=? AND nickname=? AND root_id=?",
            (ws, nick, root),
        )
        if slot is not None:
            if source and source in loads(slot["covered_sources"], []):
                return "merged", "same_source"
            return "deferred", "slot_busy"
        other = self.store.one(
            "SELECT 1 FROM slots WHERE workspace=? AND nickname=? AND issue_id=? AND root_id!=?",
            (ws, nick, row["issue_id"], root),
        )
        if other is not None:
            return "deferred", "same_issue_other_root"
        count = self.store.scalar("SELECT count(*) FROM slots")
        if count >= self.settings.max_concurrent_sessions:
            return "queued", "capacity"
        return "start", ""

    def _apply(self, row: sqlite3.Row) -> str | None:
        """Authoritative decision + writes in one transaction. Returns a run id to launch."""
        now = self._clock()
        with self.store.tx() as c:
            fresh = c.execute("SELECT * FROM triggers WHERE id=?", (row["id"],)).fetchone()
            if fresh is None or fresh["state"] not in ("new", "deferred", "queued"):
                return None
            verdict, reason = self._verdict(fresh)
            if verdict == "keep":
                return None
            if verdict == "merged":
                c.execute(
                    "UPDATE triggers SET state='merged', reason=?, finished_at=? WHERE id=?",
                    (reason, now, fresh["id"]),
                )
                log.info(
                    "trigger_merged_same_source",
                    workspace=fresh["workspace"],
                    nickname=fresh["nickname"],
                    issue=fresh["issue_id"],
                    source_run=fresh["source_run_id"],
                    reason=reason,
                )
                return None
            if verdict in ("deferred", "queued"):
                if fresh["state"] != verdict:
                    c.execute(
                        "UPDATE triggers SET state=?, reason=?, enqueued_at=COALESCE(enqueued_at,?)"
                        " WHERE id=?",
                        (verdict, reason, now, fresh["id"]),
                    )
                    log.info(
                        "spawn_deferred" if verdict == "deferred" else "spawn_queued",
                        workspace=fresh["workspace"],
                        nickname=fresh["nickname"],
                        issue=fresh["issue_id"],
                        reason=reason,
                    )
                return None
            # start: the slots PK is the atomic claim
            run_id = uuid.uuid4().hex
            group = uuid.uuid4().hex
            covered = [fresh["source_run_id"]] if fresh["source_run_id"] else []
            try:
                c.execute(
                    "INSERT INTO slots(workspace, nickname, root_id, group_id, run_id, issue_id,"
                    " covered_sources) VALUES(?,?,?,?,?,?,?)",
                    (
                        fresh["workspace"],
                        fresh["nickname"],
                        fresh["root_id"],
                        group,
                        run_id,
                        fresh["issue_id"],
                        json.dumps(covered),
                    ),
                )
            except sqlite3.IntegrityError:
                c.execute(
                    "UPDATE triggers SET state='deferred', reason='slot_race' WHERE id=?",
                    (fresh["id"],),
                )
                return None
            c.execute(
                "INSERT INTO runs(run_id, group_id, attempt, workspace, plane_base_url, project_id,"
                " nickname, root_id, issue_id, triggered_by_email, state, started_at)"
                " VALUES(?,?,0,?,?,?,?,?,?,?, 'starting', ?)",
                (
                    run_id,
                    group,
                    fresh["workspace"],
                    fresh["plane_base_url"],
                    fresh["project_id"],
                    fresh["nickname"],
                    fresh["root_id"],
                    fresh["issue_id"],
                    fresh["triggered_by_email"],
                    now,
                ),
            )
            c.execute(
                "UPDATE triggers SET state='running', run_id=?, reason=NULL WHERE id=?",
                (run_id, fresh["id"]),
            )
            return run_id

    async def _root_still_current(self, row: sqlite3.Row, ws: WorkspaceConfig) -> bool:
        """Refresh the root right before a start; a re-parented issue gets its new key.

        The slot key is ``(workspace, nickname, root)``. A trigger that waited may belong to a
        different tree now; claiming the slot under the old root would let the role run twice on
        one tree. On a change the row is updated and re-evaluated on the next tick.
        """
        try:
            root = await self.roots.root_of(
                ws.workspace_slug, str(ws.project_id), str(row["issue_id"]), fresh=True
            )
        except PlaneAPIError as exc:
            if is_gone(exc):
                self._void(row["id"], f"plane_{exc.status_code}")
            else:
                log.warning("root_refresh_transient", trigger=row["id"], status=exc.status_code)
            return False
        except RootUnresolvedError as exc:
            log.warning("root_unresolved", trigger=row["id"], reason=str(exc))
            return False
        if root == row["root_id"]:
            return True
        log.info("trigger_root_changed", trigger=row["id"], old=row["root_id"], new=root)
        self.store.execute("UPDATE triggers SET root_id=? WHERE id=?", (root, row["id"]))
        self.kick()
        return False

    async def prepare_rerun(self, run: sqlite3.Row) -> None:
        """Re-validate the deferred triggers of a finishing run's key before they take its slot.

        A rerun is a start like any other (owner / access may have changed while the trigger
        waited). Invalid ones become ``void``; ones that could not be checked (transient Plane
        error) go back to ``new`` so the normal path validates and starts them.
        """
        rows = self.store.all(
            "SELECT * FROM triggers WHERE workspace=? AND nickname=? AND root_id=?"
            " AND state='deferred' ORDER BY id",
            (run["workspace"], run["nickname"], run["root_id"]),
        )
        for row in rows:
            entry = self.workspaces.get(str(row["workspace"]))
            if entry is None:
                self._void(row["id"], "workspace_unknown")
                continue
            if await self._revalidate(row, *entry) and await self._root_still_current(
                row, entry[0]
            ):
                continue
            # void rows stay void; anything unchecked or re-keyed goes back through the normal path
            self.store.execute(
                "UPDATE triggers SET state='new' WHERE id=? AND state='deferred'", (row["id"],)
            )

    # -- start-time re-validation -----------------------------------------------------

    async def _revalidate(self, row: sqlite3.Row, ws: WorkspaceConfig, plane: PlaneClient) -> bool:
        """Owner / access may have changed while the trigger waited (FR-4 failure scenarios)."""
        tid = row["id"]
        if row["plane_base_url"] != ws.plane_base_url or row["project_id"] != str(ws.project_id):
            self._void(tid, "destination_changed")
            log.warning("trigger_destination_changed", trigger=tid)
            return False
        nick = str(row["nickname"])
        if not is_nickname_allowed(nick, ws, ws.agents_by_nickname()):
            self._void(tid, "not_allowed")
            return False
        try:
            members = await self._project_members(ws, plane)
        except PlaneAPIError as exc:
            if is_gone(exc):
                self._void(tid, f"plane_{exc.status_code}")
            else:
                log.warning("revalidate_transient", trigger=tid, status=exc.status_code)
            return False
        nicks = {(email_of(m) or "").split("@", 1)[0].lower() for m in members}
        if nick not in nicks:
            self._void(tid, "member_removed")
            log.warning("trigger_member_removed", trigger=tid, nickname=nick)
            return False
        return True

    async def _project_members(
        self, ws: WorkspaceConfig, plane: PlaneClient
    ) -> list[dict[str, Any]]:
        hit = self._members.get(ws.workspace_slug)
        if hit is not None and self._clock() - hit[0] < MEMBERS_CACHE_TTL:
            return hit[1]
        members = await plane.list_project_members(ws.project_id)
        self._members[ws.workspace_slug] = (self._clock(), members)
        return members
