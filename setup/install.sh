#!/usr/bin/env bash
#
# Plane Conductor — systemd installer
# -----------------------------------
# Idempotent. Runs the service as YOUR user by default — that's what a
# single-developer setup needs (the agents must see ~/.claude.json, your
# PROMPTS_DIR, and write into your project trees as you).
#
# Usage:
#   sudo bash setup/install.sh                         # use $SUDO_USER (recommended)
#   sudo bash setup/install.sh --user alice            # use an existing user
#   sudo bash setup/install.sh --system-user           # create dedicated 'conductor' user (multi-tenant / hardened)
#   sudo bash setup/install.sh --prefix /srv/conductor # custom install path
#   sudo bash setup/install.sh --uninstall             # remove
#
# Steps (idempotent):
#   1. Pick service user (your user or system 'conductor' on --system-user)
#   2. Sync source + venv to <prefix>
#   3. Drop /etc/plane-conductor/runtime.env skeleton if absent
#   4. Set up /var/log/plane-conductor with logrotate
#   5. Install the systemd unit
#   6. Print next steps (does NOT auto-start until you edit runtime.env)

set -euo pipefail

PREFIX="/opt/plane-conductor"
CONFIG_DIR="/etc/plane-conductor"
WORKSPACES_DIR="/etc/plane-conductor/conductor.d"
LOG_DIR="/var/log/plane-conductor"
SYSTEMD_UNIT="/etc/systemd/system/plane-conductor.service"
LOGROTATE_CONF="/etc/logrotate.d/plane-conductor"
ACTION="install"
SERVICE_USER=""
USER_MODE="auto"   # auto | explicit | system

log() { printf '\033[1;32m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix) PREFIX="$2"; shift 2 ;;
        --user) SERVICE_USER="$2"; USER_MODE="explicit"; shift 2 ;;
        --system-user) USER_MODE="system"; shift ;;
        --uninstall) ACTION="uninstall"; shift ;;
        --help|-h)
            sed -n '2,21p' "$0"
            exit 0
            ;;
        *) err "unknown arg: $1" ;;
    esac
done

[[ $EUID -eq 0 ]] || err "must run as root (sudo)"
command -v systemctl >/dev/null || err "systemd not found — this installer is for systemd hosts"

# ---------------------------------------------------------------------------
# Locate source tree
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
[[ -f "${SRC_DIR}/pyproject.toml" ]] || err "pyproject.toml not found at ${SRC_DIR}"

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------
if [[ "$ACTION" == "uninstall" ]]; then
    log "stopping & disabling service (if present)"
    systemctl stop plane-conductor.service 2>/dev/null || true
    systemctl disable plane-conductor.service 2>/dev/null || true
    rm -f "$SYSTEMD_UNIT"
    systemctl daemon-reload
    rm -f "$LOGROTATE_CONF"
    log "removed unit + logrotate. Leaving ${PREFIX}, ${CONFIG_DIR}, ${LOG_DIR} in place."
    log "to fully clean up: rm -rf ${PREFIX} ${CONFIG_DIR} ${LOG_DIR}"
    exit 0
fi

# ---------------------------------------------------------------------------
# 1. Choose the service user
# ---------------------------------------------------------------------------
if [[ "$USER_MODE" == "system" ]]; then
    SERVICE_USER="conductor"
    if ! getent group "$SERVICE_USER" >/dev/null; then
        log "creating system group: $SERVICE_USER"
        groupadd --system "$SERVICE_USER"
    fi
    if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
        log "creating system user: $SERVICE_USER"
        useradd --system --gid "$SERVICE_USER" --home-dir "$PREFIX" \
            --shell /usr/sbin/nologin "$SERVICE_USER"
    fi
    HARDEN_HOME=1
elif [[ "$USER_MODE" == "auto" ]]; then
    if [[ -n "${SUDO_USER:-}" ]] && [[ "$SUDO_USER" != "root" ]]; then
        SERVICE_USER="$SUDO_USER"
        log "using your user: $SERVICE_USER (override with --user / --system-user)"
    else
        err "could not detect sudo user; pass --user <name> or --system-user"
    fi
    HARDEN_HOME=0
else
    # explicit --user
    id -u "$SERVICE_USER" >/dev/null 2>&1 || err "user '$SERVICE_USER' does not exist"
    HARDEN_HOME=0
