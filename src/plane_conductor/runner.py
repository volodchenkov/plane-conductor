"""Subprocess runner — one supervised *slot* per ``(workspace, nickname, root)``.

QSALE-55 rework. The Dispatcher (``dispatcher.py``) decides *whether* a run starts and
inserts the ``slots`` + ``runs`` rows in one transaction; this module owns *how* it runs:

- ``claude --output-format stream-json --verbose`` is read line by line (``stream.py``):
  tower connection (FR-2), handoffs and messages to the initiator (FR-4/5), journaled to
  ``<state_dir>/streams/<run_id>.ndjson`` *before* parsing so it can be replayed;
- **watchdog / start failure (FR-2)**: no tower in ``init`` (or process exit / hang without any
  tower call) kills the group and retries up to ``start_retries`` with backoff, each attempt a
  new ``runs`` row; exhausted → one ``start_failed`` alert;
- **outcome (FR-5)** in one transaction (run exit, alert, triggers done, slot release or
  deferred rerun) once the *whole process group* is gone and the stdout reader hit EOF;
- process-group control: own session per agent, SIGTERM→SIGKILL on timeout.

All state is in the SQLite store; nothing here is memory-only except task handles.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any
from uuid import UUID

from plane_conductor import alerts
from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.config import Settings
from plane_conductor.exceptions import PlaneAPIError
from plane_conductor.logging_config import get_logger
from plane_conductor.plane_client import PlaneClient
from plane_conductor.store import Store
from plane_conductor.stream import (
    EV_TOWER_BAD,
    EV_TOWER_CALL,
    EV_TOWER_UP,
    StreamRegistrar,
    replay_journal,
)

log = get_logger(__name__)

STREAM_LIMIT = 32 * 1024 * 1024
GROUP_TERM_AFTER = 10.0
GROUP_KILL_AFTER = 15.0


def build_prompt(
    *,
    nickname: str,
    issue_uuid: UUID,
    plane_base_url: str,
    workspace_slug: str,
    project_id: str | UUID,
    triggered_by_email: str | None = None,
    implementer: str = "claude",
) -> str:
    """Compose the trigger prompt fed to `claude --print` on stdin."""
    issue_url = f"{plane_base_url}/{workspace_slug}/projects/{project_id}/issues/{issue_uuid}/"
    lines = [
        f"Triggered as @{nickname}.",
        f"Issue UUID: {issue_uuid}",
    ]
    if triggered_by_email:
        lines.append(f"Triggered by: {triggered_by_email}")
    lines.append(f"Plane URL: {issue_url}")
    if implementer != "claude":
        lines.append(f"Implementer: {implementer}")
    lines.append("")
    lines.append("Pick up the issue and proceed per your role's protocol.")
    return "\n".join(lines)


def spawn_env(slug: str, run_id: str | None = None) -> dict[str, str]:
    """Environment of a spawned agent: ``WORKSPACE_SLUG`` plus workbench isolation.

    ``WORKBENCH_DRIVER=runner`` and no ``TMUX`` make ``workbench-hook`` a no-op for the
    claude processes this runner starts, even when the conductor itself was started inside
    tmux. ``CONDUCTOR_RUN_ID`` marks every descendant of the run: after a conductor crash the
    next start finds survivors through ``/proc/<pid>/environ`` (SPEC §4.5).
    """
    env = {**os.environ, "WORKSPACE_SLUG": slug, "WORKBENCH_DRIVER": "runner"}
    if run_id:
        env["CONDUCTOR_RUN_ID"] = run_id
    env.pop("TMUX", None)
    return env


def log_path_for(log_dir: Path, workspace_slug: str, nickname: str, issue_uuid: UUID | str) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return log_dir / f"{ts}-{workspace_slug}-{nickname}-{str(issue_uuid)[:8]}.log"


def journal_path(state_dir: Path, run_id: str) -> Path:
    return state_dir / "streams" / f"{run_id}.ndjson"


def _format_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


@dataclass
class AttemptResult:
    """What one process attempt did."""

    start_failure: str | None = None
    exit_code: int = -1
    timed_out: bool = False
    tower_seen: bool = False
    registration_dirty: bool = False
    log_name: str = ""


class Runner:
    """Supervises agents of slots the Dispatcher created. Capacity lives in ``slots``."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        workspaces: dict[str, tuple[WorkspaceConfig, PlaneClient]],
        *,
        on_change: Callable[[], None] | None = None,
        on_alert: Callable[[], None] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.workspaces = workspaces
        self.on_change: Callable[[], None] = on_change or (lambda: None)
        self.on_alert: Callable[[], None] = on_alert or (lambda: None)
        #: Awaited with the finishing run before its slot is released, so deferred reruns are
        #: re-validated like any other start (set by the Dispatcher).
        self.pre_release: Callable[[sqlite3.Row], Awaitable[None]] | None = None
        self._sleep = sleep or asyncio.sleep
        self._tasks: set[asyncio.Task[None]] = set()
        # pgid (== pid, own session) → Process, for shutdown.
        self._procs: dict[int, asyncio.subprocess.Process] = {}
        self._configs = {slug: ws for slug, (ws, _p) in workspaces.items()}
        self.stopping = False

    # -- public --------------------------------------------------------------

    @property
    def active_count(self) -> int:
        return self.store.scalar("SELECT count(*) FROM slots")

    def launch(self, run_id: str) -> None:
        """Supervise the slot whose first run is ``run_id`` (already committed)."""
        task = asyncio.create_task(self._supervise_slot(run_id), name=f"slot-{run_id[:8]}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- slot supervision ------------------------------------------------------

    async def _supervise_slot(self, run_id: str) -> None:
        current: str | None = run_id
        try:
            while current is not None:
                current = await self._run_group(current)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("slot_supervisor_crashed", run_id=run_id)
            self._emergency_release(current or run_id)
        finally:
            self.on_change()

    def _run_row(self, run_id: str) -> sqlite3.Row:
        row = self.store.one("SELECT * FROM runs WHERE run_id=?", (run_id,))
        assert row is not None, run_id
        return row

    async def _run_group(self, run_id: str) -> str | None:
        """Run attempts until one gets tower (or retries run out); then the outcome.

        Returns the run id of a deferred rerun that took over the slot, or ``None``.
        """
        run = self._run_row(run_id)
        started = time.monotonic()
        announce = self._start_announce(run)
        while True:
            res = await self._attempt(run)
            if (
                res.start_failure
                and int(run["attempt"]) < self.settings.start_retries
                and not self.stopping
            ):
                self._finish_failed_attempt(run, res)
                backoffs = self.settings.start_retry_backoff_seconds or [0.0]
                delay = backoffs[min(int(run["attempt"]), len(backoffs) - 1)]
                log.warning(
                    "start_retry",
                    workspace=run["workspace"],
                    nickname=run["nickname"],
                    issue=run["issue_id"],
                    attempt=int(run["attempt"]) + 1,
                    cause=res.start_failure,
                    backoff_s=delay,
                )
                await self._sleep(delay)
                if self.stopping:
                    # shutdown began during the backoff: do not start a process that the
                    # shutdown snapshot has not seen
                    res.start_failure = f"{res.start_failure} (retry skipped: shutting down)"
                    break
                run = self._new_attempt(run)
                continue
            break
        if self.pre_release is not None:
            try:
                await self.pre_release(run)
            except (
                Exception
            ):  # a validation bug must not leak the slot; the rerun then goes unchecked
                log.exception("pre_release_failed", run_id=run["run_id"])
        next_run = self._complete(run, res)
        duration = time.monotonic() - started
        log.info(
            "agent_exited",
            workspace=run["workspace"],
            nickname=run["nickname"],
            issue=run["issue_id"],
            exit_code=res.exit_code,
            timed_out=res.timed_out,
            start_failure=res.start_failure,
            duration_s=round(duration, 1),
        )
        await self._finish_announce(run, announce, res, duration)
        return next_run

    def _new_attempt(self, prev: sqlite3.Row) -> sqlite3.Row:
        new_id = uuid.uuid4().hex
        with self.store.tx() as c:
            c.execute(
                "INSERT INTO runs(run_id, group_id, attempt, workspace, plane_base_url, project_id,"
                " nickname, root_id, issue_id, triggered_by_email, state, started_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?, 'starting', ?)",
                (
                    new_id,
                    prev["group_id"],
                    int(prev["attempt"]) + 1,
                    prev["workspace"],
                    prev["plane_base_url"],
                    prev["project_id"],
                    prev["nickname"],
                    prev["root_id"],
                    prev["issue_id"],
                    prev["triggered_by_email"],
                    time.time(),
                ),
            )
            c.execute(
                "UPDATE slots SET run_id=? WHERE workspace=? AND nickname=? AND root_id=?",
                (new_id, prev["workspace"], prev["nickname"], prev["root_id"]),
            )
        return self._run_row(new_id)

    # -- one process attempt ---------------------------------------------------

    async def _attempt(self, run: sqlite3.Row) -> AttemptResult:
        res = AttemptResult()
        run_id = str(run["run_id"])
        ws = self._configs.get(str(run["workspace"]))
        if ws is None:
            res.start_failure = f"spawn_error: workspace {run['workspace']!r} not configured"
            return res
        log_fp: IO[bytes] | None = None
        journal_fp: IO[bytes] | None = None
        proc: asyncio.subprocess.Process | None = None
        try:
            log_path = log_path_for(
                self.settings.log_dir, str(run["workspace"]), str(run["nickname"]), run["issue_id"]
            )
            res.log_name = log_path.name
            jpath = journal_path(self.settings.state_dir, run_id)
            jpath.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            log_fp = log_path.open("ab", buffering=0)
            journal_fp = jpath.open("ab", buffering=0)
            agent_cfg = ws.agents_by_nickname().get(str(run["nickname"]))
            prompt = build_prompt(
                nickname=str(run["nickname"]),
                issue_uuid=UUID(str(run["issue_id"])),
                plane_base_url=str(run["plane_base_url"]),
                workspace_slug=str(run["workspace"]),
                project_id=str(run["project_id"]),
                triggered_by_email=run["triggered_by_email"],
                implementer=agent_cfg.implementer if agent_cfg else "claude",
            )
            agent_name = agent_cfg.prompt_role if agent_cfg else str(run["nickname"])
            argv = [
                self.settings.claude_binary,
                "--agent",
                agent_name,
                # `auto`, not `acceptEdits`: under --print nobody can answer a prompt.
                "--permission-mode",
                "auto",
                "--print",
                "--output-format",
                "stream-json",
                "--verbose",
            ]
            cwd = ws.agent_working_dir or Path.cwd()
            log_fp.write(
                f"# {datetime.now(UTC).isoformat()} spawn run={run_id} attempt={run['attempt']}"
                f" workspace={run['workspace']} argv={argv!r} cwd={cwd}\n".encode()
            )
            log_fp.write(f"# prompt:\n{prompt}\n# --- stderr / stream events ---\n".encode())
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=log_fp,
                cwd=str(cwd),
                env=spawn_env(str(run["workspace"]), run_id),
                start_new_session=True,
                limit=STREAM_LIMIT,
            )
        except Exception as exc:  # any failure between the claim commit and a running process
            res.start_failure = f"spawn_error: {type(exc).__name__}: {exc}"
            for fp in (log_fp, journal_fp):
                if fp is not None:
                    with contextlib.suppress(Exception):
                        fp.close()
            log.error(
                "spawn_failed",
                run_id=run_id,
                nickname=run["nickname"],
                error=f"{type(exc).__name__}: {exc}",
            )
            return res

        assert log_fp is not None and journal_fp is not None
        self._procs[proc.pid] = proc
        with contextlib.suppress(sqlite3.Error):
            self.store.execute(
                "UPDATE runs SET state='running', pid=?, pgid=? WHERE run_id=?",
                (proc.pid, proc.pid, run_id),
            )
        log.info(
            "agent_spawned",
            run_id=run_id,
            workspace=run["workspace"],
            nickname=run["nickname"],
            issue=run["issue_id"],
            attempt=run["attempt"],
            pid=proc.pid,
            log_path=str(log_path),
        )
        if proc.stdin is not None:
            try:
                proc.stdin.write(prompt.encode("utf-8"))
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with contextlib.suppress(Exception):
                    proc.stdin.close()

        reg = StreamRegistrar(self.store, run_id, self._configs)
        tower_up = asyncio.Event()
        tower_bad: list[str] = []
        bad_ev = asyncio.Event()

        def on_event(kind: str, detail: str) -> None:
            if kind in (EV_TOWER_UP, EV_TOWER_CALL):
                tower_up.set()
            elif kind == EV_TOWER_BAD and not tower_bad:
                tower_bad.append(f"tower {detail}")
                bad_ev.set()

        reader = asyncio.create_task(self._read(proc, reg, journal_fp, log_fp, on_event))
        watchdog = asyncio.create_task(self._watchdog(proc, tower_up, bad_ev, tower_bad, res))
        try:
            try:
                res.exit_code = await asyncio.wait_for(
                    proc.wait(), timeout=self.settings.session_timeout_seconds
                )
            except TimeoutError:
                res.timed_out = True
                res.exit_code = await self._kill_group(proc)
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog
            await self._wait_group_gone(proc.pid)
            await self._reader_barrier(reader)
            await self._clean_registration(reg, run_id)
        finally:
            if proc.returncode is None:
                # cancelled (shutdown) or crashed mid-run: never leave the group behind
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
            for t in (reader, watchdog):
                if not t.done():
                    t.cancel()
            for fp in (log_fp, journal_fp):
                with contextlib.suppress(Exception):
                    fp.close()
            transport = getattr(proc, "_transport", None)
            if transport is not None:
                with contextlib.suppress(Exception):
                    transport.close()
            self._procs.pop(proc.pid, None)
        res.tower_seen = reg.tower_seen
        res.registration_dirty = reg.dirty
        if res.start_failure is None and not reg.tower_seen:
            res.start_failure = f"exit_without_tower: exit code {res.exit_code}, no tower tool call"
        return res

    async def _watchdog(
        self,
        proc: asyncio.subprocess.Process,
        tower_up: asyncio.Event,
        bad_ev: asyncio.Event,
        tower_bad: list[str],
        res: AttemptResult,
    ) -> None:
        """Kill the group when tower is not connected (init) or never called within the limit."""
        up = asyncio.create_task(tower_up.wait())
        bad = asyncio.create_task(bad_ev.wait())
        try:
            done, _ = await asyncio.wait(
                {up, bad},
                timeout=self.settings.start_no_tower_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if up in done and bad not in done:
                return
            res.start_failure = (
                tower_bad[0]
                if tower_bad
                else f"start_hang: no tower in {self.settings.start_no_tower_timeout_seconds:g}s"
            )
            log.warning("start_failure_detected", pid=proc.pid, cause=res.start_failure)
            await self._kill_group(proc)
        finally:
            for t in (up, bad):
                t.cancel()

    async def _read(
        self,
        proc: asyncio.subprocess.Process,
        reg: StreamRegistrar,
        journal_fp: IO[bytes],
        log_fp: IO[bytes],
        on_event: Callable[[str, str], None],
    ) -> None:
        """Drain stdout forever: journal first, then parse. Never stops on a write failure."""
        stream = proc.stdout
        assert stream is not None
        discarding = False
        write_failed = False
        while True:
            eof = False
            try:
                line = await stream.readuntil(b"\n")
            except asyncio.IncompleteReadError as exc:
                line, eof = exc.partial, True
            except asyncio.LimitOverrunError as exc:
                with contextlib.suppress(Exception):
                    await stream.readexactly(exc.consumed)
                if not discarding:
                    log.error("stream_line_oversized", run_id=reg.run_id)
                discarding = True
                continue
            if discarding:
                # this chunk is the tail of the oversized line: drop it
                discarding = False
                if eof:
                    return
                continue
            if line:
                try:
                    journal_fp.write(line if line.endswith(b"\n") else line + b"\n")
                except OSError as exc:
                    if not write_failed:
                        write_failed = True
                        log.error(
                            "run_log_write_failed", run_id=reg.run_id, error=type(exc).__name__
                        )
                try:
                    events = reg.feed(line)
                except Exception:  # a parser bug must never stop the drain
                    log.exception("stream_feed_crashed", run_id=reg.run_id)
                    events = []
                    reg.dirty = True
                try:
                    if reg.last_type:
                        log_fp.write(f"# event {reg.last_type}\n".encode())
                    if reg.last_type.startswith("result") and reg.result is not None:
                        text = str(reg.result.get("result", ""))
                        log_fp.write(f"# result: {text}\n".encode())
                except OSError:
                    pass
                for kind, detail in events:
                    on_event(kind, detail)
            if eof:
                return

    async def _reader_barrier(self, reader: asyncio.Task[None]) -> None:
        """Wait for stdout EOF (pipe closed by the group exit), bounded."""
        try:
            await asyncio.wait_for(
                asyncio.shield(reader), timeout=self.settings.reader_drain_seconds
            )
        except TimeoutError:
            log.error("reader_drain_timeout")
            reader.cancel()
        except Exception:
            log.exception("reader_crashed")

    async def _clean_registration(self, reg: StreamRegistrar, run_id: str) -> None:
        """Replay the journal until registration is clean (SPEC §4.2 "registration failure")."""
        if not reg.dirty:
            return
        jpath = journal_path(self.settings.state_dir, run_id)
        for _ in range(60):
            replay = replay_journal(self.store, run_id, self._configs, jpath)
            if not replay.dirty:
                reg.tower_seen = reg.tower_seen or replay.tower_seen
                reg.dirty = False
                return
            await self._sleep(1.0)
        log.error("registration_still_dirty", run_id=run_id)

    # -- process group ---------------------------------------------------------

    async def _kill_group(self, proc: asyncio.subprocess.Process) -> int:
        """SIGTERM the process group; SIGKILL after 5s if it didn't exit."""
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            return await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            return await proc.wait()

    async def _wait_group_gone(self, pgid: int) -> None:
        """Block until no process of the group is left (descendants included).

        The slot is released only after this: a grandchild still running means the role is
        still "running" for FR-3. After ``GROUP_TERM_AFTER`` SIGTERM, +5 s SIGKILL; if the group
        still does not vanish the slot stays and an ERROR is logged once a minute.
        """
        t0 = time.monotonic()
        termed = killed = False
        last_err = 0.0
        while True:
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                return
            elapsed = time.monotonic() - t0
            if elapsed >= GROUP_TERM_AFTER and not termed:
                termed = True
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(pgid, signal.SIGTERM)
            if elapsed >= GROUP_KILL_AFTER and not killed:
                killed = True
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(pgid, signal.SIGKILL)
            if elapsed >= GROUP_KILL_AFTER + 1 and time.monotonic() - last_err >= 60:
                last_err = time.monotonic()
                log.error("process_group_survives_kill", pgid=pgid)
            await self._sleep(0.5)

    # -- outcome ---------------------------------------------------------------

    def _finish_failed_attempt(self, run: sqlite3.Row, res: AttemptResult) -> None:
        """A start-failed attempt that will be retried: close the process, no alert.

        ``outcome_done`` stays 0: the *group* is not finished. A crash in the backoff must
        reach recovery as an interrupted run (``conductor_restart`` alert), not as silently
        done work. ``_complete`` closes every attempt of the group.
        """
        self.store.execute(
            "UPDATE runs SET state='exited', exit_code=?, timed_out=?, start_failure=?,"
            " outcome_done=0, replayed=1, ended_at=? WHERE run_id=?",
            (res.exit_code, int(res.timed_out), res.start_failure, time.time(), run["run_id"]),
        )

    def _complete(self, run: sqlite3.Row, res: AttemptResult) -> str | None:
        """Outcome + slot release in ONE transaction (all or nothing on a crash).

        Alert table (SPEC §4.2): start_failed > abnormal_exit > no_handoff; a clean exit that
        left a handoff or an initiator message needs nothing. Deferred triggers of the key take
        the slot as one rerun; otherwise the slot is deleted.
        """
        now = time.time()
        run_id = str(run["run_id"])
        next_run: str | None = None
        alert_made = False
        with self.store.tx() as c:
            fresh = c.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            in_doubt = int(fresh["in_doubt"]) + int(
                c.execute(
                    "SELECT count(*) AS n FROM handoffs WHERE run_id=? AND state='called'",
                    (run_id,),
                ).fetchone()["n"]
            )
            msg = c.execute(
                "SELECT 1 FROM handoffs WHERE run_id=? AND state='posted' LIMIT 1", (run_id,)
            ).fetchone()
            clean = res.exit_code == 0 and not res.timed_out
            facts: dict[str, Any] = {
                "nickname": run["nickname"],
                "issue": run["issue_id"],
                "attempts": int(run["attempt"]) + 1,
                "exit_code": res.exit_code,
                "timed_out": res.timed_out or None,
                "in_doubt_tool_calls": in_doubt or None,
                "log": res.log_name or None,
            }
            reason: str | None = None
            if res.start_failure:
                reason = "start_failed"
                facts["cause"] = res.start_failure
            elif msg is not None:
                reason = None if clean else "abnormal_exit"
            else:
                reason = "no_handoff"
                if clean:
                    log.warning(
                        "finished_without_handoff",
                        run_id=run_id,
                        nickname=run["nickname"],
                        issue=run["issue_id"],
                    )
            if reason:
                alerts.enqueue(
                    c,
                    reason=reason,
                    subject=str(run["group_id"]),
                    workspace=run["workspace"],
                    plane_base_url=run["plane_base_url"],
                    project_id=run["project_id"],
                    issue_id=run["issue_id"],
                    facts=facts,
                    now=now,
                )
                alert_made = True
            c.execute(
                "UPDATE runs SET state='exited', exit_code=?, timed_out=?, start_failure=?,"
                " tower_seen=?, in_doubt=?, outcome_done=1, replayed=?, ended_at=? WHERE run_id=?",
                (
                    res.exit_code,
                    int(res.timed_out),
                    res.start_failure,
                    int(res.tower_seen),
                    in_doubt,
                    0 if res.registration_dirty else 1,  # dirty: recovery replays it next start
                    now,
                    run_id,
                ),
            )
            c.execute("UPDATE runs SET outcome_done=1 WHERE group_id=?", (run["group_id"],))
            c.execute(
                "UPDATE triggers SET state='done', finished_at=? WHERE state='running' AND run_id"
                " IN (SELECT run_id FROM runs WHERE group_id=?)",
                (now, run["group_id"]),
            )
            next_run = self._release_slot(c, run, now)
        if alert_made:
            self.on_alert()
        return next_run

    def _release_slot(self, c: sqlite3.Connection, run: sqlite3.Row, now: float) -> str | None:
        """Hand the slot to the deferred triggers of its key (one rerun) or delete it."""
        key = (run["workspace"], run["nickname"], run["root_id"])
        deferred = c.execute(
            "SELECT * FROM triggers WHERE workspace=? AND nickname=? AND root_id=?"
            " AND state='deferred' ORDER BY id",
            key,
        ).fetchall()
        if self.stopping or not deferred:
            c.execute("DELETE FROM slots WHERE workspace=? AND nickname=? AND root_id=?", key)
            return None
        new_run = uuid.uuid4().hex
        group = uuid.uuid4().hex
        last = deferred[-1]
        sources = sorted({str(t["source_run_id"]) for t in deferred if t["source_run_id"]})
        c.execute(
            "INSERT INTO runs(run_id, group_id, attempt, workspace, plane_base_url, project_id,"
            " nickname, root_id, issue_id, triggered_by_email, state, started_at)"
            " VALUES(?,?,0,?,?,?,?,?,?,?, 'starting', ?)",
            (
                new_run,
                group,
                run["workspace"],
                run["plane_base_url"],
                run["project_id"],
                run["nickname"],
                run["root_id"],
                last["issue_id"],
                last["triggered_by_email"],
                now,
            ),
        )
        c.execute(
            "UPDATE slots SET group_id=?, run_id=?, issue_id=?, covered_sources=?"
            " WHERE workspace=? AND nickname=? AND root_id=?",
            (group, new_run, last["issue_id"], json.dumps(sources), *key),
        )
        c.execute(
            "UPDATE triggers SET state='running', run_id=? WHERE id IN ({})".format(
                ",".join("?" * len(deferred))
            ),
            (new_run, *[t["id"] for t in deferred]),
        )
        return new_run

    def _emergency_release(self, run_id: str) -> None:
        """Last resort when a supervisor crashed: never leak a slot (it would block its key)."""
        try:
            with self.store.tx() as c:
                row = c.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if row is None:
                    return
                c.execute(
                    "UPDATE runs SET state='lost', outcome_done=1, replayed=1, ended_at=? WHERE run_id=?",
                    (time.time(), run_id),
                )
                c.execute(
                    "DELETE FROM slots WHERE workspace=? AND nickname=? AND root_id=?",
                    (row["workspace"], row["nickname"], row["root_id"]),
                )
        except Exception:
            log.exception("emergency_release_failed", run_id=run_id)

    # -- announce (optional, per workspace) --------------------------------------

    def _start_announce(self, run: sqlite3.Row) -> asyncio.Task[str | None] | None:
        entry = self.workspaces.get(str(run["workspace"]))
        if entry is None or not entry[0].announce_spawn:
            return None
        ws, plane = entry
        return asyncio.create_task(
            self._post_announce(
                plane, ws.project_id, str(run["nickname"]), UUID(str(run["issue_id"]))
            )
        )

    async def _finish_announce(
        self,
        run: sqlite3.Row,
        task: asyncio.Task[str | None] | None,
        res: AttemptResult,
        duration: float,
    ) -> None:
        if task is None:
            return
        try:
            comment_id = await asyncio.wait_for(task, timeout=15)
        except Exception:
            return
        if comment_id is None:
            return
        ws, plane = self.workspaces[str(run["workspace"])]
        await self._update_announce(
            plane,
            ws.project_id,
            str(run["nickname"]),
            UUID(str(run["issue_id"])),
            comment_id,
            res,
            duration,
        )

    @staticmethod
    async def _post_announce(
        plane: PlaneClient, project_id: UUID, nickname: str, issue_uuid: UUID
    ) -> str | None:
        body = (
            f"<p><strong><code>@{nickname}</code> picking up.</strong> "
            f"Working on it — this comment will be updated when the agent finishes.</p>"
        )
        try:
            resp = await asyncio.wait_for(
                plane.create_issue_comment(project_id, issue_uuid, body), timeout=10
            )
        except (PlaneAPIError, TimeoutError) as exc:
            log.warning(
                "announce_comment_failed",
                nickname=nickname,
                issue=str(issue_uuid),
                error=getattr(exc, "message", type(exc).__name__),
            )
            return None
        comment_id = resp.get("id") if isinstance(resp, dict) else None
        return str(comment_id) if comment_id else None

    @staticmethod
    async def _update_announce(
        plane: PlaneClient,
        project_id: UUID,
        nickname: str,
        issue_uuid: UUID,
        comment_id: str,
        res: AttemptResult,
        duration: float,
    ) -> None:
        if res.start_failure:
            verdict = "failed to start"
        elif res.timed_out:
            verdict = "timed out"
        elif res.exit_code == 0:
            verdict = "done"
        else:
            verdict = f"exited {res.exit_code}"
        body = (
            f"<p><strong><code>@{nickname}</code> {verdict}.</strong> "
            f"Duration: {_format_duration(duration)}.</p>"
        )
        try:
            await plane.update_issue_comment(project_id, issue_uuid, comment_id, body)
        except PlaneAPIError as exc:
            log.error(
                "announce_update_failed",
                nickname=nickname,
                issue=str(issue_uuid),
                status_code=exc.status_code,
                error=exc.message,
            )

    # -- shutdown -------------------------------------------------------------

    async def wait_idle(self, grace_seconds: float = 30.0) -> None:
        """Wait for in-flight supervisors. After ``grace_seconds``, kill all groups.

        Deferred reruns are not started during shutdown (``stopping``): the supervisors finish
        their current group and the slot is deleted; the deferred triggers survive in the store
        and run after the next start. ``asyncio.wait`` (not ``wait_for(gather)``) is used on
        purpose: a timeout must NOT cancel the supervisors, whose cleanup would drop the process
        handles before the kill step below collects them and let the agent groups survive.
        """
        self.stopping = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace_seconds
        while self._tasks:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.wait(list(self._tasks), timeout=remaining)
        if not self._tasks:
            return
        procs = list(self._procs.values())
        log.warning("shutdown_grace_expired", killing_groups=len(procs))
        for proc in procs:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
        # the supervisors now see the exit, finish their outcome and release their slots
        post_kill = loop.time() + 10.0
        while self._tasks and loop.time() < post_kill:
            await asyncio.wait(list(self._tasks), timeout=post_kill - loop.time())
        if self._tasks:
            log.warning("wait_idle_post_kill_drain_timeout", leftover_tasks=len(self._tasks))
            for t in list(self._tasks):
                t.cancel()
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
