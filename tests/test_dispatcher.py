# mypy: disable-error-code="arg-type, dict-item, unused-ignore, attr-defined"
"""Dispatcher: claim / merge / defer / queue (FR-3), durable queue, re-validation, expiry."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.config import Settings
from plane_conductor.exceptions import PlaneAPIError
from tests.helpers import (
    ISSUE,
    RINZLER_M,
    ROOT,
    SARK_M,
    SUB,
    Harness,
    good_run,
    handoff_lines,
    init_line,
    invocations,
    result_line,
)


def _lines(*extra: str) -> list[str]:
    return good_run(*handoff_lines("t@N@", "c-out@N@"), *extra)


async def test_fr3_concurrent_triggers_on_root_and_sub_issue_start_one_process(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    """Two triggers (root + sub-issue) arriving together: one process, the other merged/deferred."""
    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines(), "sleep": 0.3}])
    h.plane.parents[SUB] = ROOT
    h.add_trigger(issue=ROOT, source="R")
    h.add_trigger(issue=SUB, source="R")
    await asyncio.gather(h.dispatcher.tick(), h.dispatcher.tick())
    assert len(h.runs()) == 1
    await h.settle()
    assert invocations(tmp_path) == 1  # same source → merged, no rerun
    assert sorted(h.trigger_states()) == ["done", "merged"]


async def test_fr3_coinex184_same_source_root_trigger_after_sub_is_merged(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    """D runs on the sub-issue for reviewer run R; 9 s later R's trigger on the root merges."""
    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines(), "sleep": 0.5}])
    h.plane.parents[SUB] = ROOT
    h.add_trigger(issue=SUB, source="R")
    await h.dispatcher.tick()
    h.add_trigger(issue=ROOT, source="R")
    await h.dispatcher.tick()
    await h.settle()
    assert invocations(tmp_path) == 1
    assert "merged" in h.trigger_states()


async def test_fr3_same_source_after_the_run_finished_does_not_start_again(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines()}])
    h.plane.parents[SUB] = ROOT
    h.add_trigger(issue=SUB, source="R")
    await h.settle()
    h.add_trigger(issue=ROOT, source="R")  # the effect key is already covered
    await h.settle()
    assert invocations(tmp_path) == 1
    assert h.trigger_states() == ["done", "merged"]


async def test_fr3_handoff_from_another_run_during_work_gives_exactly_one_rerun(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines(), "sleep": 0.5}])
    h.add_trigger(source="R1")
    await h.dispatcher.tick()
    h.add_trigger(source="R2")
    h.add_trigger(source="R3")  # two more from other runs coalesce into one rerun
    await h.dispatcher.tick()
    assert h.trigger_states().count("deferred") == 2
    await h.settle()
    assert invocations(tmp_path) == 2
    assert h.trigger_states() == ["done", "done", "done"]
    assert h.slots() == 0


async def test_fr3_human_trigger_while_running_is_deferred_not_merged(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines(), "sleep": 0.5}])
    h.add_trigger(source="R1")
    await h.dispatcher.tick()
    h.add_trigger(source=None, actor="human")
    await h.settle()
    assert invocations(tmp_path) == 2


async def test_fr3_self_handoff_d_to_d_is_not_merged_and_not_lost(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    """D hands the task to D (next step): the trigger's source is D's own run → exactly one rerun."""
    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines(), "sleep": 0.5}])
    h.add_trigger(source="R-reviewer")
    await h.dispatcher.tick()
    own_run = h.store.one("SELECT run_id FROM slots")["run_id"]
    h.add_trigger(source=own_run)
    await h.settle()
    assert invocations(tmp_path) == 2
    assert h.trigger_states() == ["done", "done"]


async def test_fr3_no_foreign_trigger_means_no_rerun(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines()}])
    h.add_trigger(source="R1")
    await h.settle()
    assert invocations(tmp_path) == 1


async def test_fr3_different_roles_and_different_roots_run_in_parallel(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines(), "sleep": 0.4}])
    h.add_trigger("rinzler", ISSUE)
    h.add_trigger("sark", ISSUE)
    h.add_trigger("rinzler", ROOT)
    await h.dispatcher.tick()
    assert h.slots() == 3
    await h.settle()


async def test_fr3_slot_survives_between_run_and_rerun_so_a_new_trigger_cannot_sneak_in(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines(), "sleep": 0.3}])
    h.add_trigger(source="R1")
    await h.dispatcher.tick()
    h.add_trigger(source="R2")
    await h.dispatcher.tick()
    await asyncio.sleep(0.6)  # run 1 ends, rerun takes over the same slot
    assert h.slots() in (0, 1)
    h.add_trigger(source="R4")
    await h.settle()
    assert len(h.store.all("SELECT 1 FROM slots")) == 0
    # never two concurrent runs of the key: the third trigger was deferred, then reran once
    assert invocations(tmp_path) == 3


