# Plane Conductor — Requirements

> **Role of this document:** business requirements (what the product does and why).
> Authored by Dmitry as initiator + reconstructed by Zuse (Prompt Architect) on bootstrap.
> When the team is in steady state — this artifact is owned by Castor (Business Analyst).

---

## 1. Vision

Plane Conductor is a webhook-driven orchestrator that turns mentions in [Plane](https://plane.so) issues into Claude Code agent runs. It enables a small team (or solo founder) to operate a full SDLC pipeline (analyst → architect → developers → testers → reviewer) where each role is an isolated Claude Code session triggered by a Plane mention.

The user works in Plane as they normally would. Mentioning a bot user (`@sark`, `@rinzler`, etc.) starts the corresponding agent in the background. The agent reads the issue context, performs its role (writes spec, writes code, runs tests), and reports back into the same Plane issue — a sub-issue with its artifact and a comment summarizing the result.

## 2. Why this exists

- Solo founders and small teams cannot afford a full team of analysts, architects, testers, and reviewers.
- LLM agents can do most of this work, but **only if** they receive structured input, follow a defined protocol, and operate within a real task management system. Ad-hoc chat with an LLM does not scale beyond toy projects.
- Plane provides the task management. Claude Code provides the execution. **Plane Conductor is the missing glue.**

## 3. Target users

- Solo developers / founders running a self-hosted Plane instance and Claude Code locally.
- Small teams (2–5 people) wanting AI augmentation of their development workflow.
- Open source maintainers who want to delegate routine tasks (refactoring, test writing, documentation) via issue mentions.

## 4. Core use cases

### UC-1: Trigger an agent via Plane mention
Initiator mentions `@sark` in a comment of issue `QSALE-42`. Plane sends a webhook. Plane Conductor:
1. Receives the webhook event.
2. Resolves the mentioned member to an agent role (via member email → nickname → prompt file mapping).
3. Spawns a Claude Code subprocess with the corresponding agent and the issue ID as context.
4. The agent runs to completion (or blocks on a question, posting a comment back to Plane).

### UC-2: Resume a long-running agent
An agent's session may take longer than a single subprocess lifetime (e.g. crash, container restart). On the next mention, Plane Conductor must support **idempotent re-entry**: the agent reads its own previous sub-issue and comments, determines whether it's a continuation or a rework, and continues without duplicating artifacts. This logic lives in agent prompts; Plane Conductor only routes the trigger.

### UC-3: Setup a fresh Plane workspace for the pipeline
A new user clones the repo, points it at their Plane instance, runs one CLI command — and the system creates 10 bot users, the artifact/role labels, optional states (Review, Blocked), and validates the configuration. They can immediately start mentioning agents.

### UC-4: Observability
The user can see:
- Which agents are currently running.
- Logs of each agent run (stdout/stderr captured to file or streamed).
- Failures (crashes, timeouts, blocked permissions) — surfaced as Plane comments.

## 5. Functional requirements

### FR-1: Plane webhook receiver
- HTTP server listening for Plane webhook events.
- Authenticates webhook payload (HMAC signature verification using a shared secret).
- Filters events to those relevant to the orchestrator (issue comments, mentions).

### FR-2: Mention parser
- Extracts mentioned member UUIDs from event payload.
- For each UUID, resolves the member via Plane API.
- Maps member email's local part to an agent nickname → prompt file.
- Mapping defined in `plane-api.md` §3 (canonical source).

### FR-3: Agent dispatcher
- For each resolved agent, spawns `claude --agent <nickname>` as a subprocess.
- Passes issue identifier (e.g. `QSALE-42`) as context.
- Captures stdout/stderr to a per-run log file with timestamp.
- Manages a registry of running sessions to prevent duplicates (same agent + same issue should not run twice in parallel).

### FR-4: Configuration
- Config loaded from environment + `.env` + optional config file.
- Required: Plane API key, Plane base URL, workspace slug, webhook secret, working directory of agent prompts.
- Optional: log level, log directory, max concurrent sessions, subprocess timeout, allowed nicknames whitelist.

### FR-5: Setup CLI
- `plane-conductor setup` — interactive or `.env`-driven. Creates bot users, labels, optional states. Idempotent (safe to run multiple times).
- `plane-conductor verify` — reads Plane and confirms bot users, labels, project access.
- `plane-conductor serve` — starts the webhook server.

### FR-6: Public repository readiness
- The codebase ships as a public GitHub repository.
- Does not contain any secrets, customer-specific UUIDs, or internal URLs (these come from user's `.env`).
- Includes README, LICENSE (MIT), CONTRIBUTING, CODE_OF_CONDUCT, CHANGELOG.
- CI: lint, type-check, tests on push.
- Publishable to PyPI (so users can `pip install plane-conductor`).

## 6. Non-functional requirements

### NFR-1: Latency
- From webhook receipt to subprocess spawn: < 2 seconds at p95.
- The user should perceive mentioning an agent as "instant".

### NFR-2: Reliability
- Webhook receiver does not lose events under reasonable load (10 events/sec).
- Subprocess crashes are caught and surfaced (a comment posted to Plane: "agent failed, see logs at <path>").

### NFR-3: Security
- HMAC verification of webhook payload — required.
- API key stored only in env vars / config file with restricted permissions, never in code or logs.
- No outbound calls to anything except Plane and Anthropic API (via Claude Code).

### NFR-4: Portability
- Runs on Linux (primary), macOS (development).
- Single Python package, no external services required (no Postgres, Redis — at least for v0.1).
- Optional Docker image for production deployment.

### NFR-5: Code quality (for public repo)
- Type-annotated (Pydantic models for events).
- Linted (ruff), formatted, tested (pytest with coverage > 70%).
- Documented (mkdocs or similar) with architecture diagram, setup guide, troubleshooting.

## 7. Out of scope (for v0.1)

- Web UI / dashboard. CLI and Plane UI are the only interfaces.
- Multi-tenancy. One Plane Conductor instance serves one Plane workspace.
- Authentication beyond webhook HMAC. The orchestrator runs in a trusted environment.
- Adapters for Linear / Jira / GitHub Issues. Plane-only for now (architecture should not preclude future adapters).
- Persistent state (database). In-memory session registry is enough for v0.1; sessions don't survive restart, but that's acceptable for a small team.
- Distributed deployment. Single instance is fine.

## 8. Constraints

- The pipeline protocol is defined in `~/Projects/claude-workspace/projects/qa/.agents/knowledge/plane-api.md`. Plane Conductor must follow its mapping table (§3) for member→prompt routing.
- Agent prompts live in a separate directory (`~/Projects/qsale/.claude/agents/` for QSale, configurable). Plane Conductor only invokes Claude Code; it does not inline prompts.
- Self-hosted Plane (Plane Community Edition). The Plane MCP server (makeplane/plane-mcp-server) is used by **agents**, not by Plane Conductor itself. Plane Conductor talks to Plane via the REST API directly (it needs access that MCP doesn't expose, e.g. invite users, create labels in bulk).

## 9. Success criteria

- Mentioning `@rinzler` on a fresh issue with a SPEC sub-issue starts the python-developer agent within 2 seconds.
- The agent's stdout is captured to `logs/<timestamp>-<nickname>-<issue>.log`.
- The agent posts a startup comment within 10 seconds, and a summary comment when done.
- A new user can clone the repo, fill in `.env`, run `plane-conductor setup` followed by `plane-conductor serve`, and have a working orchestrator in under 5 minutes.
- The repo, as published on GitHub, looks like a serious open source project (badges, docs, examples, contribution guide).
