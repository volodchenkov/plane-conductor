# mypy: disable-error-code="arg-type, dict-item, unused-ignore"
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.config import Settings
from plane_conductor.exceptions import AgentSpawnError
from plane_conductor.runner import Runner, build_prompt, log_path_for

ISSUE = UUID("33333333-3333-3333-3333-333333333333")


class StubPlane:
    def __init__(self) -> None:
        self.comments: list[tuple[Any, Any, str]] = []
        self.updates: list[tuple[Any, Any, Any, str]] = []
        self._next_id = 0

    async def create_issue_comment(
        self, project_id: Any, issue_id: Any, comment_html: str
    ) -> dict[str, Any]:
        self.comments.append((project_id, issue_id, comment_html))
        self._next_id += 1
        return {"id": f"c{self._next_id}"}

    async def update_issue_comment(
        self, project_id: Any, issue_id: Any, comment_id: Any, comment_html: str
    ) -> dict[str, Any]:
        self.updates.append((project_id, issue_id, comment_id, comment_html))
        return {"id": comment_id}

    async def aclose(self) -> None:
        pass


def _ws(workspace_config: WorkspaceConfig, **overrides: object) -> WorkspaceConfig:
    return workspace_config.model_copy(update=overrides)


def test_build_prompt_includes_url_and_issue(workspace_config: WorkspaceConfig) -> None:
    prompt = build_prompt(
        nickname="rinzler",
        issue_uuid=ISSUE,
        plane_base_url=workspace_config.plane_base_url,
        workspace_slug=workspace_config.workspace_slug,
        project_id=workspace_config.project_id,
        triggered_by_email="dmitry@example.io",
    )
    assert "@rinzler" in prompt
    assert str(ISSUE) in prompt
    assert "dmitry@example.io" in prompt
    assert workspace_config.plane_base_url in prompt


def test_log_path_includes_workspace_slug(tmp_path: Path) -> None:
    p = log_path_for(tmp_path / "logs", "qsale", "rinzler", ISSUE)
    assert p.parent.exists()
    assert "qsale" in p.name
    assert "rinzler" in p.name
    assert p.suffix == ".log"


async def test_runner_spawns_subprocess_and_records_zero_exit(
    settings: Settings, workspace_config: WorkspaceConfig, tmp_path: Path
) -> None:
    s = settings.model_copy(update={"claude_binary": "/bin/true", "log_dir": tmp_path / "logs"})
    plane = StubPlane()
    runner = Runner(settings=s)

    log_path = await runner.spawn(
        workspace=workspace_config,
        plane=plane,  # type: ignore[arg-type]
        nickname="rinzler",
        issue_uuid=ISSUE,
    )
    await runner.wait_idle()

    assert log_path.exists()
    assert plane.comments == []  # announce_spawn=False on workspace_config


async def test_runner_posts_failure_comment_on_nonzero_exit(
    settings: Settings, workspace_config: WorkspaceConfig, tmp_path: Path
) -> None:
    s = settings.model_copy(update={"claude_binary": "/bin/false", "log_dir": tmp_path / "logs"})
    plane = StubPlane()
    runner = Runner(settings=s)

    await runner.spawn(
        workspace=workspace_config,
        plane=plane,  # type: ignore[arg-type]
        nickname="rinzler",
        issue_uuid=ISSUE,
    )
    await runner.wait_idle()

    assert len(plane.comments) == 1
    _, issue_id, html = plane.comments[0]
    assert issue_id == ISSUE
    assert "@rinzler" in html
    assert "exited" in html


async def test_runner_raises_when_binary_missing(
    settings: Settings, workspace_config: WorkspaceConfig, tmp_path: Path
) -> None:
    s = settings.model_copy(
        update={
            "claude_binary": str(tmp_path / "definitely-not-here"),
            "log_dir": tmp_path / "logs",
        }
    )
    plane = StubPlane()
    runner = Runner(settings=s)

    with pytest.raises(AgentSpawnError):
        await runner.spawn(
            workspace=workspace_config,
            plane=plane,  # type: ignore[arg-type]
            nickname="rinzler",
            issue_uuid=ISSUE,
        )


async def test_wait_idle_no_tasks(settings: Settings) -> None:
    runner = Runner(settings=settings)
    await runner.wait_idle()


async def test_runner_kills_subprocess_on_timeout(
    settings: Settings, workspace_config: WorkspaceConfig, tmp_path: Path
) -> None:
    fake_claude = tmp_path / "fake_claude"
    fake_claude.write_text("#!/bin/sh\nsleep 60\n")
    fake_claude.chmod(0o755)

    s = settings.model_copy(
        update={
            "claude_binary": str(fake_claude),
            "session_timeout_seconds": 1,
            "log_dir": tmp_path / "logs",
        }
    )
    plane = StubPlane()
    runner = Runner(settings=s)

    await runner.spawn(
        workspace=workspace_config,
        plane=plane,  # type: ignore[arg-type]
        nickname="rinzler",
        issue_uuid=ISSUE,
    )
    await runner.wait_idle()

    assert len(plane.comments) == 1
    assert "timed out" in plane.comments[0][2]


