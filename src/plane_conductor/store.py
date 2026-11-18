"""Durable conductor state — one SQLite (WAL) file (QSALE-55, ADR-4).

Everything the conductor must not lose across a restart lives here: accepted
webhook triggers, the per-key run slots, run attempts, the tower calls read from
the agent's stream, and the outbox of alert comments. The *unique keys* are the
guarantees (SPEC §2):

- ``slots`` PK ``(workspace, nickname, root_id)`` — the atomic claim that keeps
  two copies of one role off one task tree (FR-3);
- ``triggers`` ``(comment_id, member_uuid)`` / ``(comment_id, nickname)`` —
  redelivered or edited webhooks do not create a second run;
- ``alerts`` ``dedupe_key`` — one cause, one comment (NFR-5).

Follows ``workbench.db`` (one connection under an ``RLock``, ``BEGIN IMMEDIATE``
transactions, ``schema_version`` in ``kv``). Calls are synchronous point queries:
at <= 5 concurrent runs and tens of events a day they take milliseconds, so the
event loop is never parked for a noticeable time.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1"

#: ``triggers.state`` values that never start (or have already finished) a run.
TRIGGER_DEAD_STATES = ("void", "expired")
#: ``triggers.state`` values waiting for the Dispatcher.
TRIGGER_WAITING_STATES = ("new", "deferred", "queued")

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS triggers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  comment_id TEXT NOT NULL,
  member_uuid TEXT,
  nickname TEXT,
  workspace TEXT NOT NULL,
  plane_base_url TEXT NOT NULL,
  project_id TEXT NOT NULL,
  issue_id TEXT NOT NULL,
  root_id TEXT,
  source_run_id TEXT,
  kind TEXT NOT NULL CHECK(kind IN ('webhook','auto_resume','redeliver')),
  state TEXT NOT NULL CHECK(state IN
    ('received','new','merged','deferred','queued','running','done','expired','void')),
  reason TEXT,
  run_id TEXT,
  triggered_by_email TEXT,
  snapshot TEXT NOT NULL DEFAULT '{}',
  received_at REAL NOT NULL,
  enqueued_at REAL,
  finished_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_trig_member ON triggers(comment_id, member_uuid);
CREATE UNIQUE INDEX IF NOT EXISTS ux_trig_nick ON triggers(comment_id, nickname)
  WHERE nickname IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_trig_resume ON triggers(comment_id)
  WHERE kind = 'auto_resume';
CREATE INDEX IF NOT EXISTS ix_trig_wait ON triggers(state, enqueued_at)
  WHERE state IN ('received','new','deferred','queued');
CREATE INDEX IF NOT EXISTS ix_trig_effect ON triggers(source_run_id, nickname, root_id)
  WHERE source_run_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS slots (
  workspace TEXT NOT NULL,
  nickname TEXT NOT NULL,
  root_id TEXT NOT NULL,
  group_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  issue_id TEXT NOT NULL,
  covered_sources TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (workspace, nickname, root_id)
);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  group_id TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 0,
  workspace TEXT NOT NULL,
  plane_base_url TEXT NOT NULL,
  project_id TEXT NOT NULL,
  nickname TEXT NOT NULL,
  root_id TEXT NOT NULL,
  issue_id TEXT NOT NULL,
  triggered_by_email TEXT,
  state TEXT NOT NULL CHECK(state IN ('starting','running','exited','lost')),
  registration_dirty INTEGER NOT NULL DEFAULT 0,
  replayed INTEGER NOT NULL DEFAULT 0,
  pid INTEGER,
  pgid INTEGER,
  started_at REAL NOT NULL,
  tower_seen INTEGER NOT NULL DEFAULT 0,
  exit_code INTEGER,
  timed_out INTEGER NOT NULL DEFAULT 0,
  start_failure TEXT,
  in_doubt INTEGER NOT NULL DEFAULT 0,
  outcome_done INTEGER NOT NULL DEFAULT 0,
  ended_at REAL
);
CREATE INDEX IF NOT EXISTS ix_runs_group ON runs(group_id);
CREATE INDEX IF NOT EXISTS ix_runs_open ON runs(state) WHERE state IN ('starting','running');

CREATE TABLE IF NOT EXISTS handoffs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  tool TEXT NOT NULL,
  tool_use_id TEXT NOT NULL UNIQUE,
  workspace TEXT NOT NULL,
  plane_base_url TEXT NOT NULL,
  project_id TEXT NOT NULL,
  issue_id TEXT,
  root_id TEXT,
  target_nickname TEXT,
  comment_id TEXT,
  state TEXT NOT NULL CHECK(state IN ('called','posted','failed')),
  created_at REAL NOT NULL,
  retries INTEGER NOT NULL DEFAULT 0,
  last_try_at REAL,
  alerted INTEGER NOT NULL DEFAULT 0  -- 0 watched, 1 alerted, 2 settled
);
CREATE INDEX IF NOT EXISTS ix_handoff_watch ON handoffs(state, alerted)
  WHERE state = 'posted' AND target_nickname IS NOT NULL AND alerted = 0;
CREATE INDEX IF NOT EXISTS ix_handoff_comment ON handoffs(comment_id);

CREATE TABLE IF NOT EXISTS alerts (
  dedupe_key TEXT PRIMARY KEY,
  workspace TEXT NOT NULL,
  plane_base_url TEXT NOT NULL,
  project_id TEXT NOT NULL,
  issue_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  facts TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL CHECK(state IN ('new','sending','unknown','sent','dropped')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_try_at REAL NOT NULL,
  comment_id TEXT,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_alert_due ON alerts(state, next_try_at)
  WHERE state IN ('new','unknown');
"""


