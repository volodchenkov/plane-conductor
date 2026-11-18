"""Shared test infrastructure for the QSALE-55 reliability tests (NFR-1: no real Plane/LLM).

- ``FakePlane`` — in-memory stand-in for ``PlaneClient`` with injectable failures.
- ``fake_claude`` — a script that prints a scripted ``stream-json`` per attempt.
- ``Harness`` — store + runner + dispatcher + watcher wired together on a temp dir.
"""

from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.config import Settings
from plane_conductor.dispatcher import Dispatcher
from plane_conductor.exceptions import PlaneAPIError
from plane_conductor.roots import RootCache
from plane_conductor.runner import Runner
from plane_conductor.store import Store
from plane_conductor.watcher import Watcher

ISSUE = "33333333-3333-3333-3333-333333333333"
ROOT = "44444444-4444-4444-4444-444444444444"
SUB = "55555555-5555-5555-5555-555555555555"
SARK_M = "11111111-1111-1111-1111-111111111111"
RINZLER_M = "22222222-2222-2222-2222-222222222222"


class FakePlane:
    """Just enough of ``PlaneClient`` for runner / dispatcher / watcher."""

    def __init__(self) -> None:
        self.members: dict[str, dict[str, Any]] = {
            SARK_M: {"email": "sark@example.io"},
            RINZLER_M: {"email": "rinzler@example.io"},
        }
        self.project_members: list[dict[str, Any]] | None = None
        self.parents: dict[str, str | None] = {}
        self.issue_comments: dict[str, list[dict[str, Any]]] = {}
        self.created: list[dict[str, Any]] = []
        self.updated: list[tuple[str, str]] = []
        self.create_failures: list[BaseException] = []
        self.lose_response_once = False
        self.get_issue_error: BaseException | None = None
        self.members_error: BaseException | None = None
        self.seen_external_ids: dict[str, str] = {}
        self._n = 0

    async def get_member(self, member_id: Any) -> dict[str, Any]:
        key = str(member_id).lower()
        if key not in self.members:
            raise PlaneAPIError(404, f"missing {member_id}")
        return self.members[key]

    async def list_project_members(self, project_id: Any) -> list[dict[str, Any]]:
        if self.members_error is not None:
            raise self.members_error
        if self.project_members is not None:
            return self.project_members
        return list(self.members.values())

    async def get_issue(self, project_id: Any, issue_id: Any) -> dict[str, Any]:
        if self.get_issue_error is not None:
            raise self.get_issue_error
        key = str(issue_id)
        return {"id": key, "parent": self.parents.get(key)}

    async def list_issue_comments(self, project_id: Any, issue_id: Any) -> list[dict[str, Any]]:
        return list(self.issue_comments.get(str(issue_id), []))

    async def list_issue_comments_all(self, project_id: Any, issue_id: Any) -> list[dict[str, Any]]:
        return await self.list_issue_comments(project_id, issue_id)

    async def create_issue_comment(
        self,
        project_id: Any,
        issue_id: Any,
        comment_html: str,
        *,
        external_source: str | None = None,
        external_id: str | None = None,
    ) -> dict[str, Any]:
        if self.create_failures:
            raise self.create_failures.pop(0)
        if external_id and external_id in self.seen_external_ids:
            raise PlaneAPIError(409, "external id exists", method="POST")
        self._n += 1
        cid = f"c{self._n}"
        rec = {"issue": str(issue_id), "html": comment_html, "id": cid, "external_id": external_id}
        self.created.append(rec)
        if external_id:
            self.seen_external_ids[external_id] = cid
        self.issue_comments.setdefault(str(issue_id), []).append(
            {"id": cid, "comment_html": comment_html}
        )
        if self.lose_response_once:
            self.lose_response_once = False
            raise PlaneAPIError(0, "transport error: response lost", method="POST")
        return {"id": cid}

    async def update_issue_comment(
        self, project_id: Any, issue_id: Any, comment_id: Any, comment_html: str
    ) -> dict[str, Any]:
        self.updated.append((str(comment_id), comment_html))
        return {"id": comment_id}

    async def aclose(self) -> None:
        pass


