# Installation

Three ways to run Plane Conductor:

1. [**Local development**](#local-development) — fastest path, runs in
   your shell. Use this for trying things out and for tests.
2. [**Production: systemd**](#production-systemd) — what you actually
   want on the host that runs your agents day-to-day. One install
   script, idempotent.
3. [**Production: Docker**](#production-docker) — containerised, useful
   if you have a Docker host but no systemd.

Whichever path you choose, you also need to make Plane reach the
service from outside — see [Exposing the webhook](#exposing-the-webhook).

---

## Prerequisites

- **Python 3.11+** on the host that will run Plane Conductor.
- **`claude` CLI** (Claude Code) installed and on `PATH` for the user
  the service runs as. Plane Conductor invokes it as
  `claude --agent <nick> --print`.
- **`~/.claude.json`** configured for that same user — particularly an
  `mcpServers.plane` entry pointing at a Plane MCP server (e.g.
  [`makeplane/plane-mcp-server`](https://github.com/makeplane/plane-mcp-server)).
  This is what lets your agents read/write Plane.
- **Plane workspace** (cloud or self-hosted) with an API key, a project
  to work in, and your own Plane member UUID for the `INITIATOR_UUID`
  setting (so the orchestrator never tries to spawn an agent for you).
- **Agent prompts and skills** in your local `PROMPTS_DIR`. Plane
  Conductor itself ships none — it just spawns whatever Claude Code
  finds. See [`prompts/README.md`](../prompts/README.md) for the
  expected file layout.

---

## Local development

```bash
git clone https://github.com/volodchenkov/plane-conductor.git
cd plane-conductor

python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]

# Settings (runtime).
cp .env.example .env
# Fill at minimum:
#   PLANE_BASE_URL, PLANE_API_KEY, PLANE_WORKSPACE_SLUG, PLANE_PROJECT_ID
#   WEBHOOK_SECRET=$(openssl rand -hex 32)
#   EMAIL_DOMAIN, PROMPTS_DIR, AGENT_WORKING_DIR, INITIATOR_UUID
#   CONDUCTOR_CONFIG=./conductor.yaml

# Workflow (agents/labels/states).
cp examples/sdlc-conductor.yaml conductor.yaml
# Or examples/minimal-conductor.yaml for a 1-agent setup.

# Smoke checks before spawning anything.
plane-conductor verify       # connectivity + roster sanity
plane-conductor setup        # invite bot accounts + create labels (idempotent)

# Run.
plane-conductor serve        # binds to WEBHOOK_HOST:WEBHOOK_PORT
```

In a second terminal you can watch what's happening:

```bash
plane-conductor agents       # print configured nickname → role map
ls logs/                     # per-run log files
tail -f logs/<file>.log
```

The full configuration reference (every `.env` variable + every YAML
field) is in [`configuration.md`](configuration.md).

---

## Production: systemd

The repo ships with an idempotent installer that does everything
expected for a systemd unit on a Linux host: creates a venv at a
prefix, lays down a config skeleton, sets up logrotate, installs the
unit file, runs it under your own user by default (so the spawned
agents see your `~/.claude.json` and `PROMPTS_DIR`).

```bash
git clone https://github.com/volodchenkov/plane-conductor.git
cd plane-conductor

sudo bash setup/install.sh
sudoedit /etc/plane-conductor/.env             # secrets, ports, paths
sudoedit /etc/plane-conductor/conductor.yaml   # agents, labels, states

# (Once) provision the Plane workspace.
sudo -u "$USER" /opt/plane-conductor/.venv/bin/plane-conductor verify
sudo -u "$USER" /opt/plane-conductor/.venv/bin/plane-conductor setup

# Boot it.
sudo systemctl enable --now plane-conductor
journalctl -u plane-conductor -f
```

### `install.sh` flags

```text
sudo bash setup/install.sh                       # use $SUDO_USER (default)
sudo bash setup/install.sh --user alice          # use a specific existing user
sudo bash setup/install.sh --system-user         # create a hardened `conductor` user
sudo bash setup/install.sh --prefix /srv/conductor
sudo bash setup/install.sh --uninstall           # remove unit + logrotate
```

Default mode runs the service as `$SUDO_USER`. That's what you want on
a **single-developer machine**: the spawned `claude` processes need to
read your `~/.claude.json` and write into your project trees, which
only works if the service runs as you. `--system-user` is for
multi-tenant / shared boxes where you'd rather isolate.

### File layout after install

```
/opt/plane-conductor/         # source + venv (rsync'd from the repo, editable install)
/etc/plane-conductor/
  .env                         # 640, root:<your-group>
  conductor.yaml               # 640, root:<your-group>
/var/log/plane-conductor/      # per-run agent logs + .active/ sentinels
/etc/systemd/system/plane-conductor.service
/etc/logrotate.d/plane-conductor   # daily, 14 days, gzip
```

To upgrade the code without touching config:

```bash
cd /home/you/Projects/plane-conductor && git pull
sudo bash setup/install.sh                      # idempotent — re-syncs source, leaves config alone
sudo systemctl restart plane-conductor
```

---

## Production: Docker

```bash
git clone https://github.com/volodchenkov/plane-conductor.git
cd plane-conductor

cp .env.example .env             # fill in
cp examples/sdlc-conductor.yaml conductor.yaml

# Build + run via the example compose file:
docker compose -f examples/docker-compose.yml up -d
docker compose -f examples/docker-compose.yml logs -f
```

Caveat: in Docker your spawned `claude` runs *inside the container*. It
won't see your host `~/.claude.json` or your local repos unless you
mount them in. For most setups the **systemd path** is simpler — the
container path makes sense if you're already shipping things via Docker
on a server you don't develop on.

---

## Exposing the webhook

Plane needs to reach `https://your-host/webhook`. Three common ways:

### (a) Cloudflare Tunnel — recommended for laptop / home setup

```bash
# One-time:
cloudflared tunnel login                    # opens browser, picks your zone
cloudflared tunnel create plane-conductor
cloudflared tunnel route dns plane-conductor pc.your-domain.dev

# Persistent config at ~/.cloudflared/config.yml:
#   tunnel: <UUID>
#   credentials-file: ~/.cloudflared/<UUID>.json
#   ingress:
#     - hostname: pc.your-domain.dev
#       service: http://localhost:8000
#     - service: http_status:404

# Install as a systemd service (or use the cloudflared install command):
sudo cloudflared service install
```

After that `https://pc.your-domain.dev/webhook` is your stable URL.

### (b) Reverse proxy on a real server

If you already have nginx/Caddy/Traefik fronting your apps, point one
hostname at `localhost:8000`. The shipped
[`examples/nginx.conf`](../examples/nginx.conf) is a starting point.

### (c) Quick tunnel for a one-shot test

```bash
cloudflared tunnel --url http://localhost:8000
# Prints a temporary trycloudflare.com URL. Dies when the process stops.
```

Useful for "just let me see if my mention works once" — not for ongoing
use.

---

## Configure Plane to send the webhook

In Plane → **Workspace settings → Webhooks → Add webhook**:

- **URL**: `https://your-host/webhook`
- **Secret**: copy the value from `WEBHOOK_SECRET` in your `.env`
- **Events**: at minimum `Issue Comment`

Save. Plane will sign each webhook body with your secret using
HMAC-SHA256, and Plane Conductor verifies it before doing anything.

---

## Provisioning bot accounts

Plane Conductor doesn't create user accounts (the public Plane API
deliberately can't). The `setup` command sends *invitations*; the
underlying users have to exist or be allowed to sign up.

If you're on a self-hosted Plane and need to bootstrap fresh bot
accounts in one shot, see the cookbook in
[`docs/internals/`](internals/) for the signup-then-accept flow we
used. Cloud Plane users invite real teammates the normal way.

---

## Verifying everything works

```bash
# 1. Service is alive.
curl https://your-host/health
# → {"status":"ok","version":"0.1.0"}

# 2. Mention a configured agent in any Plane issue comment.
# 3. Watch the journal:
journalctl -u plane-conductor -f
# → "POST /webhook 200"
# → "agent_spawned nickname=<nick> issue=<uuid>"
# → … (agent works) …
# → "agent_exited exit_code=0 duration_s=N"
```

If you don't see the POST coming in: check the Plane webhook delivery
log in the UI (it'll show 4xx/5xx if the secret is wrong or the URL
is unreachable), and check that your tunnel/proxy is up.
