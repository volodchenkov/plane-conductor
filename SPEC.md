# Plane Conductor — Technical Specification

> **Role of this document:** technical specification (how the product is built).
> Authored by Zuse (Prompt Architect) for bootstrap; in steady state — owned by Sark (System Analyst).
> Reviewed by Flynn (Architect) before implementation begins.

---

## 1. High-level architecture

```
┌────────────┐  webhook  ┌─────────────────────────────────────────┐
│   Plane    │──────────▶│           Plane Conductor               │
│ (issues,   │           │  ┌────────┐  ┌────────┐   ┌──────────┐  │
│  comments) │           │  │ Server │─▶│ Router │──▶│ Runner   │  │
│            │           │  │(FastAPI│  │(parser,│   │(subproc, │  │
│            │ Plane API │  │ HMAC)  │  │ resolve│   │ session  │  │
│            │◀──────────│  │        │  │ member)│   │ registry)│  │
│            │  Plane    │  └────────┘  └────────┘   └──────────┘  │
│            │  Client   │       ▲                          │     │
│            │           │       │ /metrics, /health        │     │
│            │           │       │                          ▼     │
│            │           │  ┌────────┐                   ┌──────┐ │
│            │           │  │  CLI   │                   │ logs │ │
│            │           │  │ (setup,│                   │ dir  │ │
│            │           │  │ serve, │                   └──────┘ │
│            │           │  │ verify)│                            │
│            │           │  └────────┘                            │
│            │           └─────────────────────────────────────────┘
└────────────┘                              │
                                            │ spawn
                                            ▼
                                     ┌──────────────┐
                                     │  claude code │
                                     │  --agent X   │
                                     │  (subprocess)│
                                     └──────────────┘
```

Components:
1. **Server** — FastAPI app, single endpoint `POST /webhook`, HMAC verification.
2. **Router** — parses payload, extracts mentions, resolves to agent.
3. **Runner** — spawns Claude Code subprocess, captures logs, manages session registry.
4. **Plane Client** — async Plane REST API wrapper.
5. **CLI** — `setup` / `serve` / `verify`.
6. **Setup tools** — Python scripts in `setup/plane/` that bulk-create users, labels, states.

---

## 2. Stack

- **Python 3.11+** (target 3.11 minimum, 3.12 supported)
- **FastAPI** — webhook server. Async by default.
- **uvicorn** — ASGI server.
- **httpx** — async HTTP client for Plane API.
- **pydantic v2** — config (BaseSettings) + payload models.
- **typer** — CLI framework (more pythonic than argparse, less heavy than click).
- **structlog** — structured logging (JSON or pretty).
- **pytest** + **pytest-asyncio** — tests.
- **respx** — mocking httpx for Plane API tests.
- **ruff** — lint + format (replaces black + flake8 + isort).
- **mypy** — type checking, strict mode for `src/`.

No database. No Redis. In-memory session registry. If multi-instance becomes needed — switch to Redis later.

---

## 3. Project structure

```
plane-conductor/
├── README.md
├── LICENSE                              # MIT
├── CHANGELOG.md
├── CONTRIBUTING.md
├── CODE_OF_CONDUCT.md
├── pyproject.toml                       # PEP 621, ruff, mypy, pytest config
├── .python-version                      # 3.11
├── .gitignore
├── .env.example                         # template, no secrets
├── .pre-commit-config.yaml              # ruff, mypy hooks
│
├── .github/
│   ├── workflows/
│   │   ├── ci.yml                       # ruff, mypy, pytest, on push/PR
│   │   ├── publish.yml                  # PyPI release on tag
│   │   └── docs.yml                     # mkdocs deploy on main
│   ├── ISSUE_TEMPLATE/
│   │   ├── bug_report.md
│   │   └── feature_request.md
│   ├── PULL_REQUEST_TEMPLATE.md
│   └── dependabot.yml
│
├── docs/
│   ├── index.md                         # mkdocs landing
│   ├── installation.md
│   ├── configuration.md
│   ├── architecture.md                  # this SPEC, polished for users
│   ├── webhooks.md                      # Plane webhook setup walkthrough
│   ├── prompts.md                       # how the agent prompt directory is structured
│   └── examples/
│       └── qsale-deployment.md          # case study of QSale (anonymized if needed)
│
├── src/
│   └── plane_conductor/
│       ├── __init__.py                  # __version__
│       ├── __main__.py                  # python -m plane_conductor → cli
│       ├── cli.py                       # typer app: serve, setup, verify
│       ├── config.py                    # pydantic Settings
│       ├── server.py                    # FastAPI app factory
│       ├── webhook.py                   # POST /webhook handler, HMAC verify
│       ├── router.py                    # mention parsing, member resolution
│       ├── runner.py                    # subprocess spawn, session registry
│       ├── plane_client.py              # async Plane REST client
│       ├── models.py                    # pydantic event models
│       ├── logging_config.py            # structlog setup
│       └── exceptions.py                # custom exceptions
│
├── setup/
│   └── plane/
│       ├── __init__.py
│       ├── create_users.py              # invite 10 bot users
│       ├── create_labels.py             # artifact:* and role:* labels
│       ├── create_states.py             # optional Review, Blocked
│       ├── verify.py                    # smoke check
│       └── README.md
│
├── examples/
│   ├── docker-compose.yml               # quick local deploy
│   ├── systemd/
│   │   └── plane-conductor.service
│   └── nginx.conf                       # reverse proxy with TLS termination
│
├── tests/
│   ├── conftest.py                      # fixtures, env loading
│   ├── test_webhook.py                  # HMAC verification, payload parsing
│   ├── test_router.py                   # nickname mapping, fallback paths
│   ├── test_runner.py                   # subprocess mocking
│   ├── test_plane_client.py             # respx-based tests
│   ├── test_setup.py                    # setup scripts (mocked Plane API)
│   └── fixtures/
│       └── webhook_payloads/            # real Plane webhook samples
│
└── prompts/                             # OPTIONAL: ship example prompts here
    └── README.md                        # explains how to point at your own prompts dir
```