async def test_fr3_crash_does_not_leave_the_key_stuck(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    from plane_conductor.recovery import recover

    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines()}])
    h.store.execute(
        "INSERT INTO slots(workspace, nickname, root_id, group_id, run_id, issue_id)"
        " VALUES('testws','rinzler',?, 'g','dead-run',?)",
        (ISSUE, ISSUE),
    )
    h.store.execute(
        "INSERT INTO runs(run_id, group_id, workspace, plane_base_url, project_id, nickname,"
        " root_id, issue_id, state, started_at) VALUES('dead-run','g','testws','https://plane.test',"
        "?, 'rinzler', ?, ?, 'running', 1)",
        (str(workspace_config.project_id), ISSUE, ISSUE),
    )
    recover(h.settings, h.store, {"testws": workspace_config}, proc_root=tmp_path / "noproc")
    assert h.slots() == 0
    h.add_trigger(source="R9")
    await h.settle()
    assert invocations(tmp_path) == 1


# --- capacity / queue -----------------------------------------------------------------------


async def test_full_capacity_queues_durably_and_starts_on_release(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(
        tmp_path,
        settings,
        workspace_config,
        [{"lines": _lines(), "sleep": 0.5}],
        max_concurrent_sessions=1,
    )
    h.add_trigger("rinzler")
    h.add_trigger("sark")
    await h.dispatcher.tick()
    assert sorted(h.trigger_states()) == ["queued", "running"]
    await h.settle()
    assert invocations(tmp_path) == 2
    assert h.alert_reasons() == []


async def test_queue_survives_a_conductor_restart_between_accept_and_start(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    from plane_conductor.dispatcher import Dispatcher
    from plane_conductor.roots import RootCache
    from plane_conductor.runner import Runner
    from plane_conductor.store import Store

    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines()}])
    h.add_trigger(state="queued")  # accepted, then the process died
    h.store.close()
    store2 = Store(h.settings.state_dir / "conductor.db")
    runner2 = Runner(h.settings, store2, h.workspaces)
    d2 = Dispatcher(
        h.settings, store2, h.workspaces, runner2, RootCache(h.workspaces), clock=h.clock
    )
    for _ in range(100):
        await d2.tick()
        if not runner2._tasks:
            break
        await asyncio.sleep(0.03)
    await asyncio.gather(*list(runner2._tasks))
    assert invocations(tmp_path) == 1


async def test_queue_age_gives_one_alert_and_no_start(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(
        tmp_path, settings, workspace_config, [{"lines": _lines()}], max_concurrent_sessions=1
    )
    h.store.execute(
        "INSERT INTO slots(workspace, nickname, root_id, group_id, run_id, issue_id)"
        " VALUES('testws','sark',?, 'g','r',?)",
        (ROOT, ROOT),
    )
    h.add_trigger("rinzler", state="queued")
    h.clock.advance(1000)  # > queue_max_age_seconds (100)
    await h.dispatcher.tick()
    await h.dispatcher.tick()
    assert h.trigger_states() == ["expired"]
    assert h.alert_reasons() == ["queue_timeout"]  # exactly one
    assert invocations(tmp_path) == 0


async def test_queued_trigger_does_not_starve_behind_later_ones(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(
        tmp_path,
        settings,
        workspace_config,
        [{"lines": _lines(), "sleep": 0.3}],
        max_concurrent_sessions=1,
    )
    h.plane.members["99999999-9999-9999-9999-999999999999"] = {"email": "gem@example.io"}
    h.add_trigger("rinzler", ISSUE)
    h.add_trigger("sark", ISSUE)
    h.add_trigger("gem", ISSUE)
    await h.settle()
    order = [r["nickname"] for r in h.runs()]
    assert order == ["rinzler", "sark", "gem"]


# --- resolution (received → new) ---------------------------------------------------------------


async def test_member_is_resolved_to_a_nickname_and_started(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines()}])
    h.store.execute(
        "INSERT INTO triggers(comment_id, member_uuid, workspace, plane_base_url, project_id,"
        " issue_id, kind, state, snapshot, received_at) VALUES('c1',?,?,?,?,?,'webhook','received',?,?)",
        (
            SARK_M,
            "testws",
            workspace_config.plane_base_url,
            str(workspace_config.project_id),
            ISSUE,
            json.dumps({"actor": "human"}),
            h.clock(),
        ),
    )
    await h.settle()
    assert [r["nickname"] for r in h.runs()] == ["sark"]
    assert h.runs()[0]["triggered_by_email"] == "sark@example.io"


