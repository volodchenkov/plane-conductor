# Configuration

Plane Conductor has **two** configuration sources, on purpose:

| Where | What goes there | Loaded by |
|---|---|---|
| `runtime.env` (or shell env) | Host-wide runtime: bind host/port, log dir, capacity caps, timeouts, claude binary. | `Settings` (pydantic-settings) |
| `conductor.d/<slug>.yaml` (one file per workspace) | Per-workspace: Plane creds, project, prompts dir, agents, labels, states, behaviour, secrets. | `WorkspaceConfig` (pydantic) |

The split keeps host knobs separate from workspace shape. Adding an AIST
workspace alongside qsale is just a new file in `conductor.d/`; it does not
touch the runtime env. Rotating an API key changes one file. Renaming an
agent changes one file.

---

## `runtime.env` — host-wide settings

Loaded from (in order, last wins):

1. `/etc/plane-conductor/runtime.env` (system-wide, written by `install.sh`)
2. `/etc/plane-conductor/.env` (legacy filename — still honoured, optional)
3. `./runtime.env` next to the cwd `plane-conductor` is invoked from
4. `./.env` (cwd, last-resort fallback)
5. Process environment variables (override everything)

Full template lives at [`examples/runtime.env.example`](../examples/runtime.env.example).

### Webhook server

| Var | Default | Purpose |
|---|---|---|
| `WEBHOOK_HOST` | `0.0.0.0` | Bind address. |
| `WEBHOOK_PORT` | `8000` | Bind port. |

### Workspace configs location

| Var | Default | Purpose |
|---|---|---|
| `CONDUCTOR_DIR` | `/etc/plane-conductor/conductor.d` | Directory of per-workspace YAML configs. One file per workspace, named `<slug>.yaml`. |

### Agent invocation

| Var | Default | Purpose |
|---|---|---|
| `CLAUDE_BINARY` | `claude` | Path to the `claude` CLI binary. Use absolute path if it isn't on the service user's `PATH`. Each workspace's `prompts_dir` and `agent_working_dir` come from its own YAML. |

### Operations

| Var | Default | Purpose |
|---|---|---|
| `LOG_DIR` | `./logs` | Where per-run agent log files (and `.active/` sentinels) live. Shared across workspaces; filenames carry the workspace slug. |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL`. |
| `LOG_FORMAT` | `pretty` | `pretty` (console renderer with colours) or `json` (one JSON object per line). |
| `MAX_CONCURRENT_SESSIONS` | `5` | **Host-wide** cap on simultaneously-running agents (across ALL workspaces). N+1th spawn is rejected with `CapacityFullError`. |
| `SESSION_TIMEOUT_SECONDS` | `3600` | Per-agent timeout. After that the supervisor kills the whole process group (SIGTERM, then SIGKILL after 5s). |
| `SHUTDOWN_GRACE_SECONDS` | `30` | On `systemctl stop`, how long to wait for in-flight agents to finish before SIGKILL. Keep below systemd's `TimeoutStopSec` (default 90s). |

---

## `conductor.d/<slug>.yaml` — per-workspace config

One self-contained YAML per workspace. The filename stem **must equal**
`workspace_slug` (the loader rejects mismatches).

The shape is enforced by pydantic with `extra="forbid"` — typos fail loudly.

Ready-to-edit starters:
- [`examples/conductor.d/sdlc.yaml`](../examples/conductor.d/sdlc.yaml) —
  full SDLC pipeline (10 roles)
- [`examples/conductor.d/minimal.yaml`](../examples/conductor.d/minimal.yaml) —
  single dev agent
- [`examples/conductor.d/content.yaml`](../examples/conductor.d/content.yaml) —
  editorial pipeline (briefer → researcher → drafter → editor → SEO → fact-checker → publisher)

> **Important:** these files hold secrets (API key, webhook secret). Treat
> them like `.env`: chmod 600, do **not** commit your edited copies. The
> versions in `examples/` are templates with placeholder credentials.

```yaml
# --- identity / Plane connection (required) ---
workspace_slug: qsale            # MUST match the filename stem
plane_base_url: https://plane.example.io
plane_api_key: plane_api_xxxxxxxxxxxxxxxxxxxxxxxx
project_id: 00000000-0000-0000-0000-000000000000
initiator_uuid: 00000000-0000-0000-0000-000000000099

# --- webhook (required) ---
webhook_secret: replace-me-with-openssl-rand-hex-32
webhook_signature_header: X-Plane-Signature   # optional, default shown

# --- agent invocation (required) ---
email_domain: example.io
prompts_dir: /home/you/Projects/yourproject/.claude/agents
agent_working_dir: /home/you/Projects/yourproject   # optional, defaults to cwd

# --- workflow (agents required, labels/states optional) ---
agents:
  - nickname: castor
    prompt_role: business-analyst
    display_name: Castor — BA

labels:
  artifacts: []
  roles: []

states: []

