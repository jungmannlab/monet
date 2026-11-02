# Rollout & auth testing runbook (WP-12a)

How to test server–client authentication and roll it to the microscopes **without
risking production**. Read alongside `docs/staging/README.md` (the hardware-free
harness) and the repo `README.md` "Authentication" section.

The whole point: with the default `PAINT_MONET_AUTH=auto` and no tokens set, the
new code behaves **exactly like today** (server open, client sends no header). So
"deploy the code" and "turn auth on" are independent steps you can test and roll
separately.

---

## 0. Getting the tokens (`<wtok>` / `<rtok>`)

A monet token is a high-entropy random string, registered on the server with a
scope + label and handed to whoever holds it. The easiest way is the **`monet
token`** CLI on the server box — it generates the value, writes the
`PAINT_MONET_TOKENS` map, and prints the client line once:

```bash
monet token add --scope write --label microscope-mercury   # -> PAINT_MONET_TOKEN=…
monet token add --scope read  --label dashboards
monet token list        # scopes + labels (never values)
monet token rotate --label microscope-mercury
monet token revoke --label microscope-mercury
```

(Equivalent by hand: generate with `python -c "import secrets;
print(secrets.token_urlsafe(32))"` — must avoid `:` `,` `;` and newlines — and add
one `token:scope:label` entry to `PAINT_MONET_TOKENS`.)

- `<wtok>` — a **write** token: for anything that writes the DB or actuates a
  laser (a microscope running `calibrate`/`set`/GUI, the recommender). `write`
  also satisfies `read`.
- `<rtok>` — a **read** token: for read-only consumers (dashboards).

Register them **server-side** in `PAINT_MONET_TOKENS` as `value:scope:label`, and
give the matching value to each holder:

```bash
# server .env  (the host running `monet serve`)
PAINT_MONET_TOKENS=Xy7...q:write:microscope-mercury,Zq9...t:read:dashboards
```
```bash
# a microscope's .env  (client)   — its own write token
PAINT_MONET_TOKEN=Xy7...q
```

Give **each rig its own token** (its own `label`) so you can revoke one without
touching the others. Rotate = edit the server map + restart; revoke = delete that
entry.

---

## 1. One box, no hardware (fastest)

```bash
bash docs/staging/run_staging.sh
```
Expect `RESULT: ALL PASS`. This starts a real uvicorn auth server against the
simulated `stg` microscope and checks, over real HTTP:
- the fail-closed host guard (refuses `0.0.0.0` without tokens),
- `401` no-token · `403` read-token write · `200` write-token · public `/health`,
- the safety clamp, and
- **the real client path**: `monet.io` picking its token up from a gitignored
  `.env` (write+read succeed; a token-less client is rejected).

This validates the auth plumbing. It does **not** validate power accuracy (stock
Test hardware) — that's the on-instrument WP-12b gate.

## 2. Two machines, copy of the production DB (the real dry run)

Stand the server up on a **non-production host**, pointed at a **copy** of the
production calibration DB, behind a TLS reverse proxy:

```bash
cp /prod/calibrations.db /srv/staging.db     # never test against the live DB
# /srv/monet/.env
PAINT_MONET_AUTH=on
PAINT_MONET_TOKENS=<wtok>:write:test-scope,<rtok>:read:dash
MONET_DB_PATH=/srv/staging.db
monet serve --host 0.0.0.0 --port 8000       # + Caddy/nginx terminating TLS
```

On a **test client** (a spare PC, or a microscope PC **outside experiment
hours**), point a config at the staging server and give the machine its token:

```yaml
# configs.yaml
database: https://staging-host:8000
```
```bash
# that machine's .env
PAINT_MONET_TOKEN=<wtok>
printenv PAINT_MONET_TOKEN        # sanity: confirm the launcher/monet loaded .env
monet calibrate <scope>           # writes calibrations over auth'd HTTPS
monet set <scope>                 # reads them back
```

Then the **negative tests** (proving enforcement, not just the happy path):

| Test | Expect |
|---|---|
| client with no token (unset `PAINT_MONET_TOKEN`) | write rejected (401) |
| client with the **read** token `<rtok>` doing a calibrate write | rejected (403) |
| client `PAINT_MONET_AUTH=off` against the `on` server | rejected (401) |
| plain `http://` instead of `https://` | refused/redirected by the proxy |

## 3. Production cutover (mind the ordering)

Under `auto`, **the server starts enforcing the instant `PAINT_MONET_TOKENS` is
set** — so a client without a token breaks. Use one of these safe orders:

- **Client-first (simplest, zero downtime):** put `PAINT_MONET_TOKEN` on every
  client *first* (the still-open server ignores the header), **then** add
  `PAINT_MONET_TOKENS` on the server. When enforcement flips on, every client
  already authenticates.
- **Toggle (atomic prep):** keep the server at `PAINT_MONET_AUTH=off` while you
  stage tokens on both sides, then flip the server to `on`.

Steps:
1. **Deploy the code everywhere with `PAINT_MONET_AUTH=auto` and no tokens** —
   behavior is unchanged; confirm normal calibrate/set/GUI still works.
2. Distribute client tokens (client-first) **or** stage both with the server
   `off`.
3. Turn on the server (add `PAINT_MONET_TOKENS`, or flip `off`→`on`), **canary a
   single microscope**, verify, then roll the rest.
4. Add TLS at the proxy and bind loopback behind it (`--host 127.0.0.1`), or bind
   the interface only with tokens configured.

**Rollback:** revert the server `.env` (drop `PAINT_MONET_TOKENS`, or set
`PAINT_MONET_AUTH=off`) and restart — enforcement is off again immediately.

## Two things that bite

1. **TLS is not optional in production** — a bearer token over plain HTTP is
   sniffable on the LAN. Test Level 2 with the proxy in place.
2. **Confirm `.env` actually reaches the process** — `printenv PAINT_MONET_TOKEN`
   on the client after your launcher runs. monet loads `.env` from the package
   root and the working directory, but an odd launch dir or a stale export can
   surprise you.
