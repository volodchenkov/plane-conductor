# mypy: disable-error-code="arg-type, dict-item, unused-ignore, attr-defined"
"""Handoff delivery watcher (FR-4) and the alert outbox (FR-2/4/5, NFR-5)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from plane_conductor import alerts
from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.config import Settings
from plane_conductor.exceptions import PlaneAPIError
from tests.helpers import (
    ISSUE,
    ROOT,
    SUB,
    Harness,
    good_run,
    handoff_lines,
    invocations,
    tool_use_line,
)


def _handoff(
    h: Harness,
    target: str | None = "rinzler",
    *,
    comment: str = "c-h1",
    run: str = "R-rev",
    issue: str = SUB,
    created_ago: float = 0.0,
    state: str = "posted",
) -> int:
    cur = h.store.execute(
        "INSERT INTO handoffs(run_id, tool, tool_use_id, workspace, plane_base_url, project_id,"
        " issue_id, target_nickname, comment_id, state, created_at)"
        " VALUES(?, 'request_handoff', ?, 'testws', ?, ?, ?, ?, ?, ?, ?)",
        (
            run,
            f"tu-{comment}",
            h.ws.plane_base_url,
            str(h.ws.project_id),
            issue,
            target,
            comment,
            state,
            h.clock() - created_ago,
        ),
    )
    return int(cur.lastrowid or 0)


def _trigger_for(
    h: Harness, comment: str, state: str, nick: str = "rinzler", source: str | None = None
) -> None:
    tid = h.add_trigger(nick, comment_id=comment, state=state, source=source)
    h.store.execute("UPDATE triggers SET state=? WHERE id=?", (state, tid))


async def test_fr4_lost_webhook_is_redelivered_after_the_ack_window_and_runs(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(
        tmp_path, settings, workspace_config, [{"lines": good_run(*handoff_lines("t@N@", "c@N@"))}]
    )
    h.plane.parents[SUB] = ROOT
    _handoff(h, created_ago=0)
    await h.watcher.check_handoffs()
    assert h.store.all("SELECT * FROM triggers") == []  # inside the window: nothing yet
    h.clock.advance(301)
    await h.watcher.check_handoffs()
    (t,) = h.store.all("SELECT * FROM triggers")
    assert t["kind"] == "redeliver" and t["nickname"] == "rinzler" and t["source_run_id"] == "R-rev"
    await h.settle()
    assert invocations(tmp_path) == 1
    assert h.store.one("SELECT retries FROM handoffs")["retries"] == 1


async def test_fr4_settled_handoff_is_never_redelivered(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config)
    _handoff(h, created_ago=1000)
    _trigger_for(h, "c-h1", "running")
    await h.watcher.check_handoffs()
    assert len(h.store.all("SELECT * FROM triggers")) == 1 and h.alert_reasons() == []
    assert h.store.one("SELECT alerted FROM handoffs")["alerted"] == 2


async def test_fr4_handoff_to_initiator_is_not_monitored(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config)
    _handoff(h, target=None, created_ago=100000)
    await h.watcher.check_handoffs()
    assert h.store.all("SELECT * FROM triggers") == [] and h.alert_reasons() == []


async def test_fr4_queued_or_deferred_trigger_is_not_an_alarm(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config)
    _handoff(h, comment="c-q", created_ago=5000)
    _handoff(h, comment="c-d", created_ago=5000)
    _trigger_for(h, "c-q", "queued")
    _trigger_for(h, "c-d", "deferred")
    await h.watcher.check_handoffs()
    assert h.alert_reasons() == []


async def test_fr4_covered_by_a_trigger_from_the_same_run_on_the_root_is_settled(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    """COINEX-184 shape: the handoff on the sub-issue was folded into the run on the root."""
    h = Harness(tmp_path, settings, workspace_config)
    h.plane.parents[SUB] = ROOT
    _handoff(h, comment="c-sub", created_ago=1000)
    _trigger_for(h, "c-root", "merged", source="R-rev")
    h.store.execute("UPDATE triggers SET root_id=?", (ROOT,))
    await h.watcher.check_handoffs()
    assert (
        h.alert_reasons() == []
        and h.store.all("SELECT * FROM triggers WHERE kind='redeliver'") == []
    )


async def test_fr4_exhausted_retries_end_in_one_alert(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config)
    hid = _handoff(h, created_ago=10000)
    h.store.execute(
        "UPDATE handoffs SET retries=3, last_try_at=? WHERE id=?", (h.clock() - 1000, hid)
    )
    await h.watcher.check_handoffs()
    await h.watcher.check_handoffs()
    assert h.alert_reasons() == ["handoff_undelivered"]
    assert h.store.one("SELECT alerted FROM handoffs")["alerted"] == 1


async def test_fr4_voided_trigger_alerts_instead_of_retrying(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config)
    _handoff(h, created_ago=1000)
    _trigger_for(h, "c-h1", "void")
    await h.watcher.check_handoffs()
    assert h.alert_reasons() == ["handoff_undelivered"]


async def test_fr4_escalation_handoff_is_watched_like_any_handoff(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config)
    h.plane.parents[SUB] = ROOT
    cur = h.store.execute(
        "INSERT INTO handoffs(run_id, tool, tool_use_id, workspace, plane_base_url, project_id,"
        " issue_id, target_nickname, comment_id, state, created_at)"
        " VALUES('R', 'escalate_upstream_gap','tu', 'testws', ?, ?, ?, 'sark', 'c-esc', 'posted', ?)",
        (h.ws.plane_base_url, str(h.ws.project_id), SUB, h.clock() - 1000),
    )
    assert cur.lastrowid
    await h.watcher.check_handoffs()
    (t,) = h.store.all("SELECT nickname, kind FROM triggers")
    assert (t["nickname"], t["kind"]) == ("sark", "redeliver")


async def test_fr4_handoff_whose_agent_already_ran_is_not_redelivered(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config)
    _handoff(h, created_ago=1000)
    _trigger_for(h, "c-h1", "done")
    await h.watcher.check_handoffs()
    assert h.store.all("SELECT * FROM triggers WHERE kind='redeliver'") == []


async def test_fr4_unrelated_later_run_of_the_same_role_does_not_settle(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config)
    h.plane.parents[SUB] = ROOT
    _handoff(h, created_ago=1000)
    _trigger_for(h, "c-other", "done", source="R-other")  # a different comment and source
    await h.watcher.check_handoffs()
    assert len(h.store.all("SELECT * FROM triggers WHERE kind='redeliver'")) == 1


async def test_fr4_lost_response_and_lost_webhook_and_no_retry_ends_in_a_visible_alert(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    """tool_use without a tool_result, nobody told: alert lists the in-doubt call."""
    lines = [
        *good_run()[:1],
        tool_use_line("t1", "request_handoff", sub_uuid=SUB, target_role="initiator"),
    ]
    h = Harness(tmp_path, settings, workspace_config, [{"lines": lines}])
    h.add_trigger()
    await h.settle()
    assert h.alert_reasons() == ["no_handoff"]
    assert '"in_doubt_tool_calls": 1' in h.store.one("SELECT facts FROM alerts")["facts"]


# --- outbox ---------------------------------------------------------------------------------


def _alert(h: Harness, reason: str = "no_handoff", subject: str = "g1") -> str:
    with h.store.tx() as c:
        return alerts.enqueue(
            c,
            reason=reason,
            subject=subject,
            workspace="testws",
            plane_base_url=h.ws.plane_base_url,
            project_id=str(h.ws.project_id),
            issue_id=ISSUE,
            facts={"nickname": "rinzler", "issue": ISSUE, "exit_code": 0},
            now=h.clock(),
        )


def _state(h: Harness, key: str) -> Any:
    return h.store.one("SELECT * FROM alerts WHERE dedupe_key=?", (key,))


async def test_alert_is_posted_once_with_mention_marker_and_no_secret(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config)
    key = _alert(h)
    await h.watcher.publish_alerts()
    await h.watcher.publish_alerts()
    (c,) = h.plane.created
    html = c["html"]
    assert html.startswith(f'<p><mention-component entity_identifier="{h.ws.initiator_uuid}"')
    assert f"conductor-alert:{key}" in html and "@rinzler" in html
    assert h.ws.plane_api_key.get_secret_value() not in html
    assert h.ws.webhook_secret.get_secret_value() not in html
    assert c["external_id"] == key and _state(h, key)["state"] == "sent"


async def test_same_reason_same_subject_is_one_alert(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config)
    assert _alert(h) == _alert(h)
    assert len(h.store.all("SELECT * FROM alerts")) == 1


async def test_plane_down_retries_with_backoff_and_logs_error(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config)
    key = _alert(h)
    h.plane.create_failures = [PlaneAPIError(503, "down", method="POST")]
    await h.watcher.publish_alerts()
    row = _state(h, key)
    assert row["state"] == "unknown" and row["attempts"] == 1 and row["next_try_at"] > h.clock()
    await h.watcher.publish_alerts()  # not due yet
    assert h.plane.created == []
    h.clock.advance(31)
    await h.watcher.publish_alerts()
    assert _state(h, key)["state"] == "sent" and len(h.plane.created) == 1


async def test_delayed_commit_then_retry_yields_one_comment(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    """Plane committed the alert but the response was lost: reconcile / external_id → still one."""
    h = Harness(tmp_path, settings, workspace_config)
    key = _alert(h)
    h.plane.lose_response_once = True
    await h.watcher.publish_alerts()
    assert _state(h, key)["state"] == "unknown" and len(h.plane.created) == 1
    h.clock.advance(31)
    await h.watcher.publish_alerts()
    assert _state(h, key)["state"] == "sent"
    assert len(h.plane.created) == 1  # reconciled by the marker, no second POST


async def test_409_on_repeat_post_counts_as_sent(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config)
    key = _alert(h)
    h.plane.seen_external_ids[key] = "old"
    await h.watcher.publish_alerts()
    assert _state(h, key)["state"] == "sent" and h.plane.created == []


async def test_revoked_access_or_deleted_issue_drops_the_alert(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config)
    key = _alert(h)
    h.plane.create_failures = [PlaneAPIError(404, "gone", method="POST")]
    await h.watcher.publish_alerts()
    assert _state(h, key)["state"] == "dropped"


async def test_reconcile_read_403_drops(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config)
    key = _alert(h)
    h.store.execute("UPDATE alerts SET state='unknown' WHERE dedupe_key=?", (key,))

    async def forbidden(*_a: Any, **_k: Any) -> Any:
        raise PlaneAPIError(403, "no access")

    h.plane.list_issue_comments_all = forbidden
    await h.watcher.publish_alerts()
    assert _state(h, key)["state"] == "dropped"


async def test_client_error_retries_as_new_and_gives_up_after_max_attempts(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config, alert_max_attempts=2)
    key = _alert(h)
    h.plane.create_failures = [PlaneAPIError(400, "bad"), PlaneAPIError(400, "bad")]
    await h.watcher.publish_alerts()
    assert _state(h, key)["state"] == "new" and _state(h, key)["attempts"] == 1
    h.clock.advance(100)
    await h.watcher.publish_alerts()
    assert _state(h, key)["state"] == "dropped"


async def test_alert_is_published_to_the_stored_issue_even_if_workspace_destination_changed_is_dropped(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config)
    key = _alert(h)
    h.store.execute("UPDATE alerts SET project_id='moved' WHERE dedupe_key=?", (key,))
    await h.watcher.publish_alerts()
    assert _state(h, key)["state"] == "dropped" and h.plane.created == []


async def test_rendered_facts_are_escaped(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    html = alerts.render(
        reason="start_failed",
        facts={"nickname": "x", "cause": "<script>alert(1)</script>"},
        key="k",
        initiator_uuid=workspace_config.initiator_uuid,
    )
    assert "<script>" not in html and "&lt;script&gt;" in html
    _ = json


async def test_purge_never_touches_waiting_triggers(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config)
    h.add_trigger(state="queued")
    h.clock.advance(10 * 365 * 86400)
    h.watcher._last_purge = 0
    await h.watcher.tick()
    assert h.trigger_states() == ["queued"]


async def test_pending_trigger_does_not_settle_the_handoff_for_good(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    """A trigger that is still `received` may later be voided (access revoked): the handoff must
    then be alerted, not silently forgotten (codex-review P2)."""
    h = Harness(tmp_path, settings, workspace_config)
    _handoff(h, created_ago=1000)
    tid = h.add_trigger(comment_id="c-h1", state="received")
    await h.watcher.check_handoffs()
    assert h.alert_reasons() == [] and h.store.one("SELECT alerted FROM handoffs")["alerted"] == 0
    h.store.execute("UPDATE triggers SET state='void', reason='member_removed' WHERE id=?", (tid,))
    await h.watcher.check_handoffs()
    assert h.alert_reasons() == ["handoff_undelivered"]


async def test_pending_effect_key_twin_does_not_settle_for_good(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config)
    h.plane.parents[SUB] = ROOT
    _handoff(h, comment="c-sub", created_ago=1000)
    tid = h.add_trigger(comment_id="c-root", state="queued", source="R-rev", root=ROOT)
    await h.watcher.check_handoffs()
    assert h.store.one("SELECT alerted FROM handoffs")["alerted"] == 0
    h.store.execute("UPDATE triggers SET state='void' WHERE id=?", (tid,))
    await h.watcher.check_handoffs()
    assert h.alert_reasons() == ["handoff_undelivered"]  # alerted, not forgotten
