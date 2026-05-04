# Configuration

Plane Conductor has **two** configuration sources. Keep them straight:

| Where | What goes there | Loaded by |
|---|---|---|
| `.env` (or shell env) | Runtime concerns: secrets, ports, file paths, capacity, log level. | `Settings` (pydantic-settings) |
| `conductor.yaml` | Workflow concerns: agents, labels, states, behaviour flags. | `ConductorConfig` (pydantic) |

The split is on purpose: rotating an API key shouldn't touch your
agent roster, and renaming an agent shouldn't touch your secrets.

---

## `.env` — runtime settings

Loaded from (in order, last wins):

1. `/etc/plane-conductor/.env` (system-wide, written by `install.sh`)
2. `./.env` next to where `plane-conductor` is invoked (overrides)
3. Process environment variables (overrides everything)

Full template lives at [`.env.example`](../.env.example). Required
fields:

### Plane connection

| Var | Purpose |
|---|---|
| `PLANE_BASE_URL` | Base URL of your Plane instance (cloud `https://app.plane.so` or self-hosted). |
| `PLANE_API_KEY` | API token from Plane → Profile → Workspace settings → API tokens. |
| `PLANE_WORKSPACE_SLUG` | Workspace slug (lowercase, the segment in the Plane URL). |
| `PLANE_PROJECT_ID` | Project UUID. Copy from the Plane URL `/projects/<this>/`. |

### Webhook

| Var | Default | Purpose |
|---|---|---|
| `WEBHOOK_SECRET` | — | Shared HMAC-SHA256 secret. Must match the secret in Plane → Webhooks. Generate with `openssl rand -hex 32`. |
| `WEBHOOK_HOST` | `0.0.0.0` | Bind address. |
| `WEBHOOK_PORT` | `8000` | Bind port. |
| `WEBHOOK_SIGNATURE_HEADER` | `X-Plane-Signature` | Header Plane uses to send the signature. Adjust if your Plane build uses a different header name. |

### Workflow config pointer

| Var | Default | Purpose |
|---|---|---|
| `CONDUCTOR_CONFIG` | `/etc/plane-conductor/conductor.yaml` | Path to the YAML described below. |

### Agent invocation

| Var | Default | Purpose |
|---|---|---|
| `EMAIL_DOMAIN` | — | Domain for bot emails. `setup` invites `<nickname>@<EMAIL_DOMAIN>`. |
| `PROMPTS_DIR` | — | Absolute path to your Claude Code agent prompt files (`<role>.md`). |
| `AGENT_WORKING_DIR` | cwd of the service | Working directory passed to spawned `claude`. Usually your project root. |
| `INITIATOR_UUID` | — | Your own Plane member UUID. Mentions of this UUID are silently ignored — we don't trigger the human as an agent. |
| `CLAUDE_BINARY` | `claude` | Path to the `claude` CLI binary. Use absolute path if it's not on the service user's `PATH`. |

### Operations

| Var | Default | Purpose |
|---|---|---|
| `LOG_DIR` | `./logs` | Where per-run agent log files (and `.active/` sentinels) live. |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL`. |
| `LOG_FORMAT` | `pretty` | `pretty` (console renderer with colours) or `json` (one JSON object per line — for log shipping). |
| `MAX_CONCURRENT_SESSIONS` | `5` | Hard cap on simultaneously-running agents. N+1th spawn is rejected with `CapacityFullError`. |
| `SESSION_TIMEOUT_SECONDS` | `3600` | Per-agent timeout. After that the supervisor kills the whole process group (SIGTERM, then SIGKILL after 5s). |
| `SHUTDOWN_GRACE_SECONDS` | `30` | On `systemctl stop`, how long to wait for in-flight agents to finish before SIGKILL. Keep below systemd's `TimeoutStopSec` (default 90s). |
| `ALLOWED_NICKNAMES` | empty | Comma-separated allow-list of nicknames the orchestrator will spawn. Empty = allow all configured agents. Use to gate access during testing or to disable an agent without removing it from `conductor.yaml`. |

---

## `conductor.yaml` — workflow config

The shape is enforced by pydantic with `extra="forbid"` — typos fail
loudly. See [`examples/sdlc-conductor.yaml`](../examples/sdlc-conductor.yaml)
(10 SDLC roles) or
[`examples/minimal-conductor.yaml`](../examples/minimal-conductor.yaml)
(single dev) for ready-to-edit starters.

```yaml
agents:                       # required, at least one
  - nickname: castor          # required — email local-part = mention name
    prompt_role: business-analyst   # required — filename stem in PROMPTS_DIR
    display_name: Castor — BA       # optional, used by `setup` for Plane display

