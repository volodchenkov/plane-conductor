"""Reliability settings (QSALE-55 NFR-2): every retry and limit is configurable and validated."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from plane_conductor.config import Settings


def test_state_dir_defaults_to_an_absolute_dir_under_log_dir(tmp_path: Path) -> None:
    s = Settings(_env_file=None, log_dir=tmp_path / "logs")  # type: ignore[call-arg]
    assert s.state_dir == tmp_path / "logs" / "state" and s.state_dir.is_absolute()


def test_explicit_state_dir_is_kept_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    s = Settings(_env_file=None, state_dir=Path("st"))  # type: ignore[call-arg]
    assert s.state_dir == tmp_path / "st"


def test_queue_age_must_exceed_session_timeout() -> None:
    with pytest.raises(ValidationError, match="queue_max_age_seconds"):
        Settings(_env_file=None, queue_max_age_seconds=100, session_timeout_seconds=3600)  # type: ignore[call-arg]


def test_documented_defaults() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.start_retries == 3 and s.start_retry_backoff_seconds == [5, 15, 45]
    assert s.start_no_tower_timeout_seconds == 180
    assert (s.handoff_ack_seconds, s.handoff_max_retries) == (300, 3)
    assert s.queue_max_age_seconds == 5400 and s.alert_max_attempts == 20