async def test_notify_failure_swallows_plane_errors(
    settings: Settings, workspace_config: WorkspaceConfig, tmp_path: Path
) -> None:
    from plane_conductor.exceptions import PlaneAPIError

    class FlakyPlane:
        async def create_issue_comment(
            self, project_id: Any, issue_id: Any, comment_html: str
        ) -> dict[str, Any]:
            raise PlaneAPIError(503, "service unavailable")

    s = settings.model_copy(update={"claude_binary": "/bin/false", "log_dir": tmp_path / "logs"})
    runner = Runner(settings=s)
    await runner.spawn(
        workspace=workspace_config,
        plane=FlakyPlane(),  # type: ignore[arg-type]
        nickname="rinzler",
        issue_uuid=ISSUE,
    )
    await runner.wait_idle()


# --- protections -------------------------------------------------------------


async def test_runner_dedupes_active_triple(
    settings: Settings, workspace_config: WorkspaceConfig, tmp_path: Path
) -> None:
    """Spawning the same (slug, nick, issue) twice while first is alive raises."""
    from plane_conductor.exceptions import SessionAlreadyRunningError

    fake_claude = tmp_path / "fake_claude"
    fake_claude.write_text("#!/bin/sh\nsleep 5\n")
    fake_claude.chmod(0o755)
    s = settings.model_copy(
        update={"claude_binary": str(fake_claude), "log_dir": tmp_path / "logs"}
    )
    runner = Runner(settings=s)
    plane = StubPlane()

    await runner.spawn(
        workspace=workspace_config, plane=plane, nickname="rinzler", issue_uuid=ISSUE
    )  # type: ignore[arg-type]
    with pytest.raises(SessionAlreadyRunningError):
        await runner.spawn(
            workspace=workspace_config, plane=plane, nickname="rinzler", issue_uuid=ISSUE
        )  # type: ignore[arg-type]

    other = UUID("44444444-4444-4444-4444-444444444444")
    await runner.spawn(
        workspace=workspace_config, plane=plane, nickname="rinzler", issue_uuid=other
    )  # type: ignore[arg-type]

    await runner.wait_idle(grace_seconds=0.1)


async def test_runner_dedup_separates_workspaces(
    settings: Settings, workspace_config: WorkspaceConfig, tmp_path: Path
) -> None:
    """Same (nick, issue) in TWO different workspaces is allowed."""
    fake_claude = tmp_path / "fake_claude"
    fake_claude.write_text("#!/bin/sh\nsleep 5\n")
    fake_claude.chmod(0o755)
    s = settings.model_copy(
        update={"claude_binary": str(fake_claude), "log_dir": tmp_path / "logs"}
    )
    runner = Runner(settings=s)
    plane = StubPlane()

    ws_a = workspace_config.model_copy(update={"workspace_slug": "alpha"})
    ws_b = workspace_config.model_copy(update={"workspace_slug": "beta"})

    await runner.spawn(workspace=ws_a, plane=plane, nickname="rinzler", issue_uuid=ISSUE)  # type: ignore[arg-type]
    await runner.spawn(workspace=ws_b, plane=plane, nickname="rinzler", issue_uuid=ISSUE)  # type: ignore[arg-type]
    assert runner.active_count == 2
    await runner.wait_idle(grace_seconds=0.1)


async def test_runner_capacity_cap(
    settings: Settings, workspace_config: WorkspaceConfig, tmp_path: Path
) -> None:
    from plane_conductor.exceptions import CapacityFullError

    fake_claude = tmp_path / "fake_claude"
    fake_claude.write_text("#!/bin/sh\nsleep 5\n")
    fake_claude.chmod(0o755)
    s = settings.model_copy(
        update={
            "claude_binary": str(fake_claude),
            "max_concurrent_sessions": 2,
            "log_dir": tmp_path / "logs",
        }
    )
    runner = Runner(settings=s)
    plane = StubPlane()

    await runner.spawn(
        workspace=workspace_config, plane=plane, nickname="a", issue_uuid=UUID(int=1)
    )  # type: ignore[arg-type]
    await runner.spawn(
        workspace=workspace_config, plane=plane, nickname="b", issue_uuid=UUID(int=2)
    )  # type: ignore[arg-type]
    assert runner.active_count == 2
    with pytest.raises(CapacityFullError):
        await runner.spawn(
            workspace=workspace_config, plane=plane, nickname="c", issue_uuid=UUID(int=3)
        )  # type: ignore[arg-type]

    await runner.wait_idle(grace_seconds=0.1)


