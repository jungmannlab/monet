# Staging harness — WP-12a target-power API + auth

Real-world testing of the networked surface **without a live microscope or the
production server**. It runs the FastAPI server against the simulated `Test*`
drivers, so it exercises the parts that unit tests (which run auth-disabled,
in-process) cannot: a real uvicorn process, real HTTP, real bearer tokens, the
fail-closed host guard, and monet's own `io.py` client sending its token.

It validates the **plumbing** (Risk A): auth scopes, host guard, safety clamp,
client token. It does **not** validate power accuracy (Risk B) — the stock Test
attenuator/meter are no-ops, so `measured_power_mw` is not meaningful here. Power
accuracy is validated on real hardware at **WP-12b**.

## One-shot run

```bash
bash docs/staging/run_staging.sh
```

This seeds a simulated calibration, proves the host guard refuses `0.0.0.0`
without tokens, starts an auth-enabled server, runs the auth/safety smoke matrix,
proves the `io.py` client works with `PAINT_MONET_TOKEN`, and tears down. Expected
tail: `RESULT: ALL PASS`.

## Manual / interactive

```bash
python docs/staging/seed_calibration.py            # writes staging_power_database.xlsx
export WRITE=$(python -c 'import secrets;print(secrets.token_urlsafe(24))')
export READ=$(python -c 'import secrets;print(secrets.token_urlsafe(24))')
export PAINT_MONET_TOKENS="$WRITE:write:stg,$READ:read:dash"
export MONET_DB_PATH=$PWD/staging.db
monet serve stg -c docs/staging/staging_config.yaml --host 0.0.0.0 --port 8765

# in another shell:
BASE_URL=http://127.0.0.1:8765 WRITE_TOKEN=$WRITE READ_TOKEN=$READ \
  bash docs/staging/smoke_auth.sh

# drive it as the recommender would:
curl -H "Authorization: Bearer $WRITE" -H 'Content-Type: application/json' \
  -X POST http://127.0.0.1:8765/power/set \
  -d '{"laser":488,"target_power_mw":25,"mode":"combined"}'
```

To test monet's own client against the same server, point a config's `database:`
at `http://127.0.0.1:8765` and `export PAINT_MONET_TOKEN=$WRITE` before running
`monet calibrate`/`set`.

## TLS (production)

This harness runs plain HTTP on loopback to exercise the application layer. In a
real deployment, **terminate TLS at a reverse proxy** (Caddy/nginx) in front of
the server, and put the browser dashboard behind the same proxy with HTTP Basic /
lab SSO (per ADR-001). Example Caddy snippet:

```
scope.lab.example {
    reverse_proxy 127.0.0.1:8765
}
```

Then bind the server to loopback (`--host 127.0.0.1`) and let the proxy be the
only network-facing door.

## Rollout note

Enabling auth on a networked server and setting `PAINT_MONET_TOKEN` on the
clients (microscopes running `calibrate`/`set`, the GUI) must happen **together** —
a token-enforcing server rejects token-less clients with 401. Until then, keep the
server loopback-bound (the default), which needs no tokens.
