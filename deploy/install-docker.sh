#!/usr/bin/env bash
# monet — containerized deploy (Docker) of the DB-only calibration server.
#
# Companion to the bare-metal deploy/setup-server.sh: SAME data/token layout
# (DB under /var/lib/monet, token file under /etc/monet/monet.env), but the
# service runs in a Docker container instead of a systemd-managed venv. No host
# Python needed — the interpreter lives in the image (which is why this path
# sidesteps the "host has no Python 3.11" problem on older distros).
#
# This deploys the DB-only server only (`monet serve`, no microscope). The
# instrument server (`monet serve <Microscope>`, which actuates lasers) is NOT
# containerized — run it on the rig via setup-server.sh.
#
# Safe to re-run — RE-RUNNING IS ALSO HOW YOU UPGRADE: it updates the source
# checkout to GIT_REF, rebuilds the image, and replaces the running container.
# It PRESERVES an existing token file and database.
#
# What it does:
#   1. installs Docker if missing (official get.docker.com; SKIP_DOCKER_INSTALL=1 to opt out)
#   2. clones/updates the source to SRC_DIR at GIT_REF (.git is needed for the version)
#   3. builds the image
#   4. on first run, writes PAINT_MONET_AUTH=on + mints a write + read token
#   5. (re)starts the container: DB on a host bind-mount, token file mounted
#      read-only (MONET_ENV_FILE) so `docker kill --signal=HUP` live-reloads tokens
#
# Usage (as root):
#   sudo bash deploy/install-docker.sh
#   sudo GIT_REF=v0.7.0 bash deploy/install-docker.sh      # pinned install / upgrade
set -euo pipefail

# ---- configuration (override via environment) -------------------------------
IMAGE="${IMAGE:-monet}"
CONTAINER="${CONTAINER:-monet}"
APP_DIR="${APP_DIR:-/opt/monet}"
# Own checkout dir (NOT $APP_DIR/src, which setup-server.sh owns as the `monet`
# user — root git ops there would trip git's dubious-ownership guard). DATA_DIR
# + ETC_DIR ARE shared with the bare-metal path on purpose, so a host can switch
# between the two deploys over the same DB and tokens.
SRC_DIR="${SRC_DIR:-$APP_DIR/docker-src}"      # source clone (build context)
DATA_DIR="${DATA_DIR:-/var/lib/monet}"         # calibrations.db (-> /data)
ETC_DIR="${ETC_DIR:-/etc/monet}"               # monet.env (tokens + auth toggle)
ENV_FILE="${ENV_FILE:-$ETC_DIR/monet.env}"
REPO_URL="${REPO_URL:-https://github.com/jungmannlab/monet.git}"
GIT_REF="${GIT_REF:-master}"                   # pin a tag in production!
# Set PULL_IMAGE to a published image ref (e.g.
# ghcr.io/jungmannlab/monet:v0.7.0) to pull a prebuilt image instead of cloning
# + building locally — the fast path for a new lab box. A private GHCR package
# needs `docker login ghcr.io` first; a public one needs nothing. Unset
# (default) = clone $REPO_URL@$GIT_REF and build.
PULL_IMAGE="${PULL_IMAGE:-}"
MONET_PORT="${MONET_PORT:-8000}"               # host port -> container 8000
# The token file path INSIDE the container (where $ETC_DIR is mounted):
CONTAINER_ETC="/etc/monet"
CONTAINER_ENV_FILE="$CONTAINER_ETC/monet.env"

log() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
[ "$(id -u)" -eq 0 ] || {
  echo "run as root (sudo bash deploy/install-docker.sh)"; exit 1;
}

# ---- 1. Docker ---------------------------------------------------------------
if command -v docker >/dev/null 2>&1; then
  log "docker present ($(docker --version))"
elif [ "${SKIP_DOCKER_INSTALL:-0}" = "1" ]; then
  echo "ERROR: docker not found and SKIP_DOCKER_INSTALL=1 set — install Docker first." >&2
  exit 1
else
  log "installing Docker (official get.docker.com convenience script)"
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker 2>/dev/null || true
docker info >/dev/null 2>&1 || {
  echo "ERROR: the Docker daemon is not reachable — start it and re-run." >&2
  exit 1
}