# ---------------------------------------------------------------------------
# claude stream lines
# ---------------------------------------------------------------------------


def init_line(tower: str | None = "connected") -> str:
    servers: list[dict[str, Any]] = [{"name": "tavily", "status": "failed"}]
    if tower is not None:
        servers.insert(0, {"name": "plane-tower", "status": tower})
    return json.dumps({"type": "system", "subtype": "init", "mcp_servers": servers})


def tool_use_line(tool_use_id: str, tool: str, **tool_input: Any) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_use_id,
                        "name": f"mcp__plane-tower__{tool}",
                        "input": tool_input,
                    }
                ]
            },
        }
    )


def tool_result_line(tool_use_id: str, payload: Any, *, is_error: bool = False) -> str:
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": content,
                        "is_error": is_error,
                    }
                ]
            },
        }
    )


def result_line() -> str:
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "ok"})


def handoff_lines(
    tool_use_id: str, comment_id: str, *, role: str = "initiator", issue: str = SUB
) -> list[str]:
    """tool_use + successful tool_result of ``request_handoff``."""
    return [
        tool_use_line(tool_use_id, "request_handoff", sub_uuid=issue, target_role=role),
        tool_result_line(tool_use_id, {"comment_id": comment_id, "target_role": role}),
    ]


def good_run(*extra: str) -> list[str]:
    """init (tower up) + the given lines + result."""
    return [init_line(), *extra, result_line()]


def fake_claude(
    tmp: Path,
    attempts: list[dict[str, Any]],
    name: str = "fake_claude",
) -> str:
    """A fake ``claude``: per invocation ``n`` it replays ``attempts[min(n, last)]``.

    attempt keys: ``lines`` (printed to stdout), ``exit`` (code), ``sleep`` (seconds before
    exit), ``bg`` (seconds a background child keeps the process group alive after exit),
    ``stderr`` (text).
    """
    spec = tmp / f"{name}.json"
    spec.write_text(json.dumps(attempts))
    script = tmp / name
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, subprocess, sys, time\n"
        "sys.stdin.read()\n"
        f"spec = json.load(open({str(spec)!r}))\n"
        f"cnt = {str(tmp / (name + '.count'))!r}\n"
        "n = int(open(cnt).read()) if os.path.exists(cnt) else 0\n"
        "open(cnt, 'w').write(str(n + 1))\n"
        "a = spec[min(n, len(spec) - 1)]\n"
        "if a.get('bg'):\n"
        "    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(%s)' % a['bg']])\n"
        "for line in a.get('lines', []):\n"
        "    sys.stdout.write(line.replace('@N@', str(n)) + '\\n'); sys.stdout.flush()\n"
        "if a.get('stderr'): sys.stderr.write(a['stderr'] + '\\n')\n"
        "time.sleep(a.get('sleep', 0))\n"
        "sys.exit(a.get('exit', 0))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def invocations(tmp: Path, name: str = "fake_claude") -> int:
    p = tmp / f"{name}.count"
    return int(p.read_text()) if p.exists() else 0


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


ACTIVE: list[Harness] = []