async def test_not_allowed_nickname_is_void(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    ws = workspace_config.model_copy(update={"allowed_nicknames": ["sark"]})
    h = Harness(tmp_path, settings, ws, [{"lines": _lines()}])
    h.store.execute(
        "INSERT INTO triggers(comment_id, member_uuid, workspace, plane_base_url, project_id,"
        " issue_id, kind, state, snapshot, received_at) VALUES('c1',?,?,?,?,?,'webhook','received','{}',?)",
        (RINZLER_M, "testws", ws.plane_base_url, str(ws.project_id), ISSUE, h.clock()),
    )
    await h.settle()
    assert h.trigger_states() == ["void"] and invocations(tmp_path) == 0


async def test_transient_plane_error_keeps_the_trigger_and_runs_later(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    """Plane down during the member lookup: the row stays `received`, nothing is lost."""
    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines()}])
    h.store.execute(
        "INSERT INTO triggers(comment_id, member_uuid, workspace, plane_base_url, project_id,"
        " issue_id, kind, state, snapshot, received_at) VALUES('c1',?,?,?,?,?,'webhook','received','{}',?)",
        (
            SARK_M,
            "testws",
            workspace_config.plane_base_url,
            str(workspace_config.project_id),
            ISSUE,
            h.clock(),
        ),
    )
    real = h.plane.get_member

    async def flaky(member_id):  # type: ignore[no-untyped-def]
        flaky.calls += 1
        if flaky.calls == 1:
            raise PlaneAPIError(503, "down")
        return await real(member_id)

    flaky.calls = 0  # type: ignore[attr-defined]
    h.plane.get_member = flaky  # type: ignore[method-assign]
    await h.dispatcher.tick()
    assert h.trigger_states() == ["received"]
    await h.settle()
    assert invocations(tmp_path) == 1


async def test_root_unresolved_never_invents_a_key(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines()}])
    h.plane.get_issue_error = PlaneAPIError(502, "bad gateway")
    h.store.execute(
        "INSERT INTO triggers(comment_id, member_uuid, nickname, workspace, plane_base_url,"
        " project_id, issue_id, kind, state, snapshot, received_at) VALUES"
        " ('c1',NULL,'rinzler','testws',?,?,?,'redeliver','received','{}',?)",
        (workspace_config.plane_base_url, str(workspace_config.project_id), ISSUE, h.clock()),
    )
    await h.dispatcher.tick()
    assert h.trigger_states() == ["received"] and h.slots() == 0
    h.plane.get_issue_error = None
    await h.settle()
    assert invocations(tmp_path) == 1


async def test_sub_issue_and_root_resolve_to_the_same_slot_key(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines(), "sleep": 0.3}])
    h.plane.parents[SUB] = ROOT
    for issue, cid in ((SUB, "c1"), (ROOT, "c2")):
        h.store.execute(
            "INSERT INTO triggers(comment_id, member_uuid, nickname, workspace, plane_base_url,"
            " project_id, issue_id, kind, state, snapshot, received_at) VALUES"
            " (?,NULL,'rinzler','testws',?,?,?,'redeliver','received','{}',?)",
            (
                cid,
                workspace_config.plane_base_url,
                str(workspace_config.project_id),
                issue,
                h.clock(),
            ),
        )
    await h.dispatcher.tick()
    assert len(h.runs()) == 1 and h.slots() == 1
    rows = h.store.all("SELECT root_id FROM triggers")
    assert {r["root_id"] for r in rows} == {ROOT}
    await h.settle()


async def test_source_is_resolved_from_the_comment_id_the_stream_reported(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines()}])
    h.store.execute(
        "INSERT INTO handoffs(run_id, tool, tool_use_id, workspace, plane_base_url, project_id,"
        " target_nickname, comment_id, state, created_at) VALUES"
        " ('RUN-R','request_handoff','tu','testws','x','p','rinzler','c-handoff','posted',1)"
    )
    h.store.execute(
        "INSERT INTO triggers(comment_id, member_uuid, nickname, workspace, plane_base_url,"
        " project_id, issue_id, kind, state, snapshot, received_at) VALUES"
        " ('c-handoff',NULL,'rinzler','testws',?,?,?,'webhook','received','{}',?)",
        (workspace_config.plane_base_url, str(workspace_config.project_id), ISSUE, h.clock()),
    )
    await h.dispatcher.tick()
    assert h.store.one("SELECT source_run_id FROM triggers")["source_run_id"] == "RUN-R"
    await h.settle()


