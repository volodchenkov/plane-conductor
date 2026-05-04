# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — 2026-05-03

### Added
- FastAPI webhook receiver with HMAC-SHA256 signature verification (`POST /webhook`).
- `<mention-component>` UUID extraction inline in the webhook handler.
- Member → nickname → prompt-role resolution against a configurable agent roster.
- **Workflow config** (`conductor.yaml`) — declarative YAML describing agents,
  labels, optional states, and behaviour flags. Two ready-to-use example
  configs ship under `examples/`:
  - `sdlc-conductor.yaml` — full SDLC pipeline (10 roles)
  - `minimal-conductor.yaml` — single dev agent
- Async subprocess spawner (`Runner`) that runs `claude --agent <nickname> --print`
  per mention, pipes a short trigger prompt to stdin, and streams
  stdout/stderr to a per-run log file.
- **`announce_spawn`** — when enabled (default), the orchestrator posts a
  `picking up @nick` comment to the issue immediately on spawn and updates the
  same comment on exit. Gives instant feedback in Plane regardless of how slow
  the agent itself is to respond.
- Async Plane REST client (`PlaneClient`) — used by the setup tooling and
  the webhook handler for member lookup, comment create / update.
- CLI: `serve`, `setup`, `setup --states`, `setup --dry-run`, `verify`,
  `agents`, `--version`.
- Idempotent setup tooling that invites every configured agent and creates
  every configured label / state.
- Test suite (~80 tests + 7 e2e skipped without `PLANE_E2E=1`) covering
  webhook routing + HMAC, runner subprocess lifecycle (incl. timeout, dedup,
  capacity, process-group kill, sentinel-recovery, announce/update), Plane
  client, and config loading.
- GitHub Actions CI (lint + mypy + pytest matrix) and PyPI publish workflow.
- Example deployments: `setup/install.sh` (idempotent systemd installer),
  Dockerfile, docker-compose, nginx reverse proxy.

### Resilience
- **Dedup** — `(nickname, issue)` pairs cannot be spawned twice in parallel.
  Plane delivers webhooks at-least-once and a human can double-mention; without
  this guard, two agents would race to create the same artifact.
- **Capacity cap** — `MAX_CONCURRENT_SESSIONS` (default 5) rejects further
  spawns once that many are in flight.
- **Process-group control** — every subprocess runs in its own session
  (`start_new_session=True`); on timeout we `killpg(SIGTERM)` then SIGKILL the
  whole group, killing descendants of `claude` (MCP servers, helper procs).
- **Graceful shutdown** — `SHUTDOWN_GRACE_SECONDS` (default 30) lets in-flight
  agents finish on SIGTERM before SIGKILL. Tuned to stay under systemd's
  default `TimeoutStopSec=90`.
- **Restart recovery** — sentinel files in `logs/.active/` mark in-flight
  spawns. On startup the server scans them and posts a recovery comment to
  Plane for each, so the human sees "agent was running when conductor
  restarted; mention me again to continue."
- **503 on transient Plane errors** — webhook returns `503 Service Unavailable`
  when member lookup hits a 5xx / network error, so Plane retries the
  delivery instead of silently dropping the mention.

### Design notes
- No persistent state besides sentinel files in the log dir.
- Re-entry — distinguishing first-run / continuation / rework — is the
  agent's responsibility (its own prompt). The orchestrator just spawns,
  supervises, and surfaces the outcome.

[Unreleased]: https://github.com/volodchenkov/plane-conductor/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/volodchenkov/plane-conductor/releases/tag/v0.1.0