async def cleanup_harnesses() -> None:
    """Kill leftover agent groups and cancel supervisors, so a failing test cannot hang the run."""
    import contextlib
    import os
    import signal

    while ACTIVE:
        h = ACTIVE.pop()
        for proc in list(h.runner._procs.values()):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
        tasks = list(h.runner._tasks)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class Harness:
    """Everything wired on a temp dir, driven by ``tick()`` calls (no background loops)."""

    def __init__(
        self,
        tmp: Path,
        base_settings: Settings,
        ws: WorkspaceConfig,
        attempts: list[dict[str, Any]] | None = None,
        **overrides: Any,
    ) -> None:
        ACTIVE.append(self)
        self.tmp = tmp
        self.clock = Clock()
        claude = fake_claude(tmp, attempts or [{"lines": good_run()}])
        base = {
            "claude_binary": claude,
            "log_dir": tmp / "logs",
            "state_dir": tmp / "state",
            "start_retries": 1,
            "start_retry_backoff_seconds": [0.0],
            "start_no_tower_timeout_seconds": 5.0,
            "reader_drain_seconds": 2.0,
            "session_timeout_seconds": 30,
            "queue_max_age_seconds": 100.0,
            "source_wait_seconds": 10.0,
        }
        self.settings = base_settings.model_copy(update=base | overrides)
        self.ws = ws
        self.plane = FakePlane()
        self.store = Store(self.settings.state_dir / "conductor.db")
        self.workspaces: dict[str, Any] = {ws.workspace_slug: (ws, self.plane)}

        async def no_sleep(_s: float) -> None:
            await asyncio.sleep(0)

        self.runner = Runner(self.settings, self.store, self.workspaces, sleep=no_sleep)
        self.roots = RootCache(self.workspaces)
        self.dispatcher = Dispatcher(
            self.settings, self.store, self.workspaces, self.runner, self.roots, clock=self.clock
        )
        self.watcher = Watcher(
            self.settings, self.store, self.workspaces, self.roots, clock=self.clock
        )
        self.runner.on_change = self.dispatcher.kick
        self.runner.pre_release = self.dispatcher.prepare_rerun
        self.runner.on_alert = self.watcher.kick

    # -- inputs ---------------------------------------------------------------

    def add_trigger(
        self,
        nickname: str = "rinzler",
        issue: str = ISSUE,
        *,
        comment_id: str | None = None,
        source: str | None = None,
        root: str | None = None,
        state: str = "new",
        kind: str = "webhook",
        member: str | None = None,
        actor: str = "",
        email: str | None = None,
        received_at: float | None = None,
    ) -> int:
        self._seq = getattr(self, "_seq", 0) + 1
        cid = comment_id or f"cm{self._seq}"
        root_id = root if root is not None else self.plane.parents.get(issue) or issue
        cur = self.store.execute(
            "INSERT INTO triggers(comment_id, member_uuid, nickname, workspace, plane_base_url,"
            " project_id, issue_id, root_id, source_run_id, kind, state, triggered_by_email,"
            " snapshot, received_at, enqueued_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                cid,
                member or f"m-{cid}",
                nickname if state != "received" or kind == "redeliver" else None,
                self.ws.workspace_slug,
                self.ws.plane_base_url,
                str(self.ws.project_id),
                issue,
                root_id if state != "received" else None,
                source,
                kind,
                state,
                email,
                json.dumps({"actor": actor}),
                received_at if received_at is not None else self.clock(),
                self.clock() if state != "received" else None,
            ),
        )
        return int(cur.lastrowid or 0)

    # -- driving -----------------------------------------------------------------

    async def settle(self, max_ticks: int = 400) -> None:
        for _ in range(max_ticks):
            await self.dispatcher.tick()
            if self.runner._tasks:
                await asyncio.sleep(0.03)
                continue
            await self.dispatcher.tick()
            if not self.runner._tasks:
                return
        raise AssertionError("harness did not settle")

    # -- views -----------------------------------------------------------------

    def runs(self, nickname: str | None = None) -> list[Any]:
        if nickname:
            return self.store.all(
                "SELECT * FROM runs WHERE nickname=? ORDER BY started_at", (nickname,)
            )
        return self.store.all("SELECT * FROM runs ORDER BY started_at")

    def alert_reasons(self) -> list[str]:
        return [
            r["reason"]
            for r in self.store.all("SELECT reason FROM alerts ORDER BY created_at, rowid")
        ]

    def trigger_states(self) -> list[str]:
        return [r["state"] for r in self.store.all("SELECT state FROM triggers ORDER BY id")]

    def slots(self) -> int:
        return int(self.store.one("SELECT count(*) AS n FROM slots")["n"])


def uid(value: str) -> UUID:
    return UUID(value)
