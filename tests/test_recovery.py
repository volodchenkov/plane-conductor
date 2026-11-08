# mypy: disable-error-code="arg-type, dict-item, unused-ignore, attr-defined"
"""Startup recovery (SPEC §4.5): survivors killed, slots cleared, journals replayed, alerts."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.config import Settings
from plane_conductor.recovery import recover
from plane_conductor.runner import journal_path
from tests.helpers import SUB, Harness, handoff_lines, init_line, tool_use_line


def _run(h: Harness, run_id: str, state: str = "running", **kw: object) -> None:
    h.store.execute(
        "INSERT INTO runs(run_id, group_id, workspace, plane_base_url, project_id, nickname,"
        " root_id, issue_id, state, started_at) VALUES(?,?,?,?,?,?,?,?,?, 1)",
        (
            run_id,
            f"g-{run_id}",
            "testws",
            h.ws.plane_base_url,
            str(h.ws.project_id),
            "rinzler",
            "root-1",
            "issue-1",
            state,
        ),
    )
    h.store.execute(
        "INSERT INTO slots(workspace, nickname, root_id, group_id, run_id, issue_id)"
        " VALUES('testws','rinzler','root-1',?,?, 'issue-1')",
        (f"g-{run_id}", run_id),
    )


def test_crash_between_claim_and_spawn_frees_the_key_and_alerts_once(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config)
    _run(h, "R1", "starting")
    recover(h.settings, h.store, {"testws": workspace_config}, proc_root=tmp_path / "noproc")
    assert h.slots() == 0
    assert h.store.one("SELECT state FROM runs")["state"] == "lost"
    assert h.alert_reasons() == ["conductor_restart"]
    # a second start finds nothing to repair, no second alert
    recover(h.settings, h.store, {"testws": workspace_config}, proc_root=tmp_path / "noproc")
    assert h.alert_reasons() == ["conductor_restart"]


def test_running_trigger_of_a_lost_run_is_done_not_replayed(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config)
    _run(h, "R1")
    tid = h.add_trigger(state="running")
    h.store.execute("UPDATE triggers SET run_id='R1' WHERE id=?", (tid,))
    waiting = h.add_trigger("sark", state="queued")
    recover(h.settings, h.store, {"testws": workspace_config}, proc_root=tmp_path / "noproc")
    states = {r["id"]: r["state"] for r in h.store.all("SELECT id, state FROM triggers")}
    assert states[tid] == "done" and states[waiting] == "queued"  # the queue survives


def test_journal_replay_registers_a_comment_committed_before_the_crash(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    """Comment committed, registration lost, webhook lost: the journal still shows it (FR-4)."""
    h = Harness(tmp_path, settings, workspace_config)
    _run(h, "R1")
    j = journal_path(h.settings.state_dir, "R1")
    j.parent.mkdir(parents=True)
    j.write_text(
        "\n".join(
            [
                init_line(),
                *handoff_lines("t1", "c-committed", role="system-analyst"),
                tool_use_line("t2", "request_handoff", sub_uuid=SUB, target_role="initiator"),
            ]
        )
        + "\n"
    )
    recover(h.settings, h.store, {"testws": workspace_config}, proc_root=tmp_path / "noproc")
    row = h.store.one("SELECT * FROM handoffs WHERE tool_use_id='t1'")
    assert row["state"] == "posted" and row["target_nickname"] == "sark"
    # the call without a result stays in doubt and is counted in the alert
    assert '"in_doubt_tool_calls": 1' in h.store.one("SELECT facts FROM alerts")["facts"]


def test_sending_alert_becomes_unknown(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config)
    h.store.execute(
        "INSERT INTO alerts(dedupe_key, workspace, plane_base_url, project_id, issue_id, reason,"
        " state, next_try_at, created_at) VALUES('k','testws','x','p','i','no_handoff','sending',0,0)"
    )
    recover(h.settings, h.store, {"testws": workspace_config}, proc_root=tmp_path / "noproc")
    assert h.store.one("SELECT state FROM alerts WHERE dedupe_key='k'")["state"] == "unknown"


def test_legacy_sentinels_are_ignored_and_not_published(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config)
    legacy = h.settings.log_dir / ".active"
    legacy.mkdir(parents=True)
    (legacy / "testws-rinzler-x.json").write_text("{}")
    recover(h.settings, h.store, {"testws": workspace_config}, proc_root=tmp_path / "noproc")
    assert (legacy / "testws-rinzler-x.json").exists()  # untouched
    assert h.alert_reasons() == [] and h.plane.created == []


def test_surviving_child_of_the_previous_conductor_is_killed_on_start(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    """V-3 behaviour: marked by CONDUCTOR_RUN_ID, found via /proc, killed before slots go."""
    h = Harness(tmp_path, settings, workspace_config)
    _run(h, "RUN-ALIVE")
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env={**os.environ, "CONDUCTOR_RUN_ID": "RUN-ALIVE"},
        start_new_session=True,
    )
    try:
        recover(h.settings, h.store, {"testws": workspace_config})
        deadline = time.monotonic() + 5
        while child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert child.poll() is not None, "survivor still alive"
        assert h.slots() == 0
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


def test_unmarked_process_is_not_touched(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config)
    _run(h, "RUN-X")
    other = subprocess.Popen(
        ["sleep", "5"],
        env={**os.environ, "CONDUCTOR_RUN_ID": "UNKNOWN-RUN"},
        start_new_session=True,
    )
    try:
        recover(h.settings, h.store, {"testws": workspace_config})
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


def test_failed_journal_replay_is_not_marked_replayed_and_is_retried_next_start(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig, monkeypatch
) -> None:
    """A replay that hit a store error must leave `replayed=0` (codex-review P2)."""
    import plane_conductor.recovery as rec
    from plane_conductor.stream import StreamRegistrar

    h = Harness(tmp_path, settings, workspace_config)
    _run(h, "R1")
    j = journal_path(h.settings.state_dir, "R1")
    j.parent.mkdir(parents=True)
    j.write_text("\n".join([init_line(), *handoff_lines("t1", "c1")]) + "\n")

    real = rec.replay_journal

    def dirty(store, run_id, workspaces, journal):  # type: ignore[no-untyped-def]
        reg = StreamRegistrar(store, run_id, workspaces)
        reg.dirty = True
        return reg

    monkeypatch.setattr(rec, "replay_journal", dirty)
    recover(h.settings, h.store, {"testws": workspace_config}, proc_root=tmp_path / "noproc")
    assert h.store.one("SELECT replayed FROM runs")["replayed"] == 0

    monkeypatch.setattr(rec, "replay_journal", real)
    recover(h.settings, h.store, {"testws": workspace_config}, proc_root=tmp_path / "noproc")
    assert h.store.one("SELECT replayed FROM runs")["replayed"] == 1
    assert h.store.one("SELECT state FROM handoffs")["state"] == "posted"


def test_crash_during_start_retry_backoff_alerts_instead_of_losing_the_work(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    """The failed attempt is closed but the group is not: a crash in the backoff must not make
    the trigger silently `done` (codex-review P2)."""
    h = Harness(tmp_path, settings, workspace_config)
    _run(h, "R1", "exited")
    h.store.execute(
        "UPDATE runs SET outcome_done=0, start_failure='exit_without_tower' WHERE run_id='R1'"
    )
    tid = h.add_trigger(state="running")
    h.store.execute("UPDATE triggers SET run_id='R1' WHERE id=?", (tid,))
    recover(h.settings, h.store, {"testws": workspace_config}, proc_root=tmp_path / "noproc")
    assert h.alert_reasons() == ["conductor_restart"]
