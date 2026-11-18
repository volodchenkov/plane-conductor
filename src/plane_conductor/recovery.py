"""Startup recovery (QSALE-55 SPEC §4.5, ADR-5).

Agents are children of conductor and ``KillMode=mixed`` ends them when it stops, so after a
restart there is normally no live agent. Conductor does **not** adopt survivors; it makes sure
none is left, then repairs the store *before* the Dispatcher starts:

(a) kill every process whose environment carries ``CONDUCTOR_RUN_ID`` of any known run —
    independent of stored pid/pgid (a crash between process creation and persisting the pgid
    is covered) and immune to pid reuse;
(b) only when none is left, delete **every** slot (a slot without a live group is never valid);
(c) runs ``starting|running`` → ``lost``; their triggers → ``done`` (not replayed: no duplicate
    work); one ``conductor_restart`` alert per group;
(d) replay the stream journal of every run not yet replayed through the normal parser — a
    comment that committed just before the crash is still watched (FR-4);
(e) exited runs without an outcome are treated like lost ones;
(f) alerts ``sending`` → ``unknown`` (reconciled before any new POST).

Waiting triggers (``received``/``new``/``deferred``/``queued``) are untouched: the queue and the
accepted handoffs survive a restart.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sqlite3
import time
from pathlib import Path

from plane_conductor import alerts
from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.config import Settings
from plane_conductor.logging_config import get_logger
from plane_conductor.runner import journal_path
from plane_conductor.store import Store
from plane_conductor.stream import replay_journal

log = get_logger(__name__)

ENV_KEY = b"CONDUCTOR_RUN_ID="


def _marked_pids(run_ids: set[str], proc_root: Path = Path("/proc")) -> dict[int, str]:
    """``{pid: run_id}`` of live processes marked with a known ``CONDUCTOR_RUN_ID``."""
    found: dict[int, str] = {}
    me = os.getpid()
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return found
    for name in entries:
        if not name.isdigit() or int(name) == me:
            continue
        try:
            raw = (proc_root / name / "environ").read_bytes()
        except OSError:
            continue
        for item in raw.split(b"\0"):
            if item.startswith(ENV_KEY):
                rid = item[len(ENV_KEY) :].decode("utf-8", "replace")
                if rid in run_ids:
                    found[int(name)] = rid
                break
    return found


def kill_survivors(store: Store, *, proc_root: Path = Path("/proc"), wait: float = 5.0) -> int:
    """Step (a). Returns how many marked processes were found (and killed)."""
    run_ids = {str(r["run_id"]) for r in store.all("SELECT run_id FROM runs")}
    if not run_ids:
        return 0
    total = 0
    deadline = time.monotonic() + wait
    while True:
        pids = _marked_pids(run_ids, proc_root)
        if not pids:
            return total
        total = max(total, len(pids))
        for pid, rid in pids.items():
            log.error("survivor_killed", pid=pid, run_id=rid)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        if time.monotonic() >= deadline:
            log.error("survivors_remain", count=len(pids))
            return total
        time.sleep(0.2)


def recover(
    settings: Settings,
    store: Store,
    workspaces: dict[str, WorkspaceConfig],
    *,
    proc_root: Path = Path("/proc"),
) -> dict[str, int]:
    """Run steps (a)-(f). Synchronous: nothing else runs yet. Returns counters for the log."""
    stats = {"survivors": 0, "lost_runs": 0, "replayed": 0, "alerts_unknown": 0}
    legacy = settings.log_dir / ".active"
    if legacy.is_dir() and any(legacy.glob("*.json")):
        log.warning("legacy_sentinel_ignored", path=str(legacy))

    stats["survivors"] = kill_survivors(store, proc_root=proc_root)
    still = _marked_pids(
        {str(r["run_id"]) for r in store.all("SELECT run_id FROM runs")}, proc_root
    )
    if still:
        # a process we could not kill: keep the slots, the owner has to look (ERROR logged above)
        log.error("recovery_slots_kept_survivors", pids=sorted(still))
        return stats

    now = time.time()
    # (d) first: so the alert facts of (c) know about unfinished tool calls
    pending = store.all(
        "SELECT run_id FROM runs WHERE replayed=0 AND state IN ('starting','running','lost','exited')"
    )
    for r in pending:
        run_id = str(r["run_id"])
        try:
            clean = False
            for attempt in range(3):
                reg = replay_journal(
                    store, run_id, workspaces, journal_path(settings.state_dir, run_id)
                )
                if not reg.dirty:
                    clean = True
                    break
                time.sleep(0.2 * (attempt + 1))
            if clean:
                store.execute("UPDATE runs SET replayed=1 WHERE run_id=?", (run_id,))
                stats["replayed"] += 1
            else:
                # keep replayed=0: the next start retries; a failed registration is not forgotten
                log.error("journal_replay_incomplete", run_id=run_id)
        except sqlite3.Error:
            log.exception("journal_replay_failed", run_id=run_id)

    with store.tx() as c:
        c.execute("DELETE FROM slots")  # (b)
        interrupted = c.execute(
            "SELECT * FROM runs WHERE state IN ('starting','running')"
            " OR (state='exited' AND outcome_done=0)"
        ).fetchall()
        groups_alerted: set[str] = set()
        for run in interrupted:
            in_doubt = int(run["in_doubt"]) + int(
                c.execute(
                    "SELECT count(*) AS n FROM handoffs WHERE run_id=? AND state='called'",
                    (run["run_id"],),
                ).fetchone()["n"]
            )
            c.execute(
                "UPDATE runs SET state='lost', outcome_done=1, ended_at=?, in_doubt=? WHERE run_id=?",
                (now, in_doubt, run["run_id"]),
            )
            c.execute(
                "UPDATE triggers SET state='done', finished_at=? WHERE state='running' AND run_id=?",
                (now, run["run_id"]),
            )
            if run["group_id"] in groups_alerted:
                continue
            groups_alerted.add(str(run["group_id"]))
            alerts.enqueue(
                c,
                reason="conductor_restart",
                subject=str(run["group_id"]),
                workspace=run["workspace"],
                plane_base_url=run["plane_base_url"],
                project_id=run["project_id"],
                issue_id=run["issue_id"],
                facts={
                    "nickname": run["nickname"],
                    "issue": run["issue_id"],
                    "in_doubt_tool_calls": in_doubt or None,
                    "note": "mention the agent again if the work is not finished",
                },
                now=now,
            )
            stats["lost_runs"] += 1
        # triggers 'running' for runs that no longer exist as open runs
        c.execute("UPDATE triggers SET state='done', finished_at=? WHERE state='running'", (now,))
        stats["alerts_unknown"] = c.execute(
            "UPDATE alerts SET state='unknown' WHERE state='sending'"
        ).rowcount  # (f)
    log.info("recovery_done", **stats)
    return stats
