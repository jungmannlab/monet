#!/usr/bin/env bash
# monet calibration server — one-shot setup for a dedicated systemd service.
#
# Creates a locked-down `monet` system user, installs monet into a venv under
# /opt/monet, keeps data (DB + log) under /var/lib/monet and the token file
# under /etc/monet, then installs and starts the systemd unit.
#
# Safe to re-run: it PRESERVES an existing token file and calibration DB, and
# just re-syncs code + unit. It does NOT touch your /root dev checkout or conda.
#
# Usage (as root):
#   sudo bash deploy/setup-server.sh
# Override any default via the environment, e.g.:
#   sudo GIT_REF=v0.4.0 MONET_HOST=127.0.0.1 bash deploy/setup-server.sh
set -euo pipefail

# ---- configuration (override via environment) -------------------------------
MONET_USER="${MONET_USER:-monet}"
APP_DIR="${APP_DIR:-/opt/monet}"              # venv + source clone
SRC_DIR="${SRC_DIR:-$APP_DIR/src}"
VENV_DIR="${VENV_DIR:-$APP_DIR/.venv}"
DATA_DIR="${DATA_DIR:-/var/lib/monet}"        # calibrations.db + monet.log (CWD)
ETC_DIR="${ETC_DIR:-/etc/monet}"              # monet.env (tokens)
ENV_FILE="${ENV_FILE:-$ETC_DIR/monet.env}"
DB_PATH="${DB_PATH:-$DATA_DIR/calibrations.db}"
REPO_URL="${REPO_URL:-https://github.com/jungmannlab/monet.git}"
GIT_REF="${GIT_REF:-feature/full-automation}"
MONET_HOST="${MONET_HOST:-0.0.0.0}"
MONET_PORT="${MONET_PORT:-8000}"
PYTHON="${PYTHON:-python3}"
UNIT="/etc/systemd/system/monet.service"

log() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo bash deploy/setup-server.sh)"; exit 1; }

# ---- 1. stop any existing service -------------------------------------------
if systemctl list-unit-files 2>/dev/null | grep -q '^monet\.service'; then
  log "stopping existing monet.service"
  systemctl stop monet 2>/dev/null || true
fi

# ---- 2. dedicated system user ------------------------------------------------
if id -u "$MONET_USER" >/dev/null 2>&1; then
  log "user $MONET_USER already exists"
else
  log "creating system user $MONET_USER"
  useradd --system --no-create-home --shell /usr/sbin/nologin "$MONET_USER"
fi

# ---- 3. directories ----------------------------------------------------------
log "ensuring $APP_DIR $DATA_DIR $ETC_DIR"
install -d -o "$MONET_USER" -g "$MONET_USER" -m 750 "$APP_DIR" "$DATA_DIR" "$ETC_DIR"

# ---- 4. source + venv (idempotent) ------------------------------------------
if [ -d "$SRC_DIR/.git" ]; then
  log "updating source in $SRC_DIR -> $GIT_REF"
  git -C "$SRC_DIR" fetch --all --tags --quiet
  git -C "$SRC_DIR" checkout --quiet "$GIT_REF"
  git -C "$SRC_DIR" pull --ff-only --quiet 2>/dev/null || true
else
  log "cloning $REPO_URL -> $SRC_DIR ($GIT_REF)"
  git clone --quiet "$REPO_URL" "$SRC_DIR"
  git -C "$SRC_DIR" checkout --quiet "$GIT_REF"
fi

if [ ! -x "$VENV_DIR/bin/python" ]; then
  log "creating venv $VENV_DIR"
  "$PYTHON" -m venv "$VENV_DIR"
fi
log "installing monet[server] into the venv"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -e "$SRC_DIR[server]"
chown -R "$MONET_USER:$MONET_USER" "$APP_DIR"

# ---- 5. token env file (preserve if it already has content) -----------------
if [ -s "$ENV_FILE" ]; then
  log "keeping existing token file $ENV_FILE"
else
  log "no tokens at $ENV_FILE — creating PAINT_MONET_AUTH=on + one write token"
  printf 'PAINT_MONET_AUTH=on\n' > "$ENV_FILE"
  sudo -u "$MONET_USER" "$VENV_DIR/bin/monet" token add \
       --scope write --label "$(hostname -s)" --env-file "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"
chown "$MONET_USER:$MONET_USER" "$ENV_FILE"

# ---- 6. hand the existing DB to the service user ----------------------------
[ -e "$DB_PATH" ] && chown "$MONET_USER:$MONET_USER" "$DB_PATH" || true

# ---- 7. systemd unit ---------------------------------------------------------
log "writing $UNIT"
cat > "$UNIT" <<UNITEOF
[Unit]
Description=monet calibration server (DB-only)
Documentation=https://github.com/jungmannlab/monet
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$MONET_USER
Group=$MONET_USER
# monet opens ./monet.log (relative) at import and may use \$HOME/.monet —
# keep CWD and HOME on a writable, service-owned dir.
WorkingDirectory=$DATA_DIR
Environment=HOME=$DATA_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$VENV_DIR/bin/monet serve --host $MONET_HOST --port $MONET_PORT --db-path $DB_PATH
Restart=on-failure
RestartSec=2
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
UNITEOF

# ---- 8. start ----------------------------------------------------------------
log "reloading + starting"
systemctl daemon-reload
systemctl reset-failed monet 2>/dev/null || true
systemctl enable --now monet
sleep 1
systemctl --no-pager --full status monet || true

echo
log "if healthy: curl -s http://localhost:$MONET_PORT/health   # -> {\"status\":\"ok\"} (200)"
log "logs:       journalctl -u monet -f    |    $DATA_DIR/monet.log"