---

## 4. Data models

### Plane webhook event (incoming)

```python
class PlaneEvent(BaseModel):
    event: Literal["issue", "issue_comment"]
    action: Literal["created", "updated"]
    data: dict  # raw payload, parsed by event-specific submodels
    workspace: str  # workspace slug

class IssueCommentEvent(PlaneEvent):
    event: Literal["issue_comment"]
    data: IssueCommentData

class IssueCommentData(BaseModel):
    id: str           # comment UUID
    issue: str        # issue UUID
    project: str      # project UUID
    actor: str        # user UUID who posted the comment
    comment_html: str
    comment_stripped: str
    created_at: datetime
    # Plane embeds mentions in comment_html as <mention-component entity_identifier="<UUID>"/>
```

### Resolved mention

```python
class ResolvedMention(BaseModel):
    member_id: UUID                      # Plane member UUID
    nickname: str                        # email local part: "sark"
    prompt_role: str                     # mapped role: "system-analyst"
    issue_identifier: str                # "QSALE-42"
    issue_uuid: UUID
    triggered_by: UUID                   # Dmitry's UUID
    triggered_at: datetime
```

### Session registry entry

```python
class AgentSession(BaseModel):
    session_id: UUID                     # generated locally
    nickname: str
    issue_identifier: str
    pid: int
    started_at: datetime
    log_path: Path
    status: Literal["running", "completed", "failed", "timeout"]
    exit_code: int | None
```

---

## 5. Key flows

### 5.1 Webhook receipt → agent run

```
1. POST /webhook arrives.
2. webhook.py: verify HMAC of body using shared secret. 401 if mismatch.
3. Parse JSON into PlaneEvent. Discard non-comment events (early return 200).
4. router.py: parse comment_html, extract <mention-component entity_identifier="..."/>
   tags. For each UUID:
   a. Skip if UUID == initiator (Dmitry's own UUID — he can mention himself).
   b. Call plane_client.get_member(uuid) → email.
   c. Compute nickname = email.split("@")[0].
   d. Look up nickname in NICKNAME_TO_PROMPT (config-loaded).
   e. If unknown nickname — log warning, skip (don't fail the whole webhook).
   f. Build ResolvedMention.
5. runner.py: for each resolved mention:
   a. Check session registry: is (nickname, issue_identifier) already running?
      If yes — log "duplicate trigger ignored", skip.
   b. Otherwise — spawn subprocess:
      claude --agent <nickname> --print <prompt-from-template>
      Where the prompt template injects issue_identifier as context.
   c. Register session in registry.
   d. Stream stdout/stderr to log_path.
   e. On exit: update registry status, post a comment to Plane if exit != 0
      ("Agent <nickname> failed: see logs at <relative-path>").
6. Return 200 to Plane.
```

### 5.2 Setup flow

```
1. User clones repo, fills .env (PLANE_BASE_URL, PLANE_API_KEY, WORKSPACE_SLUG,
   EMAIL_DOMAIN, WEBHOOK_SECRET).
2. plane-conductor setup:
   a. Verify connectivity (call /api/v1/workspaces/<slug>/).
   b. Read 10-row roster from a built-in YAML (matches plane-api.md §3).
   c. For each row: invite member via POST /api/v1/workspaces/<slug>/invite/
      with email constructed from nickname + EMAIL_DOMAIN. If already exists — skip.
   d. For each artifact and role label: POST /api/v1/workspaces/<slug>/projects/<pid>/labels/.
      Idempotent (check existing labels first).
   e. (Optional flag --states) Create Review, Blocked states.
   f. Print resulting member UUIDs to stdout (user copies into plane-config.local.md).
3. plane-conductor verify: reads Plane, confirms 10 bot users present, 18 labels,
   correct project access. Returns exit 0/1.
```

### 5.3 Subprocess invocation contract

```
claude --agent <nickname> --print
```

The prompt content passed to stdin (or as positional arg) includes:
- `Issue: QSALE-42`
- `Triggered by: <member email>`
- `Plane URL: https://plane.suze.io/qsale/projects/.../issues/<uuid>/`