async def test_unregistered_comment_waits_for_the_stream_then_continues_without_source(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    """An agent run is active and the comment is not in `handoffs` yet: wait, then source=None."""
    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines(), "sleep": 1.0}])
    h.add_trigger("sark", ROOT, source="R0")
    await h.dispatcher.tick()  # a run is active
    h.store.execute(
        "INSERT INTO triggers(comment_id, member_uuid, nickname, workspace, plane_base_url,"
        " project_id, issue_id, kind, state, snapshot, received_at) VALUES"
        " ('c-late',NULL,'rinzler','testws',?,?,?,'webhook','received','{}',?)",
        (workspace_config.plane_base_url, str(workspace_config.project_id), ISSUE, h.clock()),
    )
    await h.dispatcher.tick()
    assert "received" in h.trigger_states()  # still waiting for the tool result
    h.clock.advance(11)
    await h.dispatcher.tick()
    row = h.store.one("SELECT state, source_run_id FROM triggers WHERE comment_id='c-late'")
    assert row["state"] in ("new", "running") and row["source_run_id"] is None
    await h.settle()


async def test_late_registration_backfills_the_source_of_a_waiting_trigger(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    from plane_conductor.stream import backfill_provenance

    h = Harness(tmp_path, settings, workspace_config)
    tid = h.add_trigger(comment_id="c-x", state="deferred", source=None)
    with h.store.tx() as c:
        backfill_provenance(c, "c-x", "RUN-S")
    assert (
        h.store.one("SELECT source_run_id FROM triggers WHERE id=?", (tid,))["source_run_id"]
        == "RUN-S"
    )


# --- re-validation ------------------------------------------------------------------------------


async def test_member_removed_while_queued_is_void_not_started(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines()}])
    h.plane.project_members = [{"email": "sark@example.io"}]  # rinzler is gone
    h.add_trigger("rinzler")
    await h.dispatcher.tick()
    assert h.trigger_states() == ["void"] and invocations(tmp_path) == 0


async def test_rate_limited_revalidation_is_retried_next_tick(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines()}])
    h.plane.members_error = PlaneAPIError(429, "slow down", retry_exhausted=True)
    h.add_trigger("rinzler")
    await h.dispatcher.tick()
    assert h.trigger_states() == ["new"]
    h.plane.members_error = None
    h.clock.advance(60)
    await h.settle()
    assert invocations(tmp_path) == 1


async def test_destination_change_voids_the_trigger(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines()}])
    h.add_trigger("rinzler")
    h.store.execute("UPDATE triggers SET project_id='other-project'")
    await h.dispatcher.tick()
    assert h.trigger_states() == ["void"]


async def test_no_shared_mutable_state_between_two_workspaces_keys(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    """Same nickname + root in two workspaces are different keys."""
    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines(), "sleep": 0.3}])
    h.add_trigger("rinzler")
    await h.dispatcher.tick()
    h.store.execute(
        "INSERT INTO slots(workspace, nickname, root_id, group_id, run_id, issue_id)"
        " VALUES('other','rinzler',?, 'g','r',?)",
        (ISSUE, ISSUE),
    )
    assert h.slots() == 2
    await h.settle()
    _ = (init_line, result_line)


# --- auto-resume (initiator reply without a mention) ------------------------------------------


def _initiator_comment(h: Harness, agent_uuid: str) -> dict[str, object]:
    return {
        "actor": agent_uuid,
        "created_at": "2026-11-08T10:00:00Z",
        "comment_html": (
            f'<mention-component entity_identifier="{h.ws.initiator_uuid}" '
            f'entity_name="user_mention"></mention-component> PLAN ready'
        ),
    }


def _auto_resume_row(h: Harness) -> None:
    h.store.execute(
        "INSERT INTO triggers(comment_id, member_uuid, workspace, plane_base_url, project_id,"
        " issue_id, kind, state, snapshot, received_at) VALUES"
        " ('c-reply',NULL,'testws',?,?,?, 'auto_resume','received',?,?)",
        (
            h.ws.plane_base_url,
            str(h.ws.project_id),
            ISSUE,
            json.dumps({"actor": str(h.ws.initiator_uuid), "had_mention": False}),
            h.clock(),
        ),
    )


async def test_auto_resume_respawns_the_agent_waiting_on_the_initiator(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines()}])
    h.plane.issue_comments[ISSUE] = [_initiator_comment(h, SARK_M)]
    _auto_resume_row(h)
    await h.settle()
    assert [r["nickname"] for r in h.runs()] == ["sark"]


