"""Store: unique keys are the guarantees (QSALE-55 SPEC §2)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from plane_conductor.store import Store

_TRIG = (
    "INSERT INTO triggers(comment_id, member_uuid, nickname, workspace, plane_base_url,"
    " project_id, issue_id, kind, state, received_at) VALUES(?,?,?,?,?,?,?,?,?,?)"
)


def _trig(
    s: Store, comment: str, member: str | None, nick: str | None = None, kind: str = "webhook"
):
    s.execute(
        _TRIG, (comment, member, nick, "ws", "https://p", "proj", "iss", kind, "received", 1.0)
    )


def test_schema_version_and_wal(tmp_path: Path) -> None:
    s = Store(tmp_path / "state" / "conductor.db")
    assert s.kv_get("schema_version") == "1"
    assert s.one("PRAGMA journal_mode")[0] == "wal"
    assert (tmp_path / "state" / "conductor.db").stat().st_mode & 0o777 == 0o600


def test_redelivered_comment_same_member_is_rejected(tmp_path: Path) -> None:
    s = Store(tmp_path / "c.db")
    _trig(s, "c1", "m1")
    with pytest.raises(sqlite3.IntegrityError):
        _trig(s, "c1", "m1")
    _trig(s, "c1", "m2")  # edit that adds an addressee is a new row


def test_watcher_redelivery_collides_with_webhook_on_comment_nickname(tmp_path: Path) -> None:
    s = Store(tmp_path / "c.db")
    _trig(s, "c1", "m1", "sark")
    with pytest.raises(sqlite3.IntegrityError):
        _trig(s, "c1", None, "sark", "redeliver")


def test_auto_resume_unique_per_comment(tmp_path: Path) -> None:
    s = Store(tmp_path / "c.db")
    _trig(s, "c1", None, None, "auto_resume")
    with pytest.raises(sqlite3.IntegrityError):
        _trig(s, "c1", None, None, "auto_resume")


def test_slot_pk_is_the_atomic_claim(tmp_path: Path) -> None:
    s = Store(tmp_path / "c.db")
    ins = (
        "INSERT INTO slots(workspace,nickname,root_id,group_id,run_id,issue_id) VALUES(?,?,?,?,?,?)"
    )
    s.execute(ins, ("ws", "sark", "root", "g1", "r1", "i1"))
    with pytest.raises(sqlite3.IntegrityError):
        s.execute(ins, ("ws", "sark", "root", "g2", "r2", "i2"))


def test_tx_rolls_back_on_error(tmp_path: Path) -> None:
    s = Store(tmp_path / "c.db")
    with pytest.raises(RuntimeError), s.tx():
        _trig(s, "c1", "m1")
        raise RuntimeError
    assert s.one("SELECT count(*) AS n FROM triggers")["n"] == 0


def test_purge_keeps_non_terminal_and_fresh_rows(tmp_path: Path) -> None:
    s = Store(tmp_path / "c.db")
    _trig(s, "old-done", "m1")
    s.execute("UPDATE triggers SET state='done', finished_at=1.0 WHERE comment_id='old-done'")
    _trig(s, "old-queued", "m1")
    s.execute("UPDATE triggers SET state='queued' WHERE comment_id='old-queued'")
    n = s.purge(now=1000.0, state_retention_s=10, dedupe_retention_s=100)
    assert n == 1
    assert s.one("SELECT count(*) AS n FROM triggers WHERE comment_id='old-queued'")["n"] == 1
