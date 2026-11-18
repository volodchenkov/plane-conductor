# mypy: disable-error-code="arg-type, dict-item, unused-ignore, attr-defined"
"""Stream reader against the captured Claude Code 2.1.317 fixture and edge shapes (SPEC §3.1)."""

from __future__ import annotations

import json
from pathlib import Path

from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.store import Store
from plane_conductor.stream import (
    EV_TOWER_BAD,
    EV_TOWER_CALL,
    EV_TOWER_UP,
    StreamRegistrar,
    replay_journal,
    role_to_nickname,
)
from tests.helpers import SUB, handoff_lines, init_line, tool_result_line, tool_use_line

FIXTURE = Path(__file__).parent / "fixtures" / "claude_stream_2.1.317.ndjson"


def _reg(tmp_path: Path, ws: WorkspaceConfig, run_id: str = "R1") -> tuple[Store, StreamRegistrar]:
    store = Store(tmp_path / "c.db")
    store.execute(
        "INSERT INTO runs(run_id, group_id, workspace, plane_base_url, project_id, nickname,"
        " root_id, issue_id, state, started_at) VALUES(?,?,?,?,?,?,?,?, 'running', 1)",
        (
            run_id,
            "g",
            ws.workspace_slug,
            ws.plane_base_url,
            str(ws.project_id),
            "rinzler",
            "r",
            "i",
        ),
    )
    return store, StreamRegistrar(store, run_id, {ws.workspace_slug: ws})


def test_v1_fixture_replay_registers_tower_up_and_the_handoff(
    tmp_path: Path, workspace_config: WorkspaceConfig
) -> None:
    """V-1: the committed 2.1.317 fixture still parses into tower_up + a posted handoff."""
    store, reg = _reg(tmp_path, workspace_config)
    events = [e for line in FIXTURE.read_text().splitlines() for e in reg.feed(line)]
    kinds = [k for k, _ in events]
    assert EV_TOWER_UP in kinds and EV_TOWER_CALL in kinds and EV_TOWER_BAD not in kinds
    row = store.one("SELECT * FROM handoffs")
    assert row["state"] == "posted" and row["comment_id"] == "c-fixture-1"
    assert row["target_nickname"] is None  # initiator
    assert reg.result is not None and reg.tower_seen


def test_init_statuses(tmp_path: Path, workspace_config: WorkspaceConfig) -> None:
    for status, expected in [
        ("connected", EV_TOWER_UP),
        ("failed", EV_TOWER_BAD),
        ("needs-auth", EV_TOWER_BAD),
        ("disconnected", EV_TOWER_BAD),
    ]:
        _, reg = _reg(tmp_path / status, workspace_config)
        assert [k for k, _ in reg.feed(init_line(status))] == [expected]


def test_unknown_init_status_waits_and_missing_tower_is_bad(
    tmp_path: Path, workspace_config: WorkspaceConfig
) -> None:
    _, reg = _reg(tmp_path / "a", workspace_config)
    assert reg.feed(init_line("pending")) == [] and not reg.tower_seen
    _, reg2 = _reg(tmp_path / "b", workspace_config)
    assert [k for k, _ in reg2.feed(init_line(None))] == [EV_TOWER_BAD]
    _, reg3 = _reg(tmp_path / "c", workspace_config)  # older CLI without mcp_servers
    assert reg3.feed(json.dumps({"type": "system", "subtype": "init"})) == []


def test_agent_handoff_resolves_role_to_nickname(
    tmp_path: Path, workspace_config: WorkspaceConfig
) -> None:
    store, reg = _reg(tmp_path, workspace_config)
    for line in handoff_lines("t1", "c1", role="system-analyst"):
        reg.feed(line)
    assert store.one("SELECT target_nickname FROM handoffs")["target_nickname"] == "sark"
    assert role_to_nickname(workspace_config, "sdlc-agents:system-analyst") == "sark"


def test_unknown_role_registers_nothing(tmp_path: Path, workspace_config: WorkspaceConfig) -> None:
    store, reg = _reg(tmp_path, workspace_config)
    for line in handoff_lines("t1", "c1", role="no-such-role"):
        reg.feed(line)
    assert store.all("SELECT * FROM handoffs") == []


def test_post_review_with_next_role_is_message_plus_handoff(
    tmp_path: Path, workspace_config: WorkspaceConfig
) -> None:
    store, reg = _reg(tmp_path, workspace_config)
    reg.feed(tool_use_line("t1", "post_review", sub_uuid=SUB, next_role="ui-tester"))
    reg.feed(tool_result_line("t1", {"comment_id": "c1"}))
    row = store.one("SELECT * FROM handoffs")
    assert row["state"] == "posted" and row["target_nickname"] == "gem"