fi

SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
SERVICE_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
log "service will run as: ${SERVICE_USER}:${SERVICE_GROUP} (home: ${SERVICE_HOME})"

# ---------------------------------------------------------------------------
# 2. Source + venv
# ---------------------------------------------------------------------------
log "syncing source → ${PREFIX}"
mkdir -p "$PREFIX"
if command -v rsync >/dev/null; then
    rsync -a --delete \
        --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
        --exclude='*.pyc' --exclude='.pytest_cache' --exclude='.mypy_cache' \
        --exclude='.ruff_cache' --exclude='logs' \
        --exclude='.env' --exclude='.env.local' \
        "${SRC_DIR}/" "${PREFIX}/"
else
    warn "rsync not found, using cp"
    cp -a "${SRC_DIR}/." "${PREFIX}/"
    rm -rf "${PREFIX}/.git" "${PREFIX}/.venv" "${PREFIX}/.pytest_cache" \
        "${PREFIX}/.mypy_cache" "${PREFIX}/.ruff_cache" "${PREFIX}/logs"
fi

PY_BIN="$(command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3 || true)"
[[ -n "$PY_BIN" ]] || err "python3 (>=3.11) not found"
log "using python: $PY_BIN ($($PY_BIN --version 2>&1))"

if [[ ! -x "${PREFIX}/.venv/bin/python" ]]; then
    log "creating venv at ${PREFIX}/.venv"
    "$PY_BIN" -m venv "${PREFIX}/.venv"
fi
log "installing plane-conductor (editable)"
"${PREFIX}/.venv/bin/pip" install --upgrade pip --quiet
# Editable install: source changes are picked up on `systemctl restart`,
# no pip reinstall needed for code-only updates.
"${PREFIX}/.venv/bin/pip" install --quiet -e "${PREFIX}"

# ---------------------------------------------------------------------------
# 3. Config dir + runtime.env + conductor.d skeletons
# ---------------------------------------------------------------------------
mkdir -p "$CONFIG_DIR"
chmod 750 "$CONFIG_DIR"
chown "root:${SERVICE_GROUP}" "$CONFIG_DIR"

if [[ ! -f "${CONFIG_DIR}/runtime.env" ]]; then
    log "writing runtime.env skeleton → ${CONFIG_DIR}/runtime.env"
    cp "${PREFIX}/examples/runtime.env.example" "${CONFIG_DIR}/runtime.env"
    chmod 640 "${CONFIG_DIR}/runtime.env"
    chown "root:${SERVICE_GROUP}" "${CONFIG_DIR}/runtime.env"
    EDIT_NEEDED=1
else
    log "${CONFIG_DIR}/runtime.env already exists — leaving as-is"
    EDIT_NEEDED=0
fi

mkdir -p "$WORKSPACES_DIR"
chmod 750 "$WORKSPACES_DIR"
chown "root:${SERVICE_GROUP}" "$WORKSPACES_DIR"