# --- behaviour (optional) ---
announce_spawn: true
allowed_nicknames: []      # empty = allow all configured agents
```

### Top-level fields

| Field | Required | Notes |
|---|---|---|
| `workspace_slug` | yes | Lowercase. Filename stem must equal this. Path segment of the webhook URL: `POST /<workspace_slug>/webhook`. |
| `plane_base_url` | yes | Plane base URL (cloud or self-hosted). Trailing slash stripped. |
| `plane_api_key` | yes | Plane API token from Profile → Workspace settings → API tokens. |
| `project_id` | yes | Project UUID inside the workspace. |
| `initiator_uuid` | yes | Your Plane member UUID. Mentions of this UUID are silently ignored — we don't trigger you as an agent. |
| `webhook_secret` | yes | Per-workspace HMAC secret. Generate with `openssl rand -hex 32`. Must match the secret in Plane → Webhooks for this workspace. |
| `webhook_signature_header` | no | Default `X-Plane-Signature`. Adjust if your Plane build uses a different header. |
| `email_domain` | yes | Bot email domain. `setup` invites `<nickname>@<email_domain>`. |
| `prompts_dir` | yes | Absolute path to your Claude Code agent prompt files (`<role>.md`). |
| `agent_working_dir` | no | Working dir passed to spawned `claude`. Defaults to the orchestrator's cwd. Usually your project root. |
| `agents` | yes | At least one entry. See below. |
| `labels` | no | `{artifacts: [...], roles: [...]}`. Each label is `{name, color?, description?}`. |
| `states` | no | List of `{name, group, color?}`. `group` ∈ `backlog`/`unstarted`/`started`/`completed`/`cancelled`. |
| `announce_spawn` | no | Default `true`. Posts a 'Picking up @nick…' comment on spawn and edits it on exit. |
| `allowed_nicknames` | no | Allow-list of nicknames. Empty = allow all configured agents. Use to gate access during testing. |

### `agents`

Each entry:

```yaml
agents:
  - nickname: castor             # email local-part = mention name (lowercased)
    prompt_role: business-analyst # filename stem in PROMPTS_DIR (→ business-analyst.md)
    display_name: Castor — BA    # optional, used by `setup` for the bot's Plane display
```

When a Plane mention resolves to email `castor@<email_domain>` and there's an
`agents` entry with `nickname: castor`, the orchestrator runs:

```bash
claude --agent castor --print
```

…with the trigger prompt fed via stdin and `<prompt_role>.md` (here:
`business-analyst.md`) being read by Claude Code from `prompts_dir`.

Nicknames must be unique within the file (case-insensitive — lowercased on
load). Unknown YAML keys are rejected.

### `labels`

Two conventional groups, both optional:

- **`artifacts:`** — one label per agent output type. Use them to mark
  sub-issues by what they contain (a SPEC, a backend plan, a UX test report,
  etc.), so agents can find each other's work.
- **`roles:`** — reserve. Useful when one agent comments inside another
  agent's sub-issue and wants to mark *whose voice* it is.

```yaml
labels:
  artifacts:
    - { name: "artifact:spec", color: "#3b82f6" }
    - { name: "artifact:backend", color: "#10b981", description: "Backend plan + CHANGES" }
```

`plane-conductor setup` creates whichever labels don't exist yet. Already-existing
labels are skipped silently (idempotent).

### `states`

Optional decorative project states (only created when you pass `--states` to
`setup`). Plane's stock states (Backlog, Todo, In Progress, Done, Cancelled)
are always present — this is for adding extras like Review or Blocked.

```yaml
states:
  - { name: Review, group: started, color: "#f59e0b" }
  - { name: Blocked, group: unstarted, color: "#ef4444" }
```

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
  the failure note (it gets edited to the error summary).

Set to `false` only if you specifically want the issue thread to look
"silent until the agent itself speaks".

---

## Two-workspace example

Directory layout on the host:

```text
/etc/plane-conductor/
  runtime.env                       # 640, root:<your-group>
  conductor.d/
    qsale.yaml                      # 600 — holds qsale Plane API key + secret
    aist.yaml                       # 600 — holds aist Plane API key + secret
```

`runtime.env`:

```bash
WEBHOOK_HOST=0.0.0.0
WEBHOOK_PORT=8000
CONDUCTOR_DIR=/etc/plane-conductor/conductor.d
LOG_DIR=/var/log/plane-conductor
MAX_CONCURRENT_SESSIONS=5
```

`conductor.d/qsale.yaml` (excerpted):

```yaml
workspace_slug: qsale
plane_base_url: https://plane.example.io
plane_api_key: plane_api_qsale_xxx
project_id: 11111111-...
initiator_uuid: 99999999-...
webhook_secret: <hex-32-bytes>
email_domain: example.io
prompts_dir: /home/you/Projects/qsale/.claude/agents
agents:
  - { nickname: castor, prompt_role: business-analyst }
  - { nickname: rinzler, prompt_role: python-developer }
  # ...
```

`conductor.d/aist.yaml` (excerpted):

```yaml
workspace_slug: aist
plane_base_url: https://plane.example.io
plane_api_key: plane_api_aist_xxx     # different API key
project_id: 22222222-...                # different project
initiator_uuid: 99999999-...
webhook_secret: <hex-32-bytes>          # different secret
email_domain: example.io
prompts_dir: /home/you/Projects/aist/.claude/agents
agents:
  - { nickname: brief, prompt_role: content-briefer }
  - { nickname: scribe, prompt_role: drafter }
```

In Plane, configure two webhooks:

- qsale workspace → `https://your-host/qsale/webhook` (secret = qsale's webhook_secret)
- aist workspace → `https://your-host/aist/webhook` (secret = aist's webhook_secret)

One process serves both. `MAX_CONCURRENT_SESSIONS` is the host-wide cap.

---

## Where the orchestrator looks at startup

```text
/etc/plane-conductor/runtime.env  ← system runtime config (loaded first)
/etc/plane-conductor/.env         ← legacy filename, still honoured
./runtime.env                     ← cwd runtime config (overrides)
./.env                            ← cwd fallback
process env                       ← overrides everything

$CONDUCTOR_DIR                    ← every *.yaml / *.yml inside is loaded as a workspace
```

On startup the loader validates:

- the directory exists and contains at least one workspace file
- every file's filename stem matches its `workspace_slug` (catches typos)
- slugs are unique across files

If anything required is missing or invalid, `plane-conductor serve` fails
fast with a pydantic ValidationError naming exactly what's wrong.