def test_escalate_without_role_is_message_to_initiator_only(
    tmp_path: Path, workspace_config: WorkspaceConfig
) -> None:
    store, reg = _reg(tmp_path, workspace_config)
    reg.feed(tool_use_line("t1", "escalate_upstream_gap", my_sub_uuid=SUB))
    reg.feed(tool_result_line("t1", {"comment_id": "c1"}))
    row = store.one("SELECT * FROM handoffs")
    assert row["state"] == "posted" and row["target_nickname"] is None


def test_workspace_override_must_be_configured(
    tmp_path: Path, workspace_config: WorkspaceConfig
) -> None:
    store, reg = _reg(tmp_path, workspace_config)
    reg.feed(
        tool_use_line(
            "t1", "request_handoff", sub_uuid=SUB, target_role="reviewer", workspace="ghost"
        )
    )
    assert store.all("SELECT * FROM handoffs") == []


def test_error_result_is_failed_and_outcome_unknown_counts_in_doubt(
    tmp_path: Path, workspace_config: WorkspaceConfig
) -> None:
    store, reg = _reg(tmp_path, workspace_config)
    reg.feed(tool_use_line("t1", "request_handoff", sub_uuid=SUB, target_role="initiator"))
    reg.feed(tool_result_line("t1", "plane_error: ... outcome_unknown: may exist", is_error=True))
    reg.feed(tool_use_line("t2", "request_handoff", sub_uuid=SUB, target_role="initiator"))
    reg.feed(tool_result_line("t2", "plane_error: status=400", is_error=True))
    assert [r["state"] for r in store.all("SELECT state FROM handoffs ORDER BY id")] == [
        "failed",
        "failed",
    ]
    assert store.one("SELECT in_doubt FROM runs")["in_doubt"] == 1


def test_call_without_result_stays_called_in_doubt(
    tmp_path: Path, workspace_config: WorkspaceConfig
) -> None:
    store, reg = _reg(tmp_path, workspace_config)
    reg.feed(tool_use_line("t1", "request_handoff", sub_uuid=SUB, target_role="initiator"))
    assert store.one("SELECT state FROM handoffs")["state"] == "called"


def test_agent_retry_of_the_same_call_is_one_row(
    tmp_path: Path, workspace_config: WorkspaceConfig
) -> None:
    store, reg = _reg(tmp_path, workspace_config)
    for _ in range(2):
        for line in handoff_lines("t1", "c1"):
            reg.feed(line)
    assert len(store.all("SELECT * FROM handoffs")) == 1


def test_non_tower_tools_and_garbage_are_ignored(
    tmp_path: Path, workspace_config: WorkspaceConfig
) -> None:
    _, reg = _reg(tmp_path, workspace_config)
    assert reg.feed(b"not json at all") == []
    assert reg.feed("[1,2]") == []
    assert reg.feed(json.dumps({"type": "assistant", "message": {"content": "str"}})) == []
    line = json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": "x", "name": "Bash", "input": {}}]},
        }
    )
    assert reg.feed(line) == [] and not reg.tower_seen


def test_store_failure_marks_dirty_and_journal_replay_recovers(
    tmp_path: Path, workspace_config: WorkspaceConfig
) -> None:
    store, reg = _reg(tmp_path, workspace_config)
    journal = tmp_path / "j.ndjson"
    lines = [init_line(), *handoff_lines("t1", "c1")]
    journal.write_text("\n".join(lines) + "\n")
    store.conn.execute("PRAGMA query_only=ON")
    for line in lines:
        reg.feed(line)
    assert reg.dirty
    store.conn.execute("PRAGMA query_only=OFF")
    replay = replay_journal(
        store, "R1", {workspace_config.workspace_slug: workspace_config}, journal
    )
    assert not replay.dirty
    assert store.one("SELECT state FROM handoffs")["state"] == "posted"
    # replay twice = still one row (idempotent by tool_use_id)
    replay_journal(store, "R1", {workspace_config.workspace_slug: workspace_config}, journal)
    assert len(store.all("SELECT * FROM handoffs")) == 1


def test_replay_of_missing_journal_is_a_noop(
    tmp_path: Path, workspace_config: WorkspaceConfig
) -> None:
    store, _ = _reg(tmp_path, workspace_config)
    r = replay_journal(store, "R1", {}, tmp_path / "nope.ndjson")
    assert not r.dirty