# Drop a starter SDLC workspace if the directory is empty. User renames the
# file to match their actual workspace_slug, edits secrets/creds, chmod 600.
shopt -s nullglob
_existing_ws=("$WORKSPACES_DIR"/*.yaml "$WORKSPACES_DIR"/*.yml)
shopt -u nullglob
if [[ ${#_existing_ws[@]} -eq 0 ]]; then
    log "writing workspace skeleton → ${WORKSPACES_DIR}/sdlc.yaml (rename to match your workspace slug)"
    cp "${PREFIX}/examples/conductor.d/sdlc.yaml" "${WORKSPACES_DIR}/sdlc.yaml"
    chmod 600 "${WORKSPACES_DIR}/sdlc.yaml"
    chown "root:${SERVICE_GROUP}" "${WORKSPACES_DIR}/sdlc.yaml"
    EDIT_NEEDED=1
else
    log "${WORKSPACES_DIR}/ already has workspace configs — leaving as-is"
fi

# ---------------------------------------------------------------------------
# 4. Log directory + rotation
# ---------------------------------------------------------------------------
mkdir -p "$LOG_DIR"
chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "$LOG_DIR"
chmod 750 "$LOG_DIR"

if [[ ! -f "$LOGROTATE_CONF" ]]; then
    log "installing logrotate config → ${LOGROTATE_CONF}"
    cat > "$LOGROTATE_CONF" <<EOF
${LOG_DIR}/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0640 ${SERVICE_USER} ${SERVICE_GROUP}
    sharedscripts
}
EOF
fi

# ---------------------------------------------------------------------------
# 5. Filesystem ownership of installed code
# ---------------------------------------------------------------------------
chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "$PREFIX"

# ---------------------------------------------------------------------------
# 6. systemd unit
# ---------------------------------------------------------------------------
log "installing systemd unit → ${SYSTEMD_UNIT}"

# Hardening — only enable ProtectHome in --system-user mode.
# When running as a real user, agents need to read $PROMPTS_DIR and write into
# the user's project trees under /home, so ProtectHome would break them.
HARDENING="NoNewPrivileges=yes
ProtectSystem=strict
PrivateTmp=yes
ReadWritePaths=${LOG_DIR}"
if [[ $HARDEN_HOME -eq 1 ]]; then
    HARDENING="${HARDENING}
ProtectHome=read-only"
fi

cat > "$SYSTEMD_UNIT" <<EOF
[Unit]
Description=Plane Conductor — Claude Code agent orchestrator
Documentation=https://github.com/volodchenkov/plane-conductor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=${PREFIX}
EnvironmentFile=${CONFIG_DIR}/runtime.env
EnvironmentFile=-${CONFIG_DIR}/.env
ExecStart=${PREFIX}/.venv/bin/plane-conductor serve
Restart=on-failure
RestartSec=5s

# Match the orchestrator's SHUTDOWN_GRACE_SECONDS + a margin.
TimeoutStopSec=60
KillMode=mixed
KillSignal=SIGTERM

# Hardening
${HARDENING}

# Logs go to journal; per-run log files still live in ${LOG_DIR}.
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload

# ---------------------------------------------------------------------------
# 7. Sanity warnings
# ---------------------------------------------------------------------------
if ! sudo -u "$SERVICE_USER" -- bash -lc 'command -v claude' >/dev/null 2>&1; then
    warn "the 'claude' CLI is not on the PATH for user '$SERVICE_USER'."
    warn "install Claude Code or set CLAUDE_BINARY=/full/path in ${CONFIG_DIR}/runtime.env"
fi

if [[ -n "${SERVICE_HOME}" ]] && [[ ! -f "${SERVICE_HOME}/.claude.json" ]]; then
    warn "no ${SERVICE_HOME}/.claude.json — agents need MCP server config there."
    warn "as ${SERVICE_USER}, run: claude mcp add plane --env ..."
fi

# ---------------------------------------------------------------------------
# 8. Done
# ---------------------------------------------------------------------------
log "installation complete."
echo
if [[ $EDIT_NEEDED -eq 1 ]]; then
    cat <<EOF
Next steps:
  1. Edit host-wide runtime config (port, log dir, capacity):
       sudoedit ${CONFIG_DIR}/runtime.env
  2. Configure your workspace(s). One YAML per workspace, named <slug>.yaml.
     Each file holds Plane creds + agents + secrets for ONE workspace.
     Rename the seeded sdlc.yaml to match your real workspace slug, then edit:
       sudo mv ${WORKSPACES_DIR}/sdlc.yaml ${WORKSPACES_DIR}/<your-slug>.yaml
       sudoedit ${WORKSPACES_DIR}/<your-slug>.yaml
     Add more workspaces by dropping more files into ${WORKSPACES_DIR}/.
  3. (Once per workspace) run setup against Plane:
       sudo -u ${SERVICE_USER} ${PREFIX}/.venv/bin/plane-conductor verify
       sudo -u ${SERVICE_USER} ${PREFIX}/.venv/bin/plane-conductor setup
     (use --workspace <slug> to scope to one)
  4. Start the service:
       sudo systemctl enable --now plane-conductor
  5. Tail the logs:
       journalctl -u plane-conductor -f
  6. Point Plane webhooks at https://<your-host>/<workspace-slug>/webhook
     (one webhook per workspace in Plane's settings)
EOF
else
    cat <<EOF
Config already in place. To pick up the new code:
  sudo systemctl restart plane-conductor

Status: sudo systemctl status plane-conductor
Logs:   journalctl -u plane-conductor -f
EOF
fi

echo
echo "Uninstall: sudo bash ${SCRIPT_DIR}/install.sh --uninstall"