labels:                       # optional
  artifacts: []               # list of {name, color?, description?}
  roles: []                   # list of {name, color?, description?}

states: []                    # optional, list of {name, group, color?}
                              # group ∈ backlog | unstarted | started | completed | cancelled

announce_spawn: true          # default true
```

### `agents`

Each entry maps a nickname (the email local-part you'll mention with
`@`) to a prompt file in `PROMPTS_DIR`. When a Plane mention resolves
to email `castor@your-domain.io` and there's an `agents` entry with
`nickname: castor`, the orchestrator runs:

```
claude --agent castor --print
```

…with the prompt fed via stdin and `<prompt_role>.md` (here:
`business-analyst.md`) being read by Claude Code from `PROMPTS_DIR`.

`display_name` is only used by `plane-conductor setup` so the bot
account in Plane shows up as something readable in the UI. It has no
runtime effect.

Constraints:

- Nicknames must be unique within the file (case-insensitive — they're
  lowercased on load).
- Unknown YAML keys are rejected (`extra="forbid"`). Catches typos like
  `prompt-role:` instead of `prompt_role:`.

### `labels`

Two conventional groups, both optional:

- **`artifacts:`** — one label per agent output type. Use them to mark
  sub-issues by what they contain (a SPEC, a backend plan, a UX test
  report, etc.), so agents can find each other's work.
- **`roles:`** — reserve. Useful when one agent comments inside another
  agent's sub-issue and wants to mark *whose voice* it is.

Each label is `{name, color?, description?}`:

```yaml
labels:
  artifacts:
    - { name: "artifact:spec", color: "#3b82f6" }
    - { name: "artifact:backend", color: "#10b981", description: "Backend plan + CHANGES" }
```

`plane-conductor setup` creates whichever ones don't exist yet. Already-existing
labels are skipped silently (idempotent).

### `states`

Optional decorative project states (only created when you pass
`--states` to `setup`). Plane's stock states (Backlog, Todo, In
Progress, Done, Cancelled) are always present — this is for adding
extras like Review or Blocked that match your workflow vocabulary.

```yaml
states:
  - { name: Review, group: started, color: "#f59e0b" }
  - { name: Blocked, group: unstarted, color: "#ef4444" }
```

`group` is one of Plane's five built-in groups: `backlog`, `unstarted`,
`started`, `completed`, `cancelled`. The state inherits behaviour from
its group (e.g. issues in a `completed`-group state count as done).

### `announce_spawn`

When `true` (default), the orchestrator posts an `<code>@nick</code>
picking up. Working on it…` comment to the issue the moment a
subprocess starts, and **updates that same comment** when the agent
exits (`done. Duration: 3m12s` or `exited 1. Duration: 0m04s`).

Why you want it on:

- **Instant feedback in Plane** — no "did anything happen?" pause while
  the agent thinks.
- **Surface signal independent of agent behaviour** — if the agent
  itself is slow / has a bug / never speaks, the human still sees
  *something happened*.
- **One comment per run, not three** — the announce comment doubles as
  the failure note (it gets edited to the error summary). No separate
  `agent failed` post when announce is on.

Set to `false` only if you specifically want the issue thread to look
"silent until the agent itself speaks".

---

## Two-config example

Minimal `.env`:

```bash
PLANE_BASE_URL=https://plane.example.io
PLANE_API_KEY=plane_api_xxxxxxxxxxxxxxxxxxxxxxxx
PLANE_WORKSPACE_SLUG=acme
PLANE_PROJECT_ID=00000000-0000-0000-0000-000000000000
WEBHOOK_SECRET=replace-me-with-openssl-rand-hex-32
CONDUCTOR_CONFIG=/etc/plane-conductor/conductor.yaml
EMAIL_DOMAIN=acme.io
PROMPTS_DIR=/home/you/Projects/acme/.claude/agents
AGENT_WORKING_DIR=/home/you/Projects/acme
INITIATOR_UUID=00000000-0000-0000-0000-000000000000
```

Minimal `conductor.yaml`:

```yaml
agents:
  - nickname: dev
    prompt_role: developer
    display_name: Dev

labels:
  artifacts: []
  roles: []

states: []
announce_spawn: true
```

That's enough to mention `@dev` in any Plane issue and have
`claude --agent dev --print` spawn locally.

---

## Where the orchestrator looks at startup

```
/etc/plane-conductor/.env       ← system .env (loaded first)
./.env                          ← cwd .env (overrides)
process env                     ← overrides everything

$CONDUCTOR_CONFIG               ← resolved from settings; default is
                                  /etc/plane-conductor/conductor.yaml
```

If anything required is missing or invalid, `plane-conductor serve`
fails fast at startup with a pydantic ValidationError naming exactly
what's wrong.
