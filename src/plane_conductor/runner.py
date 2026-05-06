"""Subprocess spawner — multi-workspace, defends against realistic failure modes.

- **Dedup**: the same `(workspace_slug, nickname, issue_uuid)` cannot be spawned
  twice in this orchestrator. Plane delivers webhooks at-least-once and the
  human can double-mention; without this, two agents race to create the same
  artifact. The slug is included so two workspaces can share nicknames safely.
- **Capacity cap**: `MAX_CONCURRENT_SESSIONS` is host-wide — it counts active
  agents across all workspaces. Stops a flood of mentions from melting the box.
- **Process group**: every subprocess is spawned in its own session
  (`start_new_session=True`). On timeout we `killpg(SIGTERM)` then SIGKILL the
  whole group, so descendants of `claude` (MCP servers, helper procs) die too.
- **Sentinel files**: before spawn we touch `logs/.active/<slug>-<nick>-<issue>.json`
  and remove it on exit. On startup the server scans those — anything left
  over means conductor restarted while an agent was running, and we post a
  recovery comment to the right Plane workspace.
- **Announce-spawn (optional, per-workspace)**: when the workspace's
  `announce_spawn=True` we post a comment to the issue the moment we spawn the
  subprocess, then update that same comment when the agent exits.

State kept in memory: `_tasks` (supervisor task pins), `_active`
(in-flight `(slug, nick, issue)` keys), `_procs` (process handles for shutdown).
All three are derivable from "what's running right now"; nothing is persisted.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import IO
from uuid import UUID

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

log = get_logger(__name__)


def build_prompt(
    *,
    nickname: str,
    issue_uuid: UUID,
    plane_base_url: str,
    workspace_slug: str,
    project_id: str | UUID,
    triggered_by_email: str | None = None,
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
    lines.append("")
    lines.append("Pick up the issue and proceed per your role's protocol.")
    return "\n".join(lines)


def log_path_for(log_dir: Path, workspace_slug: str, nickname: str, issue_uuid: UUID) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return log_dir / f"{ts}-{workspace_slug}-{nickname}-{str(issue_uuid)[:8]}.log"


def _sentinel_dir(log_dir: Path) -> Path:
    p = log_dir / ".active"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _sentinel_path(log_dir: Path, workspace_slug: str, nickname: str, issue_uuid: UUID) -> Path:
    return _sentinel_dir(log_dir) / f"{workspace_slug}-{nickname}-{issue_uuid}.json"


def _format_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


class Runner:
    """Spawns claude subprocesses with dedup, capacity, and process-group control.

    Multi-workspace: the runner itself doesn't know which workspaces exist; it
    receives one `WorkspaceConfig` (and its matching `PlaneClient`) per spawn.
    The host-wide capacity cap is enforced across all workspaces.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._tasks: set[asyncio.Task[None]] = set()
        self._active: set[tuple[str, str, UUID]] = set()
        # pid → Process. pid is also the pgid since we use start_new_session=True.
        self._procs: dict[int, asyncio.subprocess.Process] = {}

    @property
    def active_count(self) -> int:
        return len(self._active)

    async def spawn(
        self,
        *,
        workspace: WorkspaceConfig,
        plane: PlaneClient,
        nickname: str,
        issue_uuid: UUID,
        triggered_by_email: str | None = None,
    ) -> Path:
        """Spawn `claude --agent <nickname>` for `(workspace, issue_uuid)`.

        Raises:
            SessionAlreadyRunningError: an agent for this triple is in flight.
            CapacityFullError: MAX_CONCURRENT_SESSIONS reached (host-wide).
            AgentSpawnError: claude binary missing.
        """
        slug = workspace.workspace_slug
        key = (slug, nickname, issue_uuid)
        if key in self._active:
            raise SessionAlreadyRunningError(
                f"@{nickname} already running on {issue_uuid} in {slug}"
            )
        if len(self._active) >= self.settings.max_concurrent_sessions:
            raise CapacityFullError(
                f"max_concurrent_sessions={self.settings.max_concurrent_sessions} reached"
            )

        log_path = log_path_for(self.settings.log_dir, slug, nickname, issue_uuid)
        prompt = build_prompt(
            nickname=nickname,
            issue_uuid=issue_uuid,
            plane_base_url=workspace.plane_base_url,
            workspace_slug=slug,
            project_id=workspace.project_id,
            triggered_by_email=triggered_by_email,
        )
        argv = [self.settings.claude_binary, "--agent", nickname, "--print"]
        cwd = workspace.agent_working_dir or Path.cwd()

        log_fp = log_path.open("ab", buffering=0)
        log_fp.write(
            f"# {datetime.now(UTC).isoformat()} spawn workspace={slug} argv={argv!r} cwd={cwd}\n".encode()
        )
        log_fp.write(f"# prompt:\n{prompt}\n# --- stdout/stderr ---\n".encode())

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=log_fp,
                stderr=log_fp,
                cwd=str(cwd),
                env=os.environ.copy(),
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            log_fp.close()
            raise AgentSpawnError(
                f"claude binary not found at {self.settings.claude_binary!r}"
            ) from exc

        self._active.add(key)
        self._procs[proc.pid] = proc
        self._write_sentinel(slug, nickname, issue_uuid, log_path)

        log.info(
            "agent_spawned",
            workspace=slug,
            nickname=nickname,
            issue=str(issue_uuid),
            pid=proc.pid,
            pgid=proc.pid,
            log_path=str(log_path),
            active=len(self._active),
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

        announce_comment_id: str | None = None
        if workspace.announce_spawn:
            announce_comment_id = await self._post_announce(
                plane, workspace.project_id, nickname, issue_uuid
            )

        started_at = time.monotonic()
        task = asyncio.create_task(
            self._supervise(
                proc=proc,
                log_fp=log_fp,
                plane=plane,
                project_id=workspace.project_id,
                workspace_slug=slug,
                nickname=nickname,
                issue_uuid=issue_uuid,
                announce_comment_id=announce_comment_id,
                started_at=started_at,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

        return log_path

    async def _supervise(
        self,
        *,
        proc: asyncio.subprocess.Process,
        log_fp: IO[bytes],
        plane: PlaneClient,
        project_id: UUID,
        workspace_slug: str,
        nickname: str,
        issue_uuid: UUID,
        announce_comment_id: str | None,
        started_at: float,
    ) -> None:
        timeout = self.settings.session_timeout_seconds
        timed_out = False
        exit_code = -1
        try:
            try:
                exit_code = await asyncio.wait_for(proc.wait(), timeout=timeout)
            except TimeoutError:
                timed_out = True
                exit_code = await self._kill_group(proc)
        finally:
            with contextlib.suppress(Exception):
                log_fp.close()
            transport = getattr(proc, "_transport", None)
            if transport is not None:
                with contextlib.suppress(Exception):
                    transport.close()
            self._active.discard((workspace_slug, nickname, issue_uuid))
            self._procs.pop(proc.pid, None)
            self._clear_sentinel(workspace_slug, nickname, issue_uuid)

        duration = time.monotonic() - started_at
        log.info(
            "agent_exited",
            workspace=workspace_slug,
            nickname=nickname,
            issue=str(issue_uuid),
            exit_code=exit_code,
            timed_out=timed_out,
            duration_s=round(duration, 1),
            active=len(self._active),
        )

        if announce_comment_id is not None:
            await self._update_announce(
                plane,
                project_id,
                nickname,
                issue_uuid,
                announce_comment_id,
                exit_code,
                timed_out,
                duration,
            )
        elif exit_code != 0 or timed_out:
            await self._notify_failure(
                plane, project_id, nickname, issue_uuid, exit_code, timed_out
            )

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

    # -- Plane comment helpers ------------------------------------------------

    @staticmethod
    async def _post_announce(
        plane: PlaneClient,
        project_id: UUID,
        nickname: str,
        issue_uuid: UUID,
    ) -> str | None:
        body = (
            f"<p><strong><code>@{nickname}</code> picking up.</strong> "
            f"Working on it — this comment will be updated when the agent finishes.</p>"
        )
        try:
            resp = await plane.create_issue_comment(project_id, issue_uuid, body)
        except PlaneAPIError as exc:
            log.warning(
                "announce_comment_failed",
                nickname=nickname,
                issue=str(issue_uuid),
                status_code=exc.status_code,
                error=exc.message,
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
        exit_code: int,
        timed_out: bool,
        duration: float,
    ) -> None:
        if timed_out:
            verdict = "timed out"
        elif exit_code == 0:
            verdict = "done"
        else:
            verdict = f"exited {exit_code}"
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

    @staticmethod
    async def _notify_failure(
        plane: PlaneClient,
        project_id: UUID,
        nickname: str,
        issue_uuid: UUID,
        exit_code: int,
        timed_out: bool,
    ) -> None:
        kind = "timed out" if timed_out else f"exited {exit_code}"
        body = f"<p><strong><code>@{nickname}</code> {kind}.</strong> See orchestrator logs.</p>"
        try:
            await plane.create_issue_comment(project_id, issue_uuid, body)
        except PlaneAPIError as exc:
            log.error(
                "failure_comment_post_failed",
                nickname=nickname,
                issue=str(issue_uuid),
                status_code=exc.status_code,
                error=exc.message,
            )

    # -- sentinel file helpers ------------------------------------------------

    def _write_sentinel(
        self,
        workspace_slug: str,
        nickname: str,
        issue_uuid: UUID,
        log_path: Path,
    ) -> None:
        path = _sentinel_path(self.settings.log_dir, workspace_slug, nickname, issue_uuid)
        payload = {
            "workspace_slug": workspace_slug,
            "nickname": nickname,
            "issue_uuid": str(issue_uuid),
            "log_path": str(log_path),
            "started_at": datetime.now(UTC).isoformat(),
        }
        with contextlib.suppress(OSError):
            path.write_text(json.dumps(payload), encoding="utf-8")

    def _clear_sentinel(self, workspace_slug: str, nickname: str, issue_uuid: UUID) -> None:
        path = _sentinel_path(self.settings.log_dir, workspace_slug, nickname, issue_uuid)
        with contextlib.suppress(FileNotFoundError, OSError):
            path.unlink()

    # -- shutdown -------------------------------------------------------------

    async def wait_idle(self, grace_seconds: float = 30.0) -> None:
        """Wait for in-flight supervisors. After `grace_seconds`, kill all groups."""
        if not self._tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._tasks, return_exceptions=True),
                timeout=grace_seconds,
            )
        except TimeoutError:
            procs = list(self._procs.values())
            log.warning("shutdown_grace_expired", killing_groups=len(procs))
            for proc in procs:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
            for proc in procs:
                with contextlib.suppress(Exception):
                    await proc.wait()
            await asyncio.gather(*self._tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# Recovery scan — called from server lifespan on startup.
# ---------------------------------------------------------------------------


async def recover_orphaned_sessions(
    settings: Settings,
    workspaces: dict[str, tuple[WorkspaceConfig, PlaneClient]],
) -> int:
    """Post a recovery comment for each leftover sentinel; remove them.

    `workspaces` is a slug-keyed dict mapping to `(WorkspaceConfig, PlaneClient)`.
    A sentinel for an unknown workspace (config removed since restart) is
    logged and the file deleted — we can't post anywhere meaningful.
    """
    sentinel_dir = settings.log_dir / ".active"
    if not sentinel_dir.exists():
        return 0
    found = 0
    for path in sentinel_dir.iterdir():
        if path.suffix != ".json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            issue_uuid = UUID(data["issue_uuid"])
            nickname = data["nickname"]
            slug = data.get("workspace_slug")
            log_path = data.get("log_path", "(unknown)")
            started_at = data.get("started_at", "(unknown)")
        except (OSError, ValueError, KeyError) as exc:
            log.warning("sentinel_unreadable", path=str(path), error=str(exc))
            with contextlib.suppress(OSError):
                path.unlink()
            continue

        if not slug or slug not in workspaces:
            log.warning(
                "sentinel_orphan_workspace_unknown",
                path=str(path),
                workspace_slug=slug,
                nickname=nickname,
            )
            with contextlib.suppress(OSError):
                path.unlink()
            continue

        workspace, plane = workspaces[slug]
        body = (
            f"<p><strong><code>@{nickname}</code> was running when "
            f"the orchestrator restarted.</strong><br>"
            f"Started: {started_at}. Log: <code>{log_path}</code>.<br>"
            f"Mention me again to continue from where I left off.</p>"
        )
        try:
            await plane.create_issue_comment(workspace.project_id, issue_uuid, body)
        except PlaneAPIError as exc:
            log.error(
                "recovery_comment_failed",
                workspace=slug,
                nickname=nickname,
                issue=str(issue_uuid),
                status_code=exc.status_code,
                error=exc.message,
            )
        finally:
            with contextlib.suppress(OSError):
                path.unlink()
        found += 1
    if found:
        log.info("recovered_orphaned_sessions", count=found)
    return found
