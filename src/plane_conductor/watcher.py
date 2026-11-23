"""Handoff delivery watcher (FR-4) and alert outbox publisher (FR-2/4/5, NFR-5).

QSALE-55 SPEC §4.3 / §4.4. One background task, two jobs:

**Handoff watcher.** ``handoffs`` rows are written by the stream reader at the moment tower's
tool result is read — independent of the webhook. A *posted* handoff addressed to an agent is
*settled* once a ``triggers`` row covers it (a trigger with its ``comment_id`` and addressee, or
one with the same effect key ``(source run, addressee, root)``). Otherwise, after
``handoff_ack_seconds`` conductor redelivers it (inserts the trigger itself), up to
``handoff_max_retries``, then raises one ``handoff_undelivered`` alert. Handoffs to the
initiator (gates) are never watched. Waiting in the queue or being deferred is *not* an
alarm — the trigger row exists, so the handoff is settled; the Dispatcher's queue-age rule
owns that alert.

**Outbox publisher.** ``alerts`` rows are posted to Plane with retries. The POST carries
``external_source/external_id`` (Plane answers 409 for a repeat). A row is persisted
``sending`` before the POST; an ambiguous failure (transport / 5xx / deadline) makes it
``unknown`` and the next attempt first reconciles by searching the issue's comments for the
``conductor-alert:<key>`` marker. Shape follows ``workbench.outbox`` (``OutboxWorker``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

from plane_conductor import alerts
from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.config import Settings
from plane_conductor.exceptions import PlaneAPIError
from plane_conductor.logging_config import get_logger
from plane_conductor.plane_client import PlaneClient
from plane_conductor.roots import RootCache
from plane_conductor.store import Store

log = get_logger(__name__)

PURGE_EVERY = 3600.0
ALERT_BATCH = 20


def backoff(attempts: int) -> float:
    """30 s doubling to a 300 s cap."""
    return float(min(300.0, 30.0 * (2 ** max(0, attempts - 1))))


class Watcher:
    """See module docstring. ``tick()`` is what tests drive."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        workspaces: dict[str, tuple[WorkspaceConfig, PlaneClient]],
        roots: RootCache,
        *,
        clock: Callable[[], float] = time.time,
        on_trigger: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.workspaces = workspaces
        self.roots = roots
        self._clock = clock
        self._on_trigger = on_trigger or (lambda: None)
        self._wake = asyncio.Event()
        self._stop = False
        self._task: asyncio.Task[None] | None = None
        self._last_handoff_check = 0.0
        self._last_purge = clock()

    # -- lifecycle -----------------------------------------------------------------

    def kick(self) -> None:
        self._wake.set()

    def start(self) -> None:
        if self._task is None:
            self._stop = False
            self._task = asyncio.create_task(self.run(), name="watcher")

    async def stop(self) -> None:
        self._stop = True
        self._wake.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        # last chance for alerts enqueued by the runs that just finished
        with contextlib.suppress(Exception):
            await self.publish_alerts()

    async def run(self) -> None:
        while not self._stop:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("watcher_tick_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self.settings.dispatcher_tick_seconds
                )
            self._wake.clear()

    async def tick(self) -> None:
        now = self._clock()
        if now - self._last_handoff_check >= self.settings.watcher_tick_seconds:
            self._last_handoff_check = now
            await self.check_handoffs()
        await self.publish_alerts()
        if now - self._last_purge >= PURGE_EVERY:
            self._last_purge = now
            with contextlib.suppress(sqlite3.Error):
                self.store.purge(
                    now=now,
                    state_retention_s=self.settings.state_retention_days * 86400,
                    dedupe_retention_s=self.settings.dedupe_retention_days * 86400,
                )

    # -- FR-4: handoff delivery --------------------------------------------------------

    async def check_handoffs(self) -> None:
        rows = self.store.all(
            "SELECT * FROM handoffs WHERE state='posted' AND target_nickname IS NOT NULL"
            " AND alerted=0 ORDER BY id"
        )
        for h in rows:
            try:
                await self._check_one(h)
            except PlaneAPIError as exc:
                log.warning("handoff_check_transient", handoff=h["id"], status=exc.status_code)
            except Exception:
                log.exception("handoff_check_failed", handoff=h["id"])

    #: trigger states that mean "this handoff reached an agent run" for good
    _FINAL = frozenset({"running", "done", "merged", "expired"})
    #: trigger states that are still on their way: neither settled nor lost yet
    _PENDING = frozenset({"received", "new", "deferred", "queued"})

    def _coverage(self, states: list[str]) -> str:
        """``final`` | ``pending`` | ``void`` | ``none`` for the states of covering triggers."""
        found = set(states)
        if found & self._FINAL:
            return "final"
        if found & self._PENDING:
            return "pending"
        if "void" in found:
            return "void"
        return "none"

    def _comment_states(self, h: sqlite3.Row) -> list[str]:
        rows = self.store.all(
            "SELECT state FROM triggers WHERE comment_id=? AND (nickname=? OR nickname IS NULL)",
            (h["comment_id"], h["target_nickname"]),
        )
        return [str(r["state"]) for r in rows]

    def _effect_states(self, h: sqlite3.Row, root: str) -> list[str]:
        rows = self.store.all(
            "SELECT state FROM triggers WHERE source_run_id=? AND nickname=? AND root_id=?",
            (h["run_id"], h["target_nickname"], root),
        )
        return [str(r["state"]) for r in rows]

    def _apply_coverage(self, h: sqlite3.Row, coverage: str) -> bool:
        """Act on a coverage verdict. ``True`` = the handoff needs nothing more this tick.

        A trigger that is merely *pending* does not settle a handoff for good: it may still be
        voided later (access revoked), and then the handoff must be alerted, not forgotten.
        """
        if coverage == "final":
            self._set_alerted(h["id"], 2)
            return True
        if coverage == "pending":
            return True
        if coverage == "void":
            self._alert_undelivered(h, voided=True)
            return True
        return False

    async def _check_one(self, h: sqlite3.Row) -> None:
        if self._apply_coverage(h, self._coverage(self._comment_states(h))):
            return
        now = self._clock()
        tried = int(h["retries"])
        reference = float(h["last_try_at"] or h["created_at"])
        interval = (
            self.settings.handoff_retry_interval_seconds
            if tried
            else self.settings.handoff_ack_seconds
        )
        if now - reference < interval:
            return
        # effect key: the handoff may be covered by a trigger from the same run on another
        # issue of the same root (COINEX-184 shape)
        root = h["root_id"]
        if root is None and h["issue_id"]:
            try:
                root = await self.roots.root_of(h["workspace"], h["project_id"], str(h["issue_id"]))
            except PlaneAPIError as exc:
                if exc.status_code not in (403, 404):
                    raise
            except Exception:
                root = None
            if root:
                self.store.execute("UPDATE handoffs SET root_id=? WHERE id=?", (root, h["id"]))
        if root and self._apply_coverage(h, self._coverage(self._effect_states(h, root))):
            return
        if tried >= self.settings.handoff_max_retries or not h["issue_id"]:
            self._alert_undelivered(h, voided=False)
            return
        self._redeliver(h, root)

    def _redeliver(self, h: sqlite3.Row, root: str | None) -> None:
        """Create the trigger the webhook should have created (no HTTP round trip, A-8)."""
        now = self._clock()
        try:
            with self.store.tx() as c:
                c.execute(
                    "UPDATE handoffs SET retries=retries+1, last_try_at=? WHERE id=?",
                    (now, h["id"]),
                )
                c.execute(
                    "INSERT INTO triggers(comment_id, nickname, workspace, plane_base_url,"
                    " project_id, issue_id, root_id, source_run_id, kind, state, snapshot,"
                    " received_at) VALUES(?,?,?,?,?,?,?,?, 'redeliver', 'received', ?, ?)",
                    (
                        h["comment_id"],
                        h["target_nickname"],
                        h["workspace"],
                        h["plane_base_url"],
                        h["project_id"],
                        h["issue_id"],
                        None,  # the Dispatcher resolves the root like for any trigger
                        h["run_id"],
                        json.dumps({"actor": "", "had_mention": True, "action": "redeliver"}),
                        now,
                    ),
                )
        except sqlite3.IntegrityError:
            log.info("handoff_redeliver_conflict", handoff=h["id"], comment=h["comment_id"])
            return
        except sqlite3.Error as exc:
            log.error("handoff_redeliver_failed", handoff=h["id"], error=type(exc).__name__)
            return
        log.warning(
            "handoff_redelivered",
            handoff=h["id"],
            nickname=h["target_nickname"],
            comment=h["comment_id"],
            retry=int(h["retries"]) + 1,
        )
        self._on_trigger()

    def _alert_undelivered(self, h: sqlite3.Row, *, voided: bool) -> None:
        now = self._clock()
        with self.store.tx() as c:
            alerts.enqueue(
                c,
                reason="handoff_undelivered",
                subject=f"handoff-{h['id']}",
                workspace=h["workspace"],
                plane_base_url=h["plane_base_url"],
                project_id=h["project_id"],
                issue_id=h["issue_id"] or "",
                facts={
                    "nickname": h["target_nickname"],
                    "issue": h["issue_id"],
                    "comment": h["comment_id"],
                    "redeliveries": int(h["retries"]),
                    "waited_seconds": int(now - float(h["created_at"])),
                    "trigger_voided": voided or None,
                },
                now=now,
            )
            c.execute("UPDATE handoffs SET alerted=1 WHERE id=?", (h["id"],))
        log.error("handoff_undelivered", handoff=h["id"], nickname=h["target_nickname"])
        self.kick()

    def _set_alerted(self, handoff_id: int, value: int) -> None:
        self.store.execute("UPDATE handoffs SET alerted=? WHERE id=?", (value, handoff_id))

    # -- outbox publisher -------------------------------------------------------------

    async def publish_alerts(self) -> None:
        now = self._clock()
        rows = self.store.all(
            "SELECT * FROM alerts WHERE state IN ('new','unknown') AND next_try_at<=?"
            " ORDER BY next_try_at LIMIT ?",
            (now, ALERT_BATCH),
        )
        for a in rows:
            try:
                await self._publish_one(a)
            except Exception:
                log.exception("alert_publish_crashed", key=a["dedupe_key"])
                self._failed(a, "unknown")

    async def _publish_one(self, a: sqlite3.Row) -> None:
        key = a["dedupe_key"]
        entry = self.workspaces.get(a["workspace"])
        if entry is None:
            self._drop(a, "workspace_unknown")
            return
        ws, plane = entry
        if a["plane_base_url"] != ws.plane_base_url or a["project_id"] != str(ws.project_id):
            self._drop(a, "destination_changed")
            return
        project = ws.project_id
        try:
            issue = UUID(str(a["issue_id"]))
        except ValueError:
            self._drop(a, "bad_issue_id")
            return

        if a["state"] == "unknown":
            try:
                comments = await plane.list_issue_comments_all(project, issue)
            except PlaneAPIError as exc:
                if exc.status_code in (403, 404):
                    self._drop(a, f"plane_{exc.status_code}")
                else:
                    self._failed(a, "unknown", exc)
                return
            marker = f"{alerts.MARKER_PREFIX}{key}"
            for comment in comments:
                if marker in str(comment.get("comment_html") or ""):
                    self._sent(a, str(comment.get("id") or "") or None)
                    return

        self.store.execute("UPDATE alerts SET state='sending' WHERE dedupe_key=?", (key,))
        facts: dict[str, Any] = json.loads(a["facts"] or "{}")
        body = alerts.render(
            reason=a["reason"], facts=facts, key=key, initiator_uuid=ws.initiator_uuid
        )
        try:
            resp = await plane.create_issue_comment(
                project, issue, body, external_source="conductor", external_id=key
            )
        except PlaneAPIError as exc:
            if exc.status_code == 409:  # same external_id already stored: our earlier attempt won
                self._sent(a, None)
            elif exc.status_code in (403, 404):
                self._drop(a, f"plane_{exc.status_code}")
            elif exc.is_transient:
                self._failed(a, "unknown", exc)
            else:
                self._failed(a, "new", exc)
            return
        comment_id = resp.get("id") if isinstance(resp, dict) else None
        self._sent(a, str(comment_id) if comment_id else None)

    def _sent(self, a: sqlite3.Row, comment_id: str | None) -> None:
        self.store.execute(
            "UPDATE alerts SET state='sent', comment_id=? WHERE dedupe_key=?",
            (comment_id, a["dedupe_key"]),
        )
        log.info("alert_sent", key=a["dedupe_key"], reason=a["reason"], comment=comment_id)

    def _drop(self, a: sqlite3.Row, why: str) -> None:
        self.store.execute(
            "UPDATE alerts SET state='dropped' WHERE dedupe_key=?", (a["dedupe_key"],)
        )
        log.error("alert_publish_gave_up", key=a["dedupe_key"], reason=a["reason"], why=why)

    def _failed(self, a: sqlite3.Row, state: str, exc: PlaneAPIError | None = None) -> None:
        attempts = int(a["attempts"]) + 1
        status = exc.status_code if exc is not None else None
        log.error(
            "alert_publish_failed",
            key=a["dedupe_key"],
            reason=a["reason"],
            status=status,
            attempts=attempts,
        )
        if attempts >= self.settings.alert_max_attempts:
            self._drop(a, f"max_attempts status={status}")
            return
        self.store.execute(
            "UPDATE alerts SET state=?, attempts=?, next_try_at=? WHERE dedupe_key=?",
            (state, attempts, self._clock() + backoff(attempts), a["dedupe_key"]),
        )
