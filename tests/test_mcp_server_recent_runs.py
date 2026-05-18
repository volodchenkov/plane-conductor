"""recent_runs must surface real log content even after logrotate.

A rotated agent log lives on disk as `<ts>-<ws>-<nick>-<short>.log.1`
(or `.log.2`, ...) while logrotate leaves a fresh empty `<ts>-<ws>-<nick>-<short>.log`
stub. The MCP-side recent_runs must:
- match the rotated extensions, not only `.log`;
- dedupe sibling variants of the same agent run to the largest size_bytes;
- still sort by spawn timestamp descending.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from plane_conductor import mcp_server


@pytest.fixture
def log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "plane-conductor-logs"
    d.mkdir()
    monkeypatch.setenv("LOG_DIR", str(d))
    return d


def _touch(d: Path, name: str, content: str = "") -> Path:
    p = d / name
    p.write_text(content)
    return p


def test_rotated_pair_returns_only_the_non_empty_variant(log_dir: Path) -> None:
    base = "20260514T181836Z-coinex-rinzler-9405d99a"
    _touch(log_dir, f"{base}.log")  # empty stub left by logrotate
    _touch(log_dir, f"{base}.log.1", "actual content here")

    runs = mcp_server.recent_runs(workspace="coinex")

    assert len(runs) == 1
    assert runs[0]["path"].endswith(".log.1")
    assert runs[0]["size_bytes"] > 0
    assert runs[0]["nickname"] == "rinzler"
    assert runs[0]["issue_short"] == "9405d99a"


def test_multiple_rotations_pick_the_largest(log_dir: Path) -> None:
    base = "20260514T181836Z-coinex-rinzler-9405d99a"
    _touch(log_dir, f"{base}.log")  # 0 bytes
    _touch(log_dir, f"{base}.log.1", "x" * 500)
    _touch(log_dir, f"{base}.log.2", "x" * 200)

    runs = mcp_server.recent_runs(workspace="coinex")

    assert len(runs) == 1
    assert runs[0]["path"].endswith(".log.1")
    assert runs[0]["size_bytes"] == 500


def test_distinct_runs_are_kept_separate(log_dir: Path) -> None:
    _touch(log_dir, "20260514T181836Z-coinex-rinzler-9405d99a.log.1", "first")
    _touch(log_dir, "20260514T190000Z-coinex-sark-9405d99a.log", "second")
    _touch(log_dir, "20260514T200000Z-coinex-flynn-b02eae4b.log", "third")

    runs = mcp_server.recent_runs(workspace="coinex")

    assert len(runs) == 3
    nicks = [r["nickname"] for r in runs]
    assert nicks == ["flynn", "sark", "rinzler"]  # newest first


def test_filters_and_limit(log_dir: Path) -> None:
    _touch(log_dir, "20260514T181836Z-coinex-rinzler-9405d99a.log.1", "x")
    _touch(log_dir, "20260514T190000Z-coinex-sark-9405d99a.log", "x")
    _touch(log_dir, "20260514T200000Z-aist-flynn-b02eae4b.log", "x")

    assert {r["workspace"] for r in mcp_server.recent_runs(workspace="aist")} == {"aist"}
    assert {r["nickname"] for r in mcp_server.recent_runs(nickname="sark")} == {"sark"}
    assert (
        len(mcp_server.recent_runs(issue_prefix="9405d99a")) == 2
    )
    assert len(mcp_server.recent_runs(limit=1)) == 1


def test_non_log_files_are_ignored(log_dir: Path) -> None:
    _touch(log_dir, "README.md", "docs")
    _touch(log_dir, "20260514T181836Z-coinex-rinzler-9405d99a.log.bak", "irrelevant suffix")
    _touch(log_dir, "20260514T190000Z-coinex-sark-9405d99a.log", "ok")

    runs = mcp_server.recent_runs()

    assert len(runs) == 1
    assert runs[0]["nickname"] == "sark"