async def test_auto_resume_with_no_pending_agent_is_void(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines()}])
    _auto_resume_row(h)
    await h.settle()
    assert h.trigger_states() == ["void"] and invocations(tmp_path) == 0


async def test_auto_resume_for_an_unregistered_agent_is_void(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines()}])
    stranger = "99999999-9999-9999-9999-999999999999"
    h.plane.members[stranger] = {"email": "stranger@example.io"}
    h.plane.issue_comments[ISSUE] = [_initiator_comment(h, stranger)]
    _auto_resume_row(h)
    await h.settle()
    assert h.trigger_states() == ["void"]


async def test_member_removed_while_deferred_is_not_rerun(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    """A rerun is a start like any other: owner/access re-validated first (codex-review P1)."""
    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines(), "sleep": 0.5}])
    h.runner.pre_release = h.dispatcher.prepare_rerun
    h.add_trigger(source="R1")
    await h.dispatcher.tick()
    h.add_trigger(source="R2")  # deferred while the first run works
    await h.dispatcher.tick()
    h.plane.project_members = [{"email": "sark@example.io"}]  # rinzler removed meanwhile
    h.clock.advance(60)  # past the members cache
    await h.settle()
    assert invocations(tmp_path) == 1
    assert h.trigger_states() == ["done", "void"] and h.slots() == 0


async def test_transient_error_while_revalidating_a_rerun_hands_it_back_to_the_dispatcher(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines(), "sleep": 0.5}])
    h.runner.pre_release = h.dispatcher.prepare_rerun
    h.add_trigger(source="R1")
    await h.dispatcher.tick()
    h.add_trigger(source="R2")
    await h.dispatcher.tick()
    h.plane.members_error = PlaneAPIError(503, "down")
    h.clock.advance(60)
    for _ in range(200):
        if not h.runner._tasks:
            break
        await asyncio.sleep(0.03)
    assert h.trigger_states() == ["done", "new"]  # not started, not lost
    h.plane.members_error = None
    await h.settle()
    assert invocations(tmp_path) == 2


async def test_reparented_queued_issue_gets_its_new_root_before_the_slot_is_claimed(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    """The root is refreshed right before a start (codex-review P2): no run under a stale key."""
    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines(), "sleep": 0.4}])
    h.add_trigger("rinzler", ROOT, source="R0")  # the role is busy on ROOT's tree
    await h.dispatcher.tick()
    h.add_trigger("rinzler", SUB, source="R1", root=SUB)  # SUB was a root when it queued
    h.plane.parents[SUB] = ROOT  # ... and has since been re-parented under ROOT
    h.roots._cache.clear()
    await h.dispatcher.tick()  # root refreshed → row re-keyed, not started under the stale key
    assert h.slots() == 1
    await h.dispatcher.tick()
    row = h.store.one("SELECT root_id, state FROM triggers WHERE source_run_id='R1'")
    assert row["root_id"] == ROOT and row["state"] == "deferred"  # same tree → not concurrent
    await h.settle()
    assert [r["root_id"] for r in h.runs()] == [ROOT, ROOT]


async def test_deferred_rerun_re_resolves_the_root_before_taking_the_slot(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    """A rerun is a start like any other: fresh root (re-parenting) and issue still exist."""
    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines(), "sleep": 0.4}])
    h.add_trigger("rinzler", ISSUE, source="R0")
    await h.dispatcher.tick()
    h.add_trigger("rinzler", ISSUE, source="R1")  # deferred behind the running one
    await h.dispatcher.tick()
    h.plane.parents[ISSUE] = ROOT  # the issue is re-parented while its trigger waits
    h.roots._cache.clear()
    await h.settle()
    second = h.store.one("SELECT root_id FROM triggers WHERE source_run_id='R1'")
    assert second["root_id"] == ROOT
    assert [r["root_id"] for r in h.runs()] == [ISSUE, ROOT]  # new key, not the old slot


async def test_deferred_rerun_of_a_deleted_issue_is_void(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    h = Harness(tmp_path, settings, workspace_config, [{"lines": _lines(), "sleep": 0.4}])
    h.add_trigger("rinzler", ISSUE, source="R0")
    await h.dispatcher.tick()
    h.add_trigger("rinzler", ISSUE, source="R1")
    await h.dispatcher.tick()
    h.plane.get_issue_error = PlaneAPIError(404, "gone")
    await h.settle()
    assert invocations(tmp_path) == 1
    assert h.trigger_states() == ["done", "void"]
