"""Read-only MCP server for Plane Conductor.

Exposes filesystem-driven introspection of agent runs to Claude Code:
list of currently active sentinels, recent log files, log contents,
extracted final-summary blocks, and a kill-by-PID escape hatch for
stuck agents. No Plane API, no spawn — those stay in the conductor's
webhook path.

Run via the `plane-conductor-mcp` console script (stdio transport).
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

SENTINEL_SUBDIR = ".active"
_DEFAULT_LOG_DIR = "/var/log/plane-conductor"
_AGENT_SUMMARY_TAIL_BYTES = 64 * 1024
_LOG_NAME_RE = re.compile(
    r"^(?P<timestamp>\d{8}T\d{6}Z)-(?P<rest>.+)-(?P<issue_short>[0-9a-fA-F]{8})" r"\.log(?:\.\d+)?$"
)

mcp = FastMCP("plane-conductor")


def _log_dir() -> Path:
    return Path(os.environ.get("LOG_DIR", _DEFAULT_LOG_DIR))


def _sentinel_dir() -> Path:
    return _log_dir() / SENTINEL_SUBDIR


def _pid_alive(pid: int) -> bool:
    """Cross-platform liveness check.

    On Linux uses /proc; elsewhere falls back to `kill(pid, 0)` and treats
    PermissionError as alive (process exists, just not ours).
    """
    if sys.platform.startswith("linux") and Path("/proc").exists():
        return Path(f"/proc/{pid}").exists()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _known_workspace_slugs() -> list[str]:
    """Workspace slugs from sentinel JSON files; used to disambiguate
    hyphenated nicknames in log filenames.
    """
    slugs: set[str] = set()
    sd = _sentinel_dir()
    if sd.exists():
        for f in sd.glob("*.json"):
            try:
                data = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            slug = data.get("workspace_slug") or data.get("workspace")
            if isinstance(slug, str):
                slugs.add(slug)
    return sorted(slugs, key=len, reverse=True)


def _parse_log_filename(name: str, known_slugs: list[str] | None = None) -> dict[str, str] | None:
    """Parse `<TIMESTAMPZ>-<workspace>-<nickname>-<issue8>.log`.

    Workspace slugs may contain hyphens, so we split using a known-slug list
    (longest match wins). When no list is provided we fall back to assuming
    the workspace is the first hyphen-delimited segment after the timestamp.
    """
    m = _LOG_NAME_RE.match(name)
    if not m:
        return None
    rest = m.group("rest")
    timestamp = m.group("timestamp")
    issue_short = m.group("issue_short")

    workspace: str | None = None
    nickname: str | None = None
    if known_slugs:
        for slug in known_slugs:
            if rest == slug or rest.startswith(slug + "-"):
                workspace = slug
                nickname = rest[len(slug) + 1 :] if rest != slug else ""
                break
    if workspace is None:
        head, _, tail = rest.partition("-")
        if not tail:
            return None
        workspace = head
        nickname = tail
    if not nickname:
        return None
    return {
        "timestamp": timestamp,
        "workspace": workspace,
        "nickname": nickname,
        "issue_short": issue_short,
    }


@mcp.tool()
def list_active_agents() -> list[dict[str, Any]]:
    """List currently active agent runs (sentinels present, PID still alive).

    Each entry is the sentinel JSON enriched with `pid` (parsed from log
    filename's mtime → process), `pid_alive` boolean, and the resolved
    `log_path`. Stale sentinels (PID gone) are still listed but flagged;
    the conductor cleans them on its own restart via `recover_orphaned_sessions`.
    """
    sd = _sentinel_dir()
    if not sd.exists():
        return []
    out: list[dict[str, Any]] = []
    for f in sorted(sd.glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        pid = _pid_from_log_dir(Path(data.get("log_path", "")))
        data["sentinel_path"] = str(f)
        data["pid"] = pid
        data["pid_alive"] = _pid_alive(pid) if pid else False
        out.append(data)
    return out


def _pid_from_log_dir(log_path: Path) -> int | None:
    """Best-effort PID lookup via /proc on Linux. Returns None elsewhere
    (or when the process can't be located).
    """
    if not log_path:
        return None
    proc = Path("/proc")
    if not proc.exists():
        return None
    log_path_str = str(log_path)
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            cmdline = (entry / "cmdline").read_bytes()
            if b"claude" not in cmdline or b"--agent" not in cmdline:
                continue
            fd1 = entry / "fd" / "1"
            if fd1.exists() and str(fd1.resolve()) == log_path_str:
                return pid
        except (OSError, PermissionError):
            continue
    return None


@mcp.tool()
def recent_runs(
    workspace: str | None = None,
    nickname: str | None = None,
    issue_prefix: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List most-recent agent runs from log directory.

    Filters: `workspace` (exact slug), `nickname` (exact, e.g. `sark`),
    `issue_prefix` (matches first 8 chars of issue UUID). Sorted by
    timestamp descending. Returns metadata only — call `read_log` to fetch
    contents or `agent_summary` for the final stdout block.

    Logrotate-aware: a single agent run can exist on disk as `*.log` (fresh
    empty stub after rotation) plus one or more `*.log.N` siblings holding
    the actual content. Each agent run is deduplicated to the variant with
    the largest `size_bytes`, so an operator scanning history doesn't see a
    wall of zero-byte stubs hiding the real logs.
    """
    if limit <= 0:
        return []
    ld = _log_dir()
    if not ld.exists():
        return []
    known = _known_workspace_slugs()
    by_base: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for f in ld.glob("*.log*"):
        meta = _parse_log_filename(f.name, known_slugs=known)
        if not meta:
            continue
        if workspace and meta["workspace"] != workspace:
            continue
        if nickname and meta["nickname"] != nickname:
            continue
        if issue_prefix and not meta["issue_short"].startswith(issue_prefix):
            continue
        try:
            stat = f.stat()
        except OSError:
            continue
        key = (
            meta["timestamp"],
            meta["workspace"],
            meta["nickname"],
            meta["issue_short"],
        )
        candidate = {
            **meta,
            "path": str(f),
            "size_bytes": stat.st_size,
            "mtime": stat.st_mtime,
        }
        prev = by_base.get(key)
        if prev is None or candidate["size_bytes"] > prev["size_bytes"]:
            by_base[key] = candidate
    out = sorted(by_base.values(), key=lambda r: r["timestamp"], reverse=True)
    return out[:limit]


@mcp.tool()
def read_log(path: str, max_bytes: int = 50_000) -> dict[str, Any]:
    """Read an agent log file. Truncates from the head if larger than
    `max_bytes` (returns the tail — that's where the final summary lives).
    """
    p = Path(path)
    ld_resolved = _log_dir().resolve()
    try:
        if not p.resolve().is_relative_to(ld_resolved):
            return {"error": f"path outside log_dir: {ld_resolved}"}
    except (OSError, ValueError):
        return {"error": "invalid path"}
    if not p.exists():
        return {"error": f"not found: {path}"}
    try:
        size = p.stat().st_size
        with p.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                truncated = True
            else:
                truncated = False
            data = fh.read()
    except OSError as exc:
        return {"error": str(exc)}
    text = data.decode("utf-8", errors="replace")
    return {
        "path": str(p),
        "size_bytes": size,
        "truncated_to_tail": truncated,
        "content": text,
    }


@mcp.tool()
def agent_summary(path: str) -> dict[str, Any]:
    """Extract the final stdout block of an agent log — the part after
    `# --- stdout/stderr ---`. That's the agent's final message before
    exit. Reads only the trailing chunk to avoid OOM on huge logs.
    """
    p = Path(path)
    ld_resolved = _log_dir().resolve()
    try:
        if not p.resolve().is_relative_to(ld_resolved):
            return {"error": f"path outside log_dir: {ld_resolved}"}
    except (OSError, ValueError):
        return {"error": "invalid path"}
    if not p.exists():
        return {"error": f"not found: {path}"}
    marker = b"# --- stdout/stderr ---"
    try:
        size = p.stat().st_size
        with p.open("rb") as fh:
            if size > _AGENT_SUMMARY_TAIL_BYTES:
                fh.seek(size - _AGENT_SUMMARY_TAIL_BYTES)
                truncated = True
            else:
                truncated = False
            blob = fh.read()
    except OSError as exc:
        return {"error": str(exc)}
    idx = blob.find(marker)
    if idx == -1:
        if truncated:
            return {
                "error": "stdout/stderr marker not found in log tail "
                f"({_AGENT_SUMMARY_TAIL_BYTES} bytes); log may be malformed"
            }
        return {"error": "no stdout/stderr marker found in log"}
    summary = blob[idx + len(marker) :].decode("utf-8", errors="replace").lstrip("\n")
    return {"path": str(p), "summary": summary, "length": len(summary)}


@mcp.tool()
def kill_agent(pid: int, sig: str = "SIGTERM") -> dict[str, Any]:
    """Send a signal to a running agent process group by PID.

    `sig` accepts SIGTERM (default, polite) or SIGKILL (force). Refuses
    if the target process does not look like a `claude --agent` run.
    Targets the whole process group (the runner spawns with
    `start_new_session=True`, so PID == PGID), terminating descendants too.
    """
    if not _pid_alive(pid):
        return {"ok": False, "error": f"pid {pid} not alive"}
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError as exc:
        return {"ok": False, "error": f"cannot read cmdline: {exc}"}
    if b"claude" not in cmdline or b"--agent" not in cmdline:
        return {
            "ok": False,
            "error": "pid does not look like a claude agent run; refusing",
        }
    try:
        signo = {"SIGTERM": signal.SIGTERM, "SIGKILL": signal.SIGKILL}[sig]
    except KeyError:
        return {"ok": False, "error": f"unsupported signal: {sig}"}
    try:
        os.killpg(pid, signo)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "pid": pid, "signal": sig}


def main() -> None:
    """Entry point for the `plane-conductor-mcp` console script."""
    mcp.run()


if __name__ == "__main__":
    main()
