# mypy: disable-error-code="arg-type, dict-item, unused-ignore, attr-defined"
"""Runner: stream reader, start failure (FR-2), outcome (FR-5), process-group release.

End to end through the Dispatcher with a fake ``claude`` that prints scripted ``stream-json``.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from uuid import UUID

from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.config import Settings
from plane_conductor.runner import build_prompt, journal_path, log_path_for, spawn_env
from tests.helpers import (
    ISSUE,
    SUB,
    Harness,
    good_run,
    handoff_lines,
    init_line,
    invocations,
    result_line,
    tool_result_line,
    tool_use_line,
)


def test_build_prompt_includes_url_and_issue(workspace_config: WorkspaceConfig) -> None:
    prompt = build_prompt(
        nickname="rinzler",
        issue_uuid=UUID(ISSUE),
        plane_base_url=workspace_config.plane_base_url,
        workspace_slug=workspace_config.workspace_slug,
        project_id=workspace_config.project_id,
        triggered_by_email="dmitry@example.io",
    )
    assert "@rinzler" in prompt and ISSUE in prompt and "dmitry@example.io" in prompt


def test_log_path_includes_workspace_slug(tmp_path: Path) -> None:
    p = log_path_for(tmp_path / "logs", "qsale", "rinzler", UUID(ISSUE))
    assert p.parent.exists() and "qsale" in p.name and "rinzler" in p.name and p.suffix == ".log"


def test_spawn_env_marks_run_and_workspace() -> None:
    env = spawn_env("ws", "abc123")
    assert env["CONDUCTOR_RUN_ID"] == "abc123" and env["WORKSPACE_SLUG"] == "ws"
    assert "TMUX" not in env


async def test_agent_with_handoff_runs_once_and_raises_no_alert(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(
        tmp_path,
        settings,
        workspace_config,
        [{"lines": good_run(*handoff_lines("t1", "c-1"))}],
    )
    h.add_trigger()
    await h.settle()
    assert invocations(tmp_path) == 1
    assert h.alert_reasons() == []
    assert h.slots() == 0
    (run,) = h.runs()
    assert run["state"] == "exited" and run["tower_seen"] == 1 and run["outcome_done"] == 1
    # the journal is the replayable record of the stream
    assert journal_path(h.settings.state_dir, run["run_id"]).read_text().count("\n") >= 4


# --- FR-2: start failure ------------------------------------------------------------


async def test_fr2_exit_zero_without_tower_is_retried_then_one_alert(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    """Given a run that exits 0 in seconds with no tower call; Then 1+retries attempts, 1 alert."""
    h = Harness(tmp_path, settings, workspace_config, [{"lines": [], "exit": 0}], start_retries=2)
    h.add_trigger()
    await h.settle()
    assert invocations(tmp_path) == 3  # first attempt + 2 retries
    assert h.alert_reasons() == ["start_failed"]
    alert = h.store.one("SELECT facts FROM alerts")
    assert '"attempts": 3' in alert["facts"] and "exit_without_tower" in alert["facts"]
    assert h.slots() == 0


async def test_fr2_first_attempt_fails_second_succeeds_no_alert(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(
        tmp_path,
        settings,
        workspace_config,
        [{"lines": []}, {"lines": good_run(*handoff_lines("t1", "c-1"))}],
    )
    h.add_trigger()
    await h.settle()
    assert invocations(tmp_path) == 2
    assert h.alert_reasons() == []
    assert [r["attempt"] for r in h.runs()] == [0, 1]
    assert h.runs()[0]["start_failure"].startswith("exit_without_tower")


async def test_fr2_tower_failed_in_init_kills_and_retries(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(
        tmp_path,
        settings,
        workspace_config,
        [
            {"lines": [init_line("failed")], "sleep": 20},
            {"lines": good_run(*handoff_lines("t", "c"))},
        ],
    )
    h.add_trigger()
    t0 = time.monotonic()
    await h.settle()
    assert time.monotonic() - t0 < 10  # killed at once, not after the 20 s sleep
    assert invocations(tmp_path) == 2 and h.alert_reasons() == []
    assert "tower failed" in h.runs()[0]["start_failure"]


async def test_fr2_hang_before_tower_is_killed_by_the_limit(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    """Given a start that never reaches tower; Then it is killed at the limit and handled as a failed start."""
    h = Harness(
        tmp_path,
        settings,
        workspace_config,
        [{"lines": [init_line("pending")], "sleep": 30}],
        start_retries=0,
        start_no_tower_timeout_seconds=0.5,
    )
    h.add_trigger()
    await h.settle()
    assert h.alert_reasons() == ["start_failed"]
    assert "start_hang" in h.runs()[0]["start_failure"]


async def test_fr2_unknown_init_status_waits_for_first_tower_call(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    lines = [init_line("pending"), *handoff_lines("t1", "c-1"), result_line()]
    h = Harness(tmp_path, settings, workspace_config, [{"lines": lines}])
    h.add_trigger()
    await h.settle()
    assert invocations(tmp_path) == 1 and h.alert_reasons() == []


async def test_fr2_spawn_error_is_a_start_failure(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(
        tmp_path, settings, workspace_config, claude_binary="/no/such/claude", start_retries=1
    )
    h.add_trigger()
    await h.settle()
    assert h.alert_reasons() == ["start_failed"]
    assert "spawn_error" in h.store.one("SELECT facts FROM alerts")["facts"]
    assert h.slots() == 0


async def test_fr2_log_dir_failure_is_a_start_failure_not_a_leaked_slot(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    h = Harness(tmp_path, settings, workspace_config, log_dir=blocker / "logs", start_retries=0)
    h.add_trigger()
    await h.settle()
    assert h.alert_reasons() == ["start_failed"] and h.slots() == 0


# --- FR-5: outcome ---------------------------------------------------------------------


async def test_fr5_exit_zero_without_handoff_or_message_alerts_once_no_retry(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    """COINEX-184: the role posted a plain comment and ended — nobody was told."""
    lines = good_run(
        tool_use_line("t1", "post_comment", work_item_uuid=SUB, comment_html="<p>done</p>"),
        tool_result_line("t1", {"id": "c9"}),
    )
    h = Harness(tmp_path, settings, workspace_config, [{"lines": lines}])
    h.add_trigger()
    await h.settle()
    assert invocations(tmp_path) == 1  # no auto retry
    assert h.alert_reasons() == ["no_handoff"]
    assert "exit_code" in h.store.one("SELECT facts FROM alerts")["facts"]


async def test_fr5_no_output_at_all_after_tower_up_alerts(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config, [{"lines": [init_line()]}])
    h.add_trigger()
    await h.settle()
    assert h.alert_reasons() == ["no_handoff"]


async def test_fr5_message_to_initiator_on_a_sub_issue_counts(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    lines = good_run(
        tool_use_line("t1", "post_review", sub_uuid=SUB, verdict="APPROVED", body_html="x"),
        tool_result_line("t1", {"comment_id": "c-r"}),
    )
    h = Harness(tmp_path, settings, workspace_config, [{"lines": lines}])
    h.add_trigger()
    await h.settle()
    assert h.alert_reasons() == []


async def test_fr5_nonzero_exit_with_handoff_is_abnormal_exit(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(
        tmp_path,
        settings,
        workspace_config,
        [{"lines": good_run(*handoff_lines("t1", "c-1")), "exit": 3}],
    )
    h.add_trigger()
    await h.settle()
    assert h.alert_reasons() == ["abnormal_exit"]


async def test_fr5_nonzero_exit_without_message_is_no_handoff_with_exit_in_facts(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config, [{"lines": [init_line()], "exit": 2}])
    h.add_trigger()
    await h.settle()
    assert h.alert_reasons() == ["no_handoff"]
    assert '"exit_code": 2' in h.store.one("SELECT facts FROM alerts")["facts"]


async def test_fr5_timeout_with_handoff_is_abnormal_exit(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(
        tmp_path,
        settings,
        workspace_config,
        [{"lines": good_run(*handoff_lines("t1", "c-1")), "sleep": 30}],
        session_timeout_seconds=1,
    )
    h.add_trigger()
    await h.settle()
    assert h.alert_reasons() == ["abnormal_exit"]
    assert h.runs()[0]["timed_out"] == 1


async def test_fr5_failed_start_gets_one_comment_not_two(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config, [{"lines": [], "exit": 1}])
    h.add_trigger()
    await h.settle()
    assert h.alert_reasons() == ["start_failed"]  # not also no_handoff


async def test_late_final_tool_result_is_registered_before_the_outcome(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    """The last tool result is buffered in the pipe when the process exits: still seen (reader barrier)."""
    h = Harness(
        tmp_path,
        settings,
        workspace_config,
        [{"lines": [init_line(), *handoff_lines("t1", "c-last")], "exit": 0}],
    )
    h.add_trigger()
    await h.settle()
    assert h.alert_reasons() == []
    assert h.store.one("SELECT state FROM handoffs")["state"] == "posted"


async def test_oversized_line_is_discarded_and_later_handoff_still_registered(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    import plane_conductor.runner as runner_mod

    big = "x" * (200 * 1024)
    lines = [init_line(), big, *handoff_lines("t1", "c-after-big")]
    h = Harness(tmp_path, settings, workspace_config, [{"lines": lines}])
    orig = runner_mod.STREAM_LIMIT
    runner_mod.STREAM_LIMIT = 64 * 1024  # a "40 MiB line" scaled down for the test
    try:
        h.add_trigger()
        await h.settle()
    finally:
        runner_mod.STREAM_LIMIT = orig
    assert h.store.one("SELECT comment_id FROM handoffs")["comment_id"] == "c-after-big"
    assert h.alert_reasons() == []


async def test_unwritable_journal_does_not_stop_the_drain(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig, monkeypatch
) -> None:
    """A failing journal write is logged once and parsing continues (no stalled pipe)."""
    import io

    class Boom(io.RawIOBase):
        def writable(self) -> bool:
            return True

        def write(self, b):  # type: ignore[no-untyped-def]
            raise OSError("disk full")

    h = Harness(
        tmp_path,
        settings,
        workspace_config,
        [{"lines": good_run(*handoff_lines("t1", "c-1"))}],
    )
    real_path = journal_path

    def fake_journal_path(state_dir: Path, run_id: str) -> Path:
        return real_path(state_dir, run_id)

    # make the journal file read-only after creation: open("ab") on a read-only file fails →
    # that is a start failure; instead make writes fail by pointing at /dev/full
    import plane_conductor.runner as runner_mod

    monkeypatch.setattr(runner_mod, "journal_path", lambda *_a: Path("/dev/full"))
    h.add_trigger()
    await h.settle()
    # handoff still registered from the live stream although every journal write failed
    assert h.store.one("SELECT state FROM handoffs")["state"] == "posted"
    assert h.alert_reasons() == []


# --- process group ------------------------------------------------------------------------


async def test_slot_is_held_until_the_whole_group_is_gone(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    """A grandchild outliving the leader keeps the role 'running' (FR-3)."""
    h = Harness(
        tmp_path,
        settings,
        workspace_config,
        [{"lines": good_run(*handoff_lines("t1", "c-1")), "bg": 2}],
    )
    h.add_trigger()
    await h.dispatcher.tick()
    await asyncio.sleep(1.0)  # leader long gone, grandchild alive
    assert h.slots() == 1
    t0 = time.monotonic()
    await h.settle()
    assert h.slots() == 0
    assert time.monotonic() - t0 < 6


async def test_timeout_kills_descendants(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(
        tmp_path,
        settings,
        workspace_config,
        [{"lines": good_run(*handoff_lines("t1", "c-1")), "sleep": 30, "bg": 30}],
        session_timeout_seconds=1,
    )
    h.add_trigger()
    t0 = time.monotonic()
    await h.settle()
    assert time.monotonic() - t0 < 12
    assert h.slots() == 0


# --- announce -------------------------------------------------------------------------------


async def test_announce_posts_and_updates_without_delaying_start(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    ws = workspace_config.model_copy(update={"announce_spawn": True})
    h = Harness(tmp_path, settings, ws, [{"lines": good_run(*handoff_lines("t1", "c-1"))}])
    h.add_trigger()
    await h.settle()
    assert len(h.plane.created) == 1 and "picking up" in h.plane.created[0]["html"]
    assert h.plane.updated and "done" in h.plane.updated[0][1]


async def test_announce_update_says_failed_to_start(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    ws = workspace_config.model_copy(update={"announce_spawn": True})
    h = Harness(tmp_path, settings, ws, [{"lines": []}], start_retries=0)
    h.add_trigger()
    await h.settle()
    assert "failed to start" in h.plane.updated[0][1]


# --- codex-review findings --------------------------------------------------------------------


async def test_grace_expiry_kills_the_agent_group_instead_of_leaking_it(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    """wait_idle(grace) must SIGKILL the running group; cancelling the supervisors first would
    drop the process handles before the kill step and let the agent survive shutdown."""
    import os
    import signal as _signal

    h = Harness(
        tmp_path,
        settings,
        workspace_config,
        [{"lines": good_run(*handoff_lines("t1", "c-1")), "sleep": 60, "bg": 60}],
    )
    h.add_trigger()
    await h.dispatcher.tick()
    for _ in range(100):  # wait for the process to exist
        row = h.store.one("SELECT pid FROM runs")
        if row is not None and row["pid"]:
            break
        await asyncio.sleep(0.05)
    pid = h.store.one("SELECT pid FROM runs")["pid"]
    await h.runner.wait_idle(grace_seconds=0.5)
    time.sleep(0.2)
    with __import__("contextlib").suppress(ProcessLookupError):
        os.killpg(pid, _signal.SIGKILL)  # cleanup if the test is about to fail
        raise AssertionError("agent process group survived wait_idle")


async def test_start_retry_is_not_attempted_during_shutdown(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    """After shutdown began, a failed start must not spawn another attempt (codex-review P2)."""
    h = Harness(tmp_path, settings, workspace_config, [{"lines": []}], start_retries=3)
    real_sleep = h.runner._sleep

    async def stopping_sleep(delay: float) -> None:
        h.runner.stopping = True  # shutdown begins during the backoff
        await real_sleep(delay)

    h.runner._sleep = stopping_sleep
    h.add_trigger()
    await h.settle()
    assert invocations(tmp_path) == 1  # no second attempt after the stop flag


async def test_cancelled_supervisor_still_kills_its_process_group(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    import os
    import signal as _signal

    h = Harness(
        tmp_path,
        settings,
        workspace_config,
        [{"lines": good_run(*handoff_lines("t1", "c-1")), "sleep": 60, "bg": 60}],
    )
    h.add_trigger()
    await h.dispatcher.tick()
    for _ in range(100):
        row = h.store.one("SELECT pid FROM runs")
        if row is not None and row["pid"]:
            break
        await asyncio.sleep(0.05)
    pid = h.store.one("SELECT pid FROM runs")["pid"]
    for t in list(h.runner._tasks):
        t.cancel()
    await asyncio.gather(*list(h.runner._tasks), return_exceptions=True)
    time.sleep(0.2)
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return
    os.killpg(pid, _signal.SIGKILL)
    raise AssertionError("cancelling the supervisor left the agent group running")


async def test_incomplete_registration_keeps_replayed_zero_for_the_next_start(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig, monkeypatch
) -> None:
    """Journal registration that never became clean must be retried at startup (codex-review P2)."""
    import sqlite3

    import plane_conductor.runner as runner_mod
    from plane_conductor.stream import StreamRegistrar

    def boom(self, block):  # type: ignore[no-untyped-def]
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(StreamRegistrar, "_on_tool_result", boom)
    real_replay = runner_mod.replay_journal

    def dirty_replay(store, run_id, ws, jpath):  # type: ignore[no-untyped-def]
        reg = real_replay(store, run_id, ws, jpath)
        reg.dirty = True
        return reg

    monkeypatch.setattr(runner_mod, "replay_journal", dirty_replay)
    h = Harness(
        tmp_path, settings, workspace_config, [{"lines": good_run(*handoff_lines("t1", "c-1"))}]
    )
    h.add_trigger()
    await h.settle()
    assert h.store.one("SELECT replayed FROM runs")["replayed"] == 0


async def test_all_attempts_of_a_group_are_closed_when_the_group_completes(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(
        tmp_path,
        settings,
        workspace_config,
        [{"lines": []}, {"lines": good_run(*handoff_lines("t1", "c-1"))}],
    )
    h.add_trigger()
    await h.settle()
    rows = h.store.all("SELECT attempt, state, outcome_done FROM runs ORDER BY attempt")
    assert [(r["attempt"], r["outcome_done"]) for r in rows] == [(0, 1), (1, 1)]


async def test_failed_attempt_stays_open_until_the_group_completes(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config, [{"lines": []}], start_retries=1)
    seen: list[int] = []
    real_sleep = h.runner._sleep

    async def spying_sleep(delay: float) -> None:
        row = h.store.one("SELECT outcome_done FROM runs WHERE attempt=0")
        seen.append(int(row["outcome_done"]))
        await real_sleep(delay)

    h.runner._sleep = spying_sleep
    h.add_trigger()
    await h.settle()
    assert seen == [0]  # open during the backoff, closed at completion
    assert h.store.one("SELECT outcome_done FROM runs WHERE attempt=0")["outcome_done"] == 1
