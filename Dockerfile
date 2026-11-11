# syntax=docker/dockerfile:1
# monet DB-only calibration server image.
#
# Builds the FastAPI calibration server (`monet serve`, NO microscope name — the
# hardware-free web/database host, analogous to the picasso-registry service)
# into a small runtime image. This image is ONLY the DB-only server; the
# instrument server (`monet serve <Microscope>`, which actuates lasers over
# COM/USB) is not containerized and stays on the systemd/SSH install.
#
# SQLite by default (a mounted /data volume keeps calibrations.db across
# restarts). Tokens ride a mounted /etc/monet/monet.env (MONET_ENV_FILE), so
# `docker kill --signal=HUP` live-reloads add/rotate/revoke without downtime.
#
# Multi-stage: the builder has git so setuptools-scm reads the tags and bakes
# the REAL version into the wheel (the version IS the git tag in this repo), and
# so the git-URL dep (picasso-registry[auth] @ git+…) resolves. The runtime
# stage installs from the pre-built wheels offline — no git, no .git — so a
# `docker build` from a tagged checkout reports e.g. 0.7.0, not the 0.3.6
# fallback a git-less build would produce.

# Python floor — keep in sync with pyproject's requires-python. deploy/
# install-docker.sh derives this from pyproject and passes it as --build-arg
# PYVER=3.N; this default is the fallback for a bare `docker build`, and CI's
# image build would fail loudly (pip "requires a different Python") if it drifts.
ARG PYVER=3.11

# ---- builder: resolve version from git, build wheels ------------------------
FROM python:${PYVER}-slim AS builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# .git is needed here (setuptools-scm reads the tags); it is NOT copied into
# the runtime image below.
COPY . /app
# Build the package wheel (version stamped from the tags) plus wheels for the
# [server] dependency closure, so the runtime install is fully offline. The
# [server] extra carries picasso-registry[auth] as a git-URL direct reference;
# `pip wheel` clones + builds it here (git + network are available), dropping a
# picasso_registry wheel into /wheels alongside fastapi/uvicorn + monet's base
# deps. The BuildKit cache mount keeps pip's download cache across rebuilds.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip wheel --wheel-dir /wheels ".[server]"

# ---- runtime: slim, git-less ------------------------------------------------
FROM python:${PYVER}-slim
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MONET_HOST=0.0.0.0 \
    MONET_PORT=8000 \
    MONET_DB_PATH=/data/calibrations.db \
    MONET_ENV_FILE=/etc/monet/monet.env \
    MONET_LOG_DIR=/data

WORKDIR /app
COPY --from=builder /wheels /wheels
# Install the server stack from the pre-built wheels by NAME (not `monet[server]`):
# the [server] extra's git-URL direct reference would make pip re-fetch the git
# repo even under --no-index, which fails in this git-less runtime stage. Naming
# monet + the server libs installs the SAME closure from /wheels (picasso_registry
# was already built there) without re-evaluating the direct reference.
RUN pip install --no-index --find-links=/wheels \
        monet picasso-registry fastapi "uvicorn[standard]" \
    && rm -rf /wheels \
    && mkdir -p /data /etc/monet

# DB + logs live on a volume so they survive container replacement.
VOLUME ["/data"]
EXPOSE 8000

# Serve the DB-only app (no microscope name). create_all runs in the app
# lifespan (monet has no Alembic), so there is no migrate step. `exec` makes
# `monet serve` PID 1 so signals reach it — `docker kill --signal=HUP <container>`
# live-reloads the token map (MONET_ENV_FILE); without exec, sh stays PID 1 and
# swallows the HUP. The fail-closed host guard refuses a 0.0.0.0 bind without
# tokens, so the mounted env file must carry PAINT_MONET_TOKENS (install-docker.sh
# mints them before first start).
CMD ["sh", "-c", "exec monet serve --host \"$MONET_HOST\" --port \"$MONET_PORT\" --db-path \"$MONET_DB_PATH\""]
