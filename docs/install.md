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
- **Plane workspace(s)** (cloud or self-hosted) — one or more. Each one
  needs an API key, a project to work in, and your own Plane member
  UUID for the workspace's `initiator_uuid` (so the orchestrator never
  tries to spawn an agent for you).
- **Agent prompts** in each workspace's `prompts_dir`. Plane Conductor
  itself ships none — it just spawns whatever Claude Code finds.

---

## Local development

```bash
git clone https://github.com/volodchenkov/plane-conductor.git
cd plane-conductor

python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]

# Host-wide runtime config.
cp examples/runtime.env.example runtime.env
# Adjust WEBHOOK_PORT, LOG_DIR, MAX_CONCURRENT_SESSIONS, CONDUCTOR_DIR if needed.

# One YAML per workspace. Pick a starter and edit:
mkdir -p conductor.d
cp examples/conductor.d/minimal.yaml conductor.d/myws.yaml
# Then edit conductor.d/myws.yaml — fill in:
#   workspace_slug (must match the filename stem!), plane_base_url,
#   plane_api_key, project_id, initiator_uuid, webhook_secret
#   (openssl rand -hex 32), email_domain, prompts_dir.
# chmod 600 conductor.d/myws.yaml   (it has secrets)

# Tell the orchestrator where conductor.d/ is (cwd-relative for dev):
export CONDUCTOR_DIR=$(pwd)/conductor.d

# Smoke checks before spawning anything.
plane-conductor verify       # connectivity + roster sanity (all workspaces)
plane-conductor verify --workspace myws   # or just one
plane-conductor setup        # invite bots + create labels (all workspaces)

# Run.
plane-conductor serve        # binds to WEBHOOK_HOST:WEBHOOK_PORT
```

In a second terminal:

```bash
plane-conductor agents       # print configured nickname → role for every workspace
ls logs/                     # per-run log files (carry the workspace slug)
tail -f logs/<file>.log
curl http://localhost:8000/health
# {"status":"ok","version":"...","workspaces":["myws"]}
```

The full configuration reference (every env var + every YAML field) is
in [`configuration.md`](configuration.md).

---

## Production: systemd

The repo ships with an idempotent installer that does everything
expected for a systemd unit on a Linux host: creates a venv at a
prefix, lays down a config skeleton, sets up logrotate, installs the
unit file, runs it under your own user by default (so the spawned
agents see your `~/.claude.json` and your project trees).

```bash
git clone https://github.com/volodchenkov/plane-conductor.git
cd plane-conductor

sudo bash setup/install.sh
sudoedit /etc/plane-conductor/runtime.env             # host-wide runtime

# The installer drops a starter sdlc.yaml into conductor.d/. Rename it
# to match your real workspace slug, then edit. Add more files for more
# workspaces.
sudo mv /etc/plane-conductor/conductor.d/sdlc.yaml \
        /etc/plane-conductor/conductor.d/<your-slug>.yaml
sudoedit /etc/plane-conductor/conductor.d/<your-slug>.yaml

# (Once per workspace) provision Plane.
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
  runtime.env                  # 640, root:<your-group>
  conductor.d/                 # 750, root:<your-group>
    qsale.yaml                 # 600 — holds Plane API key + webhook secret
    aist.yaml                  # 600 — second workspace
/var/log/plane-conductor/      # per-run agent logs + .active/ sentinels (slug-prefixed)
/etc/systemd/system/plane-conductor.service
/etc/logrotate.d/plane-conductor   # daily, 14 days, gzip
```

To upgrade the code without touching config:

```bash
cd /home/you/Projects/plane-conductor && git pull
sudo bash setup/install.sh                      # idempotent — re-syncs source, leaves config alone
sudo systemctl restart plane-conductor
```

To add a new workspace later: drop another file into
`/etc/plane-conductor/conductor.d/<new-slug>.yaml`, restart the service,
point Plane at `https://<host>/<new-slug>/webhook`. No code changes.

---

## Production: Docker

```bash
git clone https://github.com/volodchenkov/plane-conductor.git
cd plane-conductor

cp examples/runtime.env.example runtime.env       # fill in
mkdir -p conductor.d
cp examples/conductor.d/sdlc.yaml conductor.d/myws.yaml   # rename to your slug, edit

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

Plane needs to reach `https://your-host/<workspace-slug>/webhook`. Three
common ways:

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

# Install as a systemd service:
sudo cloudflared service install
```

After that `https://pc.your-domain.dev/<slug>/webhook` is your stable URL
for each workspace.

### (b) Reverse proxy on a real server

If you already have nginx/Caddy/Traefik fronting your apps, point one
hostname at `localhost:8000`. The shipped
[`examples/nginx.conf`](../examples/nginx.conf) is a starting point.

### (c) Quick tunnel for a one-shot test

```bash
cloudflared tunnel --url http://localhost:8000
# Prints a temporary trycloudflare.com URL. Dies when the process stops.
```

---

## Configure Plane to send the webhook

For each workspace you've added to `conductor.d/`, in Plane → **Workspace
settings → Webhooks → Add webhook**:

- **URL**: `https://your-host/<workspace_slug>/webhook` (the slug must
  match the workspace's `workspace_slug` in `conductor.d/<slug>.yaml`)
- **Secret**: copy the value of `webhook_secret` from that workspace's YAML
- **Events**: at minimum `Issue Comment`

Save. Plane signs each webhook body with that secret using HMAC-SHA256;
Plane Conductor verifies it before doing anything. Each workspace has
its own secret — leaks are isolated.

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
# 1. Service is alive (and lists every loaded workspace).
curl https://your-host/health
# → {"status":"ok","version":"...","workspaces":["qsale","aist"]}

# 2. Mention a configured agent in any Plane issue comment.
# 3. Watch the journal:
journalctl -u plane-conductor -f
# → "POST /qsale/webhook 200"
# → "agent_spawned workspace=qsale nickname=<nick> issue=<uuid>"
# → … (agent works) …
# → "agent_exited workspace=qsale exit_code=0 duration_s=N"
```

If you don't see the POST coming in: check the Plane webhook delivery
log in the UI (it'll show 4xx/5xx if the secret is wrong, the URL is
wrong, or the workspace slug doesn't match a file in `conductor.d/`),
and check that your tunnel/proxy is up.
