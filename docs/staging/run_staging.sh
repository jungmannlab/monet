#!/usr/bin/env bash
# docs/staging/run_staging.sh
#
# Turnkey staging run for WP-12a (target-power API + auth), NO hardware. It:
#   1. seeds a simulated calibration,
#   2. proves the fail-closed host guard refuses 0.0.0.0 without tokens,
#   3. starts an auth-enabled server bound to 0.0.0.0 (tokens configured),
#   4. runs the auth/safety smoke matrix,
#   5. proves monet's own io.py client works with PAINT_MONET_TOKEN,
#   6. tears everything down.
#
# TLS is intentionally out of scope here (terminate it at a reverse proxy in a
# real deployment — see README.md). This exercises the application-layer surface.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
WORK="$(mktemp -d)"
PORT="${PORT:-8765}"
BASE_URL="http://127.0.0.1:$PORT"
WRITE_TOKEN="stg-write-$(python -c 'import secrets;print(secrets.token_hex(8))')"
READ_TOKEN="stg-read-$(python -c 'import secrets;print(secrets.token_hex(8))')"

cd "$WORK"
export MONET_DB_PATH="$WORK/staging.db"
CONFIG="$REPO/docs/staging/staging_config.yaml"

echo "### work dir: $WORK"
echo "### seeding calibration"
python "$REPO/docs/staging/seed_calibration.py"

echo
echo "### [guard] serve 0.0.0.0 WITHOUT tokens must be refused"
unset PAINT_MONET_TOKENS
if monet serve stg -c "$CONFIG" --host 0.0.0.0 --port "$PORT" \
     >/dev/null 2>"$WORK/guard.err"; then
  echo "  FAIL  server started without tokens on 0.0.0.0"; guard=1
else
  echo "  PASS  refused (exit $?):"; sed 's/^/        /' "$WORK/guard.err" | tail -2
  guard=0
fi

echo
echo "### starting auth-enabled server on 0.0.0.0:$PORT"
export PAINT_MONET_TOKENS="$WRITE_TOKEN:write:stg,$READ_TOKEN:read:dash"
monet serve stg -c "$CONFIG" --host 0.0.0.0 --port "$PORT" \
  >"$WORK/server.log" 2>&1 &
SRV=$!
cleanup() { kill "$SRV" 2>/dev/null; wait "$SRV" 2>/dev/null; rm -rf "$WORK"; }
trap cleanup EXIT

# wait for health
for _ in $(seq 1 40); do
  curl -sf "$BASE_URL/health" >/dev/null 2>&1 && break
  sleep 0.25
done
curl -sf "$BASE_URL/health" >/dev/null 2>&1 \
  || { echo "  server did not come up"; tail -20 "$WORK/server.log"; exit 1; }
echo "  up: $(curl -s "$BASE_URL/health")"

echo
BASE_URL="$BASE_URL" WRITE_TOKEN="$WRITE_TOKEN" READ_TOKEN="$READ_TOKEN" \
  bash "$REPO/docs/staging/smoke_auth.sh"
smoke=$?

echo
echo "### [io client] monet.io against the auth-enabled DB endpoints"
export PAINT_MONET_TOKEN="$WRITE_TOKEN"
python - "$BASE_URL" <<'PY'
import sys
import monet.io as mio
url = sys.argv[1]
idx = {"name": "stg", "wavelength [nm]": 488, "laser_power [mW]": 100}
mio.save_calibration(url, dict(idx), {"bkg": 0.0, "amp": 1.0, "phi": 0.0})
got = mio.load_calibration(url, dict(idx))
assert abs(got["amp"] - 1.0) < 1e-6, got
print("  PASS  io client write+read with token")
import os
os.environ.pop("PAINT_MONET_TOKEN", None)
try:
    mio.save_calibration(url, dict(idx), {"bkg": 0.0, "amp": 2.0, "phi": 0.0})
    print("  FAIL  token-less io write was accepted"); sys.exit(1)
except Exception:
    print("  PASS  token-less io write rejected")
PY
ioclient=$?

echo
echo "=================== STAGING SUMMARY ==================="
echo "  host guard refusal : $([ "$guard" = 0 ] && echo PASS || echo FAIL)"
echo "  auth/safety smoke  : $([ "$smoke" = 0 ] && echo PASS || echo FAIL)"
echo "  io client tokens   : $([ "$ioclient" = 0 ] && echo PASS || echo FAIL)"
[ "$guard" = 0 ] && [ "$smoke" = 0 ] && [ "$ioclient" = 0 ] \
  && echo "  RESULT: ALL PASS" || { echo "  RESULT: FAILURES"; exit 1; }