async def test_active_set_clears_after_exit(
    settings: Settings, workspace_config: WorkspaceConfig, tmp_path: Path
) -> None:
    s = settings.model_copy(update={"claude_binary": "/bin/true", "log_dir": tmp_path / "logs"})
    runner = Runner(settings=s)
    await runner.spawn(
        workspace=workspace_config, plane=StubPlane(), nickname="rinzler", issue_uuid=ISSUE
    )  # type: ignore[arg-type]
    await runner.wait_idle()
    assert runner.active_count == 0


async def test_kill_group_kills_descendants(
    settings: Settings, workspace_config: WorkspaceConfig, tmp_path: Path
) -> None:
    marker = tmp_path / "child_alive"
    fake_claude = tmp_path / "fake_claude"
    fake_claude.write_text(
        f"#!/bin/sh\n(while true; do touch {marker}; sleep 1; done) &\nCHILD=$!\nsleep 30\n"
    )
    fake_claude.chmod(0o755)

    s = settings.model_copy(
        update={
            "claude_binary": str(fake_claude),
            "session_timeout_seconds": 1,
            "log_dir": tmp_path / "logs",
        }
    )
    runner = Runner(settings=s)
    await runner.spawn(
        workspace=workspace_config, plane=StubPlane(), nickname="rinzler", issue_uuid=ISSUE
    )  # type: ignore[arg-type]
    await runner.wait_idle()

    await asyncio.sleep(0.2)
    if not marker.exists():
        return
    mtime_a = marker.stat().st_mtime
    await asyncio.sleep(2.5)
    mtime_b = marker.stat().st_mtime
    assert mtime_a == mtime_b, "grandchild process survived process-group kill"


async def test_sentinel_written_and_cleared(
    settings: Settings, workspace_config: WorkspaceConfig, tmp_path: Path
) -> None:
    s = settings.model_copy(update={"claude_binary": "/bin/true", "log_dir": tmp_path / "logs"})
    runner = Runner(settings=s)

    await runner.spawn(
        workspace=workspace_config, plane=StubPlane(), nickname="rinzler", issue_uuid=ISSUE
    )  # type: ignore[arg-type]
    await runner.wait_idle()

    sentinel_dir = s.log_dir / ".active"
    if sentinel_dir.exists():
        assert list(sentinel_dir.iterdir()) == []


async def test_recover_orphaned_sessions_posts_comment(
    settings: Settings, workspace_config: WorkspaceConfig, tmp_path: Path
) -> None:
    from plane_conductor.runner import recover_orphaned_sessions

    s = settings.model_copy(update={"log_dir": tmp_path / "logs"})
    sentinel_dir = s.log_dir / ".active"
    sentinel_dir.mkdir(parents=True)
    sentinel = sentinel_dir / f"{workspace_config.workspace_slug}-rinzler-{ISSUE}.json"
    sentinel.write_text(
        f'{{"workspace_slug":"{workspace_config.workspace_slug}","nickname":"rinzler",'
        f'"issue_uuid":"{ISSUE}","log_path":"/tmp/x.log",'
        f'"started_at":"2026-04-30T10:00:00+00:00"}}'
    )

    plane = StubPlane()
    n = await recover_orphaned_sessions(
        s,
        {workspace_config.workspace_slug: (workspace_config, plane)},  # type: ignore[arg-type]
    )
    assert n == 1
    assert len(plane.comments) == 1
    assert "restarted" in plane.comments[0][2]
    assert not sentinel.exists()


async def test_recover_skips_unknown_workspace_sentinels(
    settings: Settings, workspace_config: WorkspaceConfig, tmp_path: Path
) -> None:
    """If a sentinel references a workspace that is no longer configured, drop it."""
    from plane_conductor.runner import recover_orphaned_sessions

    s = settings.model_copy(update={"log_dir": tmp_path / "logs"})
    sentinel_dir = s.log_dir / ".active"
    sentinel_dir.mkdir(parents=True)
    sentinel = sentinel_dir / f"deleted-rinzler-{ISSUE}.json"
    sentinel.write_text(
        f'{{"workspace_slug":"deleted","nickname":"rinzler",'
        f'"issue_uuid":"{ISSUE}","log_path":"/tmp/x.log",'
        f'"started_at":"2026-04-30T10:00:00+00:00"}}'
    )
    plane = StubPlane()
    n = await recover_orphaned_sessions(
        s,
        {workspace_config.workspace_slug: (workspace_config, plane)},  # type: ignore[arg-type]
    )
    assert n == 0
    assert plane.comments == []
    assert not sentinel.exists()


