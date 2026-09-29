# Deploying `monet serve` as a systemd service

This is the runbook for running the monet calibration server as a managed
service on Linux, instead of a detached `monet serve` in a terminal. It covers
the **DB-only** server (no microscope name, no hardware — the web/database host,
analogous to the picasso-registry service). For the **instrument** server
(`monet serve <Microscope>`, which actuates lasers), see
[Instrument server](#instrument-server) at the end.

A committed unit template lives at [`deploy/monet.service`](../deploy/monet.service).

## Quick start (scripted)

[`deploy/setup-server.sh`](../deploy/setup-server.sh) does the whole
dedicated-user install below in one shot — creates the `monet` user, installs a
venv under `/opt/monet`, preserves any existing token file and calibration DB,
writes the unit, and starts it. Safe to re-run.

```bash
sudo bash deploy/setup-server.sh
# override defaults via env, e.g.:
sudo GIT_REF=v0.4.0 MONET_HOST=127.0.0.1 bash deploy/setup-server.sh
```

The manual steps below are what the script automates, for when you want to
understand or customise them.

## 1. Dedicated service user

Run the service as a locked-down system account, not root:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin monet
```

> **The interpreter must be readable by this user.** A non-root user cannot read
> `/root`, so a conda env or venv under `/root/miniconda3` will not work with a
> dedicated user — put the app under `/opt` (below), or run the service as root
> (see [Running as root](#running-as-root)).

## 2. App + data in accessible locations

```bash
sudo install -d -o monet -g monet -m 750 /opt/monet /var/lib/monet
sudo git clone https://github.com/jungmannlab/monet.git /opt/monet/src
sudo python3 -m venv /opt/monet/.venv
sudo /opt/monet/.venv/bin/pip install -e "/opt/monet/src[server]"
sudo chown -R monet:monet /opt/monet /var/lib/monet
```

`--db-path /var/lib/monet/calibrations.db` (in the unit) keeps the SQLite DB in
a writable, service-owned directory.

## 3. Tokens (authentication)

Tokens live in the file the unit reads as its `EnvironmentFile`. Use a writable
path the service owns — **not** `/etc/monet` (avoids read-only/immutable-`/etc`
surprises); `/var/lib/monet/monet.env` works well:

```bash
sudo -u monet touch /var/lib/monet/monet.env
sudo chmod 600 /var/lib/monet/monet.env

# require auth, then mint a write token per rig and a read token per dashboard
printf 'PAINT_MONET_AUTH=on\n' | sudo -u monet tee -a /var/lib/monet/monet.env
sudo -u monet /opt/monet/.venv/bin/monet token add \
    --scope write --label microscope-mercury \
    --env-file /var/lib/monet/monet.env
sudo -u monet /opt/monet/.venv/bin/monet token list \
    --env-file /var/lib/monet/monet.env
```

Each `add` prints the token value **once** and the `PAINT_MONET_TOKEN=…` line to
paste on the client rig. See the top-level README "Authentication" section for
the client side.

> **Applying token changes:** `SIGHUP` reloads monet's package-root `.env`, **not**
> a systemd `EnvironmentFile`. After `monet token add/revoke/rotate --env-file
> /var/lib/monet/monet.env`, run `sudo systemctl restart monet`.

## 4. Install and start the unit

```bash
sudo cp /opt/monet/src/deploy/monet.service /etc/systemd/system/monet.service
sudoedit /etc/systemd/system/monet.service    # set User, venv path, host/port, db-path
sudo systemctl daemon-reload
sudo systemctl enable --now monet
systemctl status monet
journalctl -u monet -f
```

Verify: `curl -s http://localhost:8000/health` → `200`.

## Stopping / restarting

```bash
sudo systemctl restart monet     # apply config/token/code changes
sudo systemctl stop monet
sudo systemctl disable monet     # stop starting at boot
```

To stop a **pre-systemd, detached** `monet serve` still holding the port:

```bash
sudo ss -ltnp 'sport = :8000'    # find the PID bound to the port
kill <PID>                       # graceful; kill -9 if it refuses
```

## Troubleshooting

| Symptom (`systemctl status` / `journalctl`) | Cause | Fix |
|---|---|---|
| `status=217/USER` | `User=`/`Group=` names a non-existent account, or the unit was edited without `daemon-reload` | create the user (step 1), or fix `User=`, then `sudo systemctl daemon-reload` |
| `status=203/EXEC` | interpreter path wrong, **or** `ProtectHome=true` while the venv is under `/root` | fix the `ExecStart` path; if under `/root`, run as root and remove `ProtectHome` (see below), or relocate to `/opt` |
| `status=1/FAILURE` + `OSError: Read-only file system: '/monet.log'` | monet opens `./monet.log` (relative) at import; with no `WorkingDirectory` the CWD is `/`, read-only under `ProtectSystem=strict` | set `WorkingDirectory=/var/lib/monet` (writable + in `ReadWritePaths`); the template and script already do this |
| Fails to start, no obvious error | missing non-optional `EnvironmentFile` | prefix with `-` (`EnvironmentFile=-/var/lib/monet/monet.env`) or create the file |
| `refusing to bind non-loopback host … without auth` | `--host 0.0.0.0` with no tokens configured | add a token (step 3), or bind `127.0.0.1` |

## Running as root

If the interpreter must stay under `/root` (e.g. an existing conda env), run the
service as root and **remove `ProtectHome`** (it hides `/root` even for root, so
the service can't exec its interpreter):

```ini
User=root
Group=root
# ProtectHome removed — conda env lives under /root
ReadWritePaths=/var/lib/monet /root/miniconda3
```

Less isolated than a dedicated user; acceptable for an internal, loopback- or
auth-gated host.

## Instrument server

`monet serve <Microscope>` builds the instrument and actuates lasers, so it must
run on the rig PC next to the hardware, with the `hardware` extra installed
(`pip install -e ".[hardware]"`). On Linux, use the same unit with the microscope
name appended to `ExecStart`. On a **Windows** rig, systemd is unavailable — wrap
`monet serve <Microscope>` as a Windows service with
[NSSM](https://nssm.cc/) or a Task Scheduler "at system startup" task; token
changes apply on service restart.