Inside Claude Code, the agent prompt (e.g. `python-developer.md`) defines its own re-entry logic (read root issue → check own sub-issue exists → continuation/rework/first-run, see plane-api.md §7). Plane Conductor doesn't manage agent state — it only spawns and logs.

---

## 6. Configuration

`.env` example:

```bash
# Plane connection
PLANE_BASE_URL=https://plane.suze.io
PLANE_API_KEY=plane_api_xxxxxxxxxxxxx
PLANE_WORKSPACE_SLUG=qsale
PLANE_PROJECT_ID=ba6d77f4-5086-4a35-8cef-a4e45111e91f

# Webhook
WEBHOOK_SECRET=use-openssl-rand-hex-32-here
WEBHOOK_HOST=0.0.0.0
WEBHOOK_PORT=8000

# Agent invocation
EMAIL_DOMAIN=qsale.io                         # used to construct bot emails: <nick>@qsale.io
PROMPTS_DIR=/home/user/Projects/qsale/.claude/agents
INITIATOR_UUID=d3ee78fe-dfe4-4cbe-a60f-bc24b18f2f92  # Dmitry, ignored as mention target

# Operations
LOG_DIR=/var/log/plane-conductor
LOG_LEVEL=INFO
MAX_CONCURRENT_SESSIONS=5
SESSION_TIMEOUT_SECONDS=3600

# Optional Object Storage for screenshots (used by ui-tester agent, not by Conductor itself)
S3_BUCKET=plane-data-private
S3_ENDPOINT=https://storage.yandexcloud.net
```

---

## 7. CLI

Built with `typer`:

```
plane-conductor serve [--host HOST] [--port PORT]
plane-conductor setup [--states] [--dry-run]
plane-conductor verify
plane-conductor sessions  # list active sessions from registry
plane-conductor logs <session-id>  # tail or display log file
```

---

## 8. Testing strategy

- **Unit tests** for `router.py` (mention parsing, mapping). No I/O.
- **Unit tests** for `runner.py` with subprocess mocking.
- **Integration tests** for `webhook.py` using FastAPI TestClient and signed requests.
- **Integration tests** for `plane_client.py` using respx.
- **End-to-end tests** with real Plane (gated by env var `PLANE_E2E=1`, run only locally / nightly).
- Coverage target: **>70%**, excluding `__main__.py` and `cli.py` glue.

---

## 9. Deployment

### 9.1 Local / single-server

```bash
pip install plane-conductor
cp .env.example .env  # edit
plane-conductor setup
plane-conductor serve
```

### 9.2 Docker

```yaml
# examples/docker-compose.yml
version: "3.9"
services:
  plane-conductor:
    image: ghcr.io/dmitry/plane-conductor:latest
    env_file: .env
    ports:
      - "8000:8000"
    volumes:
      - ./logs:/var/log/plane-conductor
      - ./prompts:/prompts:ro
    restart: unless-stopped
```

### 9.3 Behind nginx

```nginx
# examples/nginx.conf
server {
    server_name conductor.suze.io;
    listen 443 ssl;
    location /webhook {
        proxy_pass http://localhost:8000;
        proxy_set_header X-Plane-Signature $http_x_plane_signature;
    }
}
```

---

## 10. Branding & docs

For the public repo:
- **README.md**: hero section with one-liner, animated GIF or screenshot of a Plane issue with `@rinzler` triggering an agent run, badges (PyPI, Python versions, CI, license, codecov), 30-second quickstart, link to docs.
- **mkdocs-material** for docs site (deployed to GitHub Pages on push to main).
- **Architecture diagram** as Mermaid in README + docs/architecture.md.
- **CHANGELOG.md** following Keep a Changelog format. Conventional Commits in git history.
- **CONTRIBUTING.md**: dev setup, lint commands, PR template, issue triage flow.
- **CODE_OF_CONDUCT.md**: Contributor Covenant 2.1.
- **LICENSE**: MIT.

---

## 11. Out of scope (must be explicitly declared)

- See REQUIREMENTS.md §7. No web UI, no multi-tenancy, no DB, no Linear/Jira adapters in v0.1.
- No automated agent-to-agent triggering. The initiator (human) is the only one who mentions agents.

---

## 12. Open questions for the implementer

These can be resolved during implementation, with reasonable defaults:

1. **Webhook payload exact schema** — Plane's webhook documentation is incomplete on self-hosted versions. The implementer should send a real test webhook and capture the actual payload, save to `tests/fixtures/webhook_payloads/`.
2. **Plane invite API behavior** — does it support inviting non-existent emails (creating new accounts) or only existing? On self-hosted Plane, this likely depends on `ALLOW_NEW_USER_REGISTRATION` env var. If invites can't auto-create, fallback: create accounts via direct DB seeding script (out of scope for v0.1; document the manual UI invite step).
3. **MCP plane server config in subprocess** — the spawned `claude --agent ...` subprocess must inherit the MCP plane server config from the user's `~/.claude.json`. Verify this works; if not, propagate via `--mcp-config` flag.
4. **Idempotency of setup script** — Plane API may not have proper `409 Conflict` for duplicates. Implement client-side check (list existing → skip).

These are documented as TODOs in code, not as blockers for v0.1.