class Store:
    """Thin synchronous SQLite wrapper: one connection, ``RLock``, WAL."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            parent = Path(self.path).parent
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.conn = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False, timeout=10.0
        )
        self.conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
            with contextlib.suppress(OSError):
                os.chmod(self.path, 0o600)
        self._lock = threading.RLock()
        self._depth = 0
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO kv(key,value) VALUES('schema_version',?)", (SCHEMA_VERSION,)
        )

    def close(self) -> None:
        self.conn.close()

    def __del__(self) -> None:
        # Python 3.13 warns about an unclosed sqlite3 connection; close quietly on GC.
        with contextlib.suppress(Exception):
            self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` transaction; nested use joins the outer one."""
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield self.conn
                finally:
                    self._depth -= 1
                return
            self.conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")
            finally:
                self._depth = 0

    def execute(self, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, args)

    def one(self, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self.conn.execute(sql, args).fetchone()
            return row

    def scalar(self, sql: str, args: tuple[Any, ...] = ()) -> int:
        """First column of the first row as an int (``SELECT count(*) ...``); 0 when no row."""
        row = self.one(sql, args)
        return int(row[0]) if row is not None else 0

    def all(self, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    # -- kv -------------------------------------------------------------------

    def kv_get(self, key: str) -> str | None:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return None if row is None else str(row["value"])

    def kv_set(self, key: str, value: str) -> None:
        self.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (key, value))

    # -- retention ------------------------------------------------------------

    def purge(self, *, now: float, state_retention_s: float, dedupe_retention_s: float) -> int:
        """Delete terminal rows past retention. Non-terminal rows are never touched.

        Terminal triggers are kept ``dedupe_retention`` ("no second run for the same
        comment + addressee for a year"); other terminal rows ``state_retention``.
        """
        n = 0
        with self.tx() as c:
            n += c.execute(
                "DELETE FROM triggers WHERE state IN ('done','merged','void','expired')"
                " AND COALESCE(finished_at, received_at) < ?",
                (now - dedupe_retention_s,),
            ).rowcount
            n += c.execute(
                "DELETE FROM runs WHERE state IN ('exited','lost') AND outcome_done=1"
                " AND COALESCE(ended_at, started_at) < ?",
                (now - state_retention_s,),
            ).rowcount
            n += c.execute(
                "DELETE FROM handoffs WHERE (alerted != 0 OR state != 'posted') AND created_at < ?",
                (now - state_retention_s,),
            ).rowcount
            n += c.execute(
                "DELETE FROM alerts WHERE state IN ('sent','dropped') AND created_at < ?",
                (now - state_retention_s,),
            ).rowcount
        return n


def loads(raw: str | None, default: Any) -> Any:
    """``json.loads`` that never raises."""
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default