# ---- 2. obtain the image (pull a prebuilt one, or clone + build) ------------
if [ -n "$PULL_IMAGE" ]; then
  log "pulling prebuilt image $PULL_IMAGE"
  docker pull "$PULL_IMAGE" || {
    echo "ERROR: could not pull $PULL_IMAGE. If the GHCR package is private," >&2
    echo "run 'docker login ghcr.io' first, or unset PULL_IMAGE to build locally." >&2
    exit 1
  }
  docker tag "$PULL_IMAGE" "$IMAGE:latest"
else
  # .git is part of the build context: setuptools-scm derives the version from
  # the tags at build time (see Dockerfile). mkdir -p (not install -d) so an
  # existing $APP_DIR created by setup-server.sh keeps its owner/mode.
  mkdir -p "$APP_DIR"
  if [ -d "$SRC_DIR/.git" ]; then
    log "updating source in $SRC_DIR -> $GIT_REF"
    git -C "$SRC_DIR" fetch --all --tags --quiet
    git -C "$SRC_DIR" checkout --quiet "$GIT_REF"
    # If GIT_REF is a BRANCH, fast-forward it to the just-fetched remote tip.
    # (A tag/SHA leaves HEAD detached -> symbolic-ref fails -> skip.) Do NOT
    # swallow a failed ff: a diverged local branch must abort loudly, not
    # silently rebuild the image from stale source.
    if git -C "$SRC_DIR" symbolic-ref -q HEAD >/dev/null; then
      git -C "$SRC_DIR" merge --ff-only --quiet "origin/$GIT_REF"
    fi
  else
    log "cloning $REPO_URL -> $SRC_DIR ($GIT_REF)"
    git clone --quiet "$REPO_URL" "$SRC_DIR"
    git -C "$SRC_DIR" checkout --quiet "$GIT_REF"
  fi
  # Derive the Python floor from the single source of truth (pyproject's
  # requires-python) and feed it to the image's PYVER build-arg, so a floor
  # bump doesn't need a matching Dockerfile edit. Fallback 11 if unparseable.
  PYVER_MINOR="$(sed -nE 's/^[[:space:]]*requires-python[[:space:]]*=[[:space:]]*"[^"]*3\.([0-9]+).*/\1/p' "$SRC_DIR/pyproject.toml" | head -1)"
  TAG="$(git -C "$SRC_DIR" describe --tags --always 2>/dev/null || echo "$GIT_REF")"
  log "building image $IMAGE:$TAG (python 3.${PYVER_MINOR:-11})"
  DOCKER_BUILDKIT=1 docker build \
    --build-arg "PYVER=3.${PYVER_MINOR:-11}" \
    -t "$IMAGE:$TAG" -t "$IMAGE:latest" "$SRC_DIR"
fi

# ---- 3. admin wrapper + a copy of this installer (for `upgrade`) ------------
# Installed from this script's own dir, so it works in BOTH pull and build
# modes. The admin console (monet-admin-cli / -gui) drives this wrapper over
# SSH; `monet-admin upgrade` re-runs the copied installer.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
install -d -m 755 "$APP_DIR/deploy"
copy_if_newer() {  # copy_if_newer <src> <dest>
  [ -f "$1" ] || return 0
  [ "$(readlink -f "$1")" = "$(readlink -f "$2")" ] && return 0
  install -m 755 "$1" "$2"
}
copy_if_newer "$SCRIPT_DIR/install-docker.sh" "$APP_DIR/deploy/install-docker.sh"
if [ -f "$APP_DIR/deploy/install-docker.sh" ] \
    || [ -f "$SCRIPT_DIR/monet-admin" ]; then
  copy_if_newer "$SCRIPT_DIR/monet-admin" "$APP_DIR/deploy/monet-admin"
  if [ -f "$APP_DIR/deploy/monet-admin" ]; then
    ln -sf "$APP_DIR/deploy/monet-admin" /usr/local/bin/monet-admin
    log "admin wrapper -> /usr/local/bin/monet-admin"
  fi
fi
if [ ! -e /usr/local/bin/monet-admin ]; then
  # Happens if the installer was run standalone (e.g. a bare PULL_IMAGE curl)
  # with no sibling deploy/monet-admin to copy. The server still runs, but the
  # SSH admin console (monet-admin* -> this wrapper) won't work until it is
  # installed. Warn loudly rather than leave it silently missing.
  echo "WARNING: deploy/monet-admin was not found next to this installer, so" \
    "the server-side admin wrapper (/usr/local/bin/monet-admin) was NOT" \
    "installed — the SSH admin console won't work. Re-run from a monet" \
    "checkout (so deploy/monet-admin is present)." >&2
fi

# ---- 4. directories + token env file (preserve if it already has content) ---
install -d -m 750 "$DATA_DIR" "$ETC_DIR"
if [ -s "$ENV_FILE" ]; then
  log "keeping existing token file $ENV_FILE"
else
  # Sanitize the hostname to the token-label charset and fall back if empty,
  # so an odd/empty `hostname -s` can't make `token add` reject the label and
  # abort the install (set -e) after the image is already built.
  HOSTLABEL="$(hostname -s 2>/dev/null | tr -cd 'A-Za-z0-9._-')"
  HOSTLABEL="${HOSTLABEL:-monet-host}"
  log "no tokens at $ENV_FILE — enabling auth + minting one write ($HOSTLABEL) + one read (lab-dashboard) token"
  # Enforce auth (the serve host guard refuses a non-loopback bind otherwise).
  printf 'PAINT_MONET_AUTH=on\n' > "$ENV_FILE"
  # The token CLI lives in the image; mint via throwaway containers with
  # $ETC_DIR mounted rw so the file is updated on the host.
  docker run --rm -v "$ETC_DIR:$CONTAINER_ETC" "$IMAGE:latest" \
    monet token add --scope write --label "$HOSTLABEL" \
    --env-file "$CONTAINER_ENV_FILE"
  docker run --rm -v "$ETC_DIR:$CONTAINER_ETC" "$IMAGE:latest" \
    monet token add --scope read --label lab-dashboard \
    --env-file "$CONTAINER_ENV_FILE"
fi
chmod 600 "$ENV_FILE"

# ---- 5. (re)start the container ----------------------------------------------
log "replacing any existing container $CONTAINER"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

# Tokens ride a MOUNTED file + MONET_ENV_FILE — NOT `-e PAINT_MONET_TOKENS`.
# Injecting the map into the process env would, by dotenv's override=False at
# import, still work, but reading it from the mounted file keeps `docker kill
# --signal=HUP $CONTAINER` re-reading add/rotate/revoke without downtime (the
# server's SIGHUP handler re-loads MONET_ENV_FILE). The DB is a host bind-mount
# under $DATA_DIR (the one thing to back up). create_all runs in the app
# lifespan (no Alembic).
log "starting container $CONTAINER (host port $MONET_PORT -> 8000)"
docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  -p "$MONET_PORT:8000" \
  -v "$DATA_DIR:/data" \
  -v "$ETC_DIR:$CONTAINER_ETC:ro" \
  -e "MONET_ENV_FILE=$CONTAINER_ENV_FILE" \
  "$IMAGE:latest" >/dev/null

# ---- 6. health ---------------------------------------------------------------
log "waiting for the service to answer /health"
ok=""
for _ in $(seq 1 30); do
  if curl -fsS "http://localhost:$MONET_PORT/health" >/dev/null 2>&1; then
    ok=1; break
  fi
  sleep 1
done
docker ps --filter "name=$CONTAINER" \
  --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
if [ -z "$ok" ]; then
  echo "WARNING: /health did not answer within ~30s — check: docker logs $CONTAINER" >&2
fi

echo
log "health:    curl -s http://localhost:$MONET_PORT/health"
log "logs:      docker logs -f $CONTAINER"
log "dashboard: http://$(hostname -s):$MONET_PORT/dashboard"
log "admin:     monet-admin status   (or from a laptop: monet-admin-gui)"
log "tokens:    monet-admin token add --scope write --label <holder>"
