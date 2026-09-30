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

# Run a command as the service user, with HOME on a writable, service-owned dir
# (git/pip write config/cache there). Used for git and pip so they act as the
# repo/venv owner — otherwise git's "dubious ownership" guard blocks root from
# operating on the monet-owned /opt/monet checkout.
as_monet() { sudo -u "$MONET_USER" env "HOME=$APP_DIR" "$@"; }

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
# git and pip run AS THE monet USER so a monet-owned checkout doesn't trip git's
# "dubious ownership" guard (root operating on another user's repo is refused).
# Normalise ownership first, in case an earlier run left root-owned files.
[ -e "$SRC_DIR" ] && chown -R "$MONET_USER:$MONET_USER" "$SRC_DIR"
if [ -d "$SRC_DIR/.git" ]; then
  log "updating source in $SRC_DIR -> $GIT_REF"
  as_monet git -C "$SRC_DIR" fetch --all --tags --quiet
  as_monet git -C "$SRC_DIR" checkout --quiet "$GIT_REF"
  as_monet git -C "$SRC_DIR" pull --ff-only --quiet 2>/dev/null || true
else
  log "cloning $REPO_URL -> $SRC_DIR ($GIT_REF)"
  as_monet git clone --quiet "$REPO_URL" "$SRC_DIR"
  as_monet git -C "$SRC_DIR" checkout --quiet "$GIT_REF"
fi

# monet needs Python >=3.10. Auto-detect one (or honour $PYTHON); the system
# python3 on older distros (e.g. Ubuntu 20.04 = 3.8) is too old.
pick_python() {
  local c
  for c in "${PYTHON:-}" python3.12 python3.11 python3.10; do
    [ -n "$c" ] || continue
    command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c 'import sys;raise SystemExit(0 if sys.version_info[:2]>=(3,10) else 1)' 2>/dev/null; then
      command -v "$c"; return 0
    fi
  done
  return 1
}
PYBIN="$(pick_python)" || {
  cat >&2 <<'MSG'
ERROR: monet needs Python >=3.10 and none was found.
On Ubuntu 20.04 (focal), install one via deadsnakes:
  sudo add-apt-repository -y ppa:deadsnakes/ppa
  sudo apt update && sudo apt install -y python3.10 python3.10-venv
Then re-run. Or pass a 3.10+ interpreter: PYTHON=/path/to/python3.10 bash deploy/setup-server.sh
(it must NOT be under /root — the service user cannot read /root).
MSG
  exit 1
}
case "$PYBIN" in
  /root/*) echo "WARNING: $PYBIN is under /root; the '$MONET_USER' user cannot read it — the service will fail. Use a system python3.10." >&2 ;;
esac

# Recreate the venv if it is missing OR incomplete (a failed ensurepip leaves a
# bin/python but no bin/pip).
if [ ! -x "$VENV_DIR/bin/pip" ]; then
  log "creating venv $VENV_DIR from $PYBIN ($("$PYBIN" -V 2>&1))"
  rm -rf "$VENV_DIR"
  as_monet "$PYBIN" -m venv "$VENV_DIR"
fi
log "installing monet[server] into the venv"
as_monet "$VENV_DIR/bin/pip" install --quiet --upgrade pip
as_monet "$VENV_DIR/bin/pip" install --quiet -e "$SRC_DIR[server]"
chown -R "$MONET_USER:$MONET_USER" "$APP_DIR"

# Put `monet` on PATH so admins don't have to spell out the venv path or
# activate anything: `sudo -u monet monet token ... --env-file $ENV_FILE`.
ln -sf "$VENV_DIR/bin/monet" /usr/local/bin/monet

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
