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
import signal
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

DEFAULT_LOG_DIR = Path(os.environ.get('LOG_DIR', '/var/log/plane-conductor'))
SENTINEL_SUBDIR = '.active'

mcp = FastMCP('plane-conductor')


def _log_dir() -> Path:
    return Path(os.environ.get('LOG_DIR', str(DEFAULT_LOG_DIR)))


def _sentinel_dir() -> Path:
    return _log_dir() / SENTINEL_SUBDIR


def _pid_alive(pid: int) -> bool:
    """Cheap liveness check via /proc — no kill(0) ambiguity on permissions."""
    return Path(f'/proc/{pid}').exists()


def _parse_log_filename(name: str) -> dict[str, str] | None:
    """Parse `<TIMESTAMPZ>-<workspace>-<nickname>-<issue8>.log`.

    Conductor uses `_log_path_for` with format
    `{ts}-{workspace_slug}-{nickname}-{str(issue_uuid)[:8]}.log`.
    """
    if not name.endswith('.log'):
        return None
    stem = name[:-4]
    parts = stem.split('-')
    if len(parts) < 4:
        return None
    timestamp = parts[0]
    workspace = parts[1]
    issue_short = parts[-1]
    nickname = '-'.join(parts[2:-1])
    return {
        'timestamp': timestamp,
        'workspace': workspace,
        'nickname': nickname,
        'issue_short': issue_short,
    }


@mcp.tool()
def list_active_agents() -> list[dict[str, Any]]:
    """List currently active agent runs (sentinels present, PID still alive).

    Each entry is the sentinel JSON enriched with `pid` (parsed from log
    filename's mtime → process), `pid_alive` boolean, and the resolved
    `log_path`. Stale sentinels (PID gone) are still listed but flagged.
    """
    sd = _sentinel_dir()
    if not sd.exists():
        return []
    out: list[dict[str, Any]] = []
    for f in sorted(sd.glob('*.json')):
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        pid = _pid_from_log_dir(Path(data.get('log_path', '')))
        data['sentinel_path'] = str(f)
        data['pid'] = pid
        data['pid_alive'] = _pid_alive(pid) if pid else False
        out.append(data)
    return out


def _pid_from_log_dir(log_path: Path) -> int | None:
    """Best-effort PID lookup: scan /proc for a claude --agent process whose
    cwd starts with the conductor working dir AND whose stdout fd points to
    this log file. Cheap enough for a few dozen processes; no caching needed.
    """
    if not log_path:
        return None
    log_path_str = str(log_path)
    proc = Path('/proc')
    if not proc.exists():
        return None
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            cmdline = (entry / 'cmdline').read_bytes()
            if b'claude' not in cmdline or b'--agent' not in cmdline:
                continue
            fd1 = entry / 'fd' / '1'
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
    """
    out: list[dict[str, Any]] = []
    ld = _log_dir()
    if not ld.exists():
        return []
    for f in sorted(ld.glob('*.log'), reverse=True):
        meta = _parse_log_filename(f.name)
        if not meta:
            continue
        if workspace and meta['workspace'] != workspace:
            continue
        if nickname and meta['nickname'] != nickname:
            continue
        if issue_prefix and not meta['issue_short'].startswith(issue_prefix):
            continue
        try:
            stat = f.stat()
        except OSError:
            continue
        out.append(
            {
                **meta,
                'path': str(f),
                'size_bytes': stat.st_size,
                'mtime': stat.st_mtime,
            }
        )
        if len(out) >= limit:
            break
    return out


@mcp.tool()
def read_log(path: str, max_bytes: int = 50_000) -> dict[str, Any]:
    """Read an agent log file. Truncates from the head if larger than
    `max_bytes` (returns the tail — that's where the final summary lives).
    """
    p = Path(path)
    ld_resolved = _log_dir().resolve()
    try:
        if not p.resolve().is_relative_to(ld_resolved):
            return {'error': f'path outside log_dir: {ld_resolved}'}
    except (OSError, ValueError):
        return {'error': 'invalid path'}
    if not p.exists():
        return {'error': f'not found: {path}'}
    try:
        data = p.read_bytes()
    except OSError as exc:
        return {'error': str(exc)}
    truncated = False
    if len(data) > max_bytes:
        data = data[-max_bytes:]
        truncated = True
    try:
        text = data.decode('utf-8', errors='replace')
    except Exception as exc:
        return {'error': f'decode failed: {exc}'}
    return {
        'path': str(p),
        'size_bytes': p.stat().st_size,
        'truncated_to_tail': truncated,
        'content': text,
    }


@mcp.tool()
def agent_summary(path: str) -> dict[str, Any]:
    """Extract the final stdout block of an agent log — the part after
    `# --- stdout/stderr ---`. That's the agent's final message before
    exit.
    """
    p = Path(path)
    ld_resolved = _log_dir().resolve()
    try:
        if not p.resolve().is_relative_to(ld_resolved):
            return {'error': f'path outside log_dir: {ld_resolved}'}
    except (OSError, ValueError):
        return {'error': 'invalid path'}
    if not p.exists():
        return {'error': f'not found: {path}'}
    text = p.read_text(errors='replace')
    marker = '# --- stdout/stderr ---'
    if marker not in text:
        return {'error': 'no stdout/stderr marker found in log'}
    summary = text.split(marker, 1)[1].lstrip('\n')
    return {'path': str(p), 'summary': summary, 'length': len(summary)}


@mcp.tool()
def kill_agent(pid: int, sig: str = 'SIGTERM') -> dict[str, Any]:
    """Send a signal to a running agent process by PID.

    `sig` accepts SIGTERM (default, polite) or SIGKILL (force). Refuses
    if the target process does not look like a `claude --agent` run.
    """
    if not _pid_alive(pid):
        return {'ok': False, 'error': f'pid {pid} not alive'}
    try:
        cmdline = Path(f'/proc/{pid}/cmdline').read_bytes()
    except OSError as exc:
        return {'ok': False, 'error': f'cannot read cmdline: {exc}'}
    if b'claude' not in cmdline or b'--agent' not in cmdline:
        return {
            'ok': False,
            'error': 'pid does not look like a claude agent run; refusing',
        }
    try:
        signo = {'SIGTERM': signal.SIGTERM, 'SIGKILL': signal.SIGKILL}[sig]
    except KeyError:
        return {'ok': False, 'error': f'unsupported signal: {sig}'}
    try:
        os.kill(pid, signo)
    except OSError as exc:
        return {'ok': False, 'error': str(exc)}
    return {'ok': True, 'pid': pid, 'signal': sig}


def main() -> None:
    """Entry point for the `plane-conductor-mcp` console script."""
    mcp.run()


if __name__ == '__main__':
    main()