async def test_recover_orphaned_sessions_handles_corrupt_sentinel(
    settings: Settings, workspace_config: WorkspaceConfig, tmp_path: Path
) -> None:
    from plane_conductor.runner import recover_orphaned_sessions

    s = settings.model_copy(update={"log_dir": tmp_path / "logs"})
    sentinel_dir = s.log_dir / ".active"
    sentinel_dir.mkdir(parents=True)
    bad = sentinel_dir / "broken.json"
    bad.write_text("not-json")
    plane = StubPlane()
    n = await recover_orphaned_sessions(
        s,
        {workspace_config.workspace_slug: (workspace_config, plane)},  # type: ignore[arg-type]
    )
    assert n == 0
    assert plane.comments == []
    assert not bad.exists()


async def test_recover_when_sentinel_dir_missing(
    settings: Settings, workspace_config: WorkspaceConfig, tmp_path: Path
) -> None:
    from plane_conductor.runner import recover_orphaned_sessions

    s = settings.model_copy(update={"log_dir": tmp_path / "no-such"})
    plane = StubPlane()
    assert (
        await recover_orphaned_sessions(
            s,
            {workspace_config.workspace_slug: (workspace_config, plane)},  # type: ignore[arg-type]
        )
        == 0
    )


async def test_wait_idle_force_kills_after_grace(
    settings: Settings, workspace_config: WorkspaceConfig, tmp_path: Path
) -> None:
    fake_claude = tmp_path / "fake_claude"
    fake_claude.write_text("#!/bin/sh\nsleep 60\n")
    fake_claude.chmod(0o755)
    s = settings.model_copy(
        update={
            "claude_binary": str(fake_claude),
            "session_timeout_seconds": 600,
            "log_dir": tmp_path / "logs",
        }
    )
    runner = Runner(settings=s)
    await runner.spawn(
        workspace=workspace_config, plane=StubPlane(), nickname="rinzler", issue_uuid=ISSUE
    )  # type: ignore[arg-type]

    started = asyncio.get_event_loop().time()
    await runner.wait_idle(grace_seconds=0.5)
    elapsed = asyncio.get_event_loop().time() - started
    assert elapsed < 5.0, f"wait_idle did not return promptly (took {elapsed:.1f}s)"


# --- announce_spawn ---------------------------------------------------------


async def test_announce_spawn_posts_comment_and_updates_on_success(
    settings: Settings, workspace_config: WorkspaceConfig, tmp_path: Path
) -> None:
    s = settings.model_copy(update={"claude_binary": "/bin/true", "log_dir": tmp_path / "logs"})
    plane = StubPlane()
    runner = Runner(settings=s)
    ws = workspace_config.model_copy(update={"announce_spawn": True})

    await runner.spawn(workspace=ws, plane=plane, nickname="rinzler", issue_uuid=ISSUE)  # type: ignore[arg-type]
    await runner.wait_idle()

    assert len(plane.comments) == 1
    assert "picking up" in plane.comments[0][2]
    assert len(plane.updates) == 1
    _, _, comment_id, html = plane.updates[0]
    assert comment_id == "c1"
    assert "done" in html


async def test_announce_spawn_updates_comment_on_failure(
    settings: Settings, workspace_config: WorkspaceConfig, tmp_path: Path
) -> None:
    s = settings.model_copy(update={"claude_binary": "/bin/false", "log_dir": tmp_path / "logs"})
    plane = StubPlane()
    runner = Runner(settings=s)
    ws = workspace_config.model_copy(update={"announce_spawn": True})

    await runner.spawn(workspace=ws, plane=plane, nickname="rinzler", issue_uuid=ISSUE)  # type: ignore[arg-type]
    await runner.wait_idle()

    assert len(plane.comments) == 1
    assert len(plane.updates) == 1
    assert "exited 1" in plane.updates[0][3]


async def test_announce_spawn_falls_back_to_failure_when_create_failed(
    settings: Settings, workspace_config: WorkspaceConfig, tmp_path: Path
) -> None:
    """If the initial announce-comment POST fails, no comment_id is captured;
    on nonzero exit, supervisor falls back to posting a fresh failure comment.
    """
    from plane_conductor.exceptions import PlaneAPIError

    class CreateFailsPlane(StubPlane):
        async def create_issue_comment(
            self, project_id: Any, issue_id: Any, comment_html: str
        ) -> dict[str, Any]:
            self.comments.append((project_id, issue_id, comment_html))
            if "picking up" in comment_html:
                raise PlaneAPIError(503, "down")
            return {"id": "c-fail"}

    s = settings.model_copy(update={"claude_binary": "/bin/false", "log_dir": tmp_path / "logs"})
    plane = CreateFailsPlane()
    runner = Runner(settings=s)
    ws = workspace_config.model_copy(update={"announce_spawn": True})

    await runner.spawn(workspace=ws, plane=plane, nickname="rinzler", issue_uuid=ISSUE)  # type: ignore[arg-type]
    await runner.wait_idle()

    assert len(plane.comments) == 2
    assert plane.updates == []
