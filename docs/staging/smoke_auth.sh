#!/usr/bin/env bash
# docs/staging/smoke_auth.sh
#
# Auth / safety smoke test against a running monet staging server. Validates the
# networked surface added by WP-12a: bearer-token scopes, the safety clamp, and
# the public health probe. Does NOT check power accuracy (see WP-12b).
#
# Usage:
#   BASE_URL=http://127.0.0.1:8765 WRITE_TOKEN=... READ_TOKEN=... ./smoke_auth.sh
set -u

BASE_URL="${BASE_URL:-http://127.0.0.1:8765}"
WRITE_TOKEN="${WRITE_TOKEN:?set WRITE_TOKEN}"
READ_TOKEN="${READ_TOKEN:?set READ_TOKEN}"

fail=0
check() {  # check <label> <expected> <actual>
  if [ "$2" = "$3" ]; then
    echo "  PASS  $1 (got $3)"
  else
    echo "  FAIL  $1 (expected $2, got $3)"
    fail=1
  fi
}
code() { curl -s -o /dev/null -w "%{http_code}" "$@"; }
body() { curl -s "$@"; }

SET="$BASE_URL/power/set"
echo "== auth scopes =="
check "POST /power/set no token -> 401" 401 \
  "$(code -X POST "$SET" -H 'Content-Type: application/json' \
     -d '{"laser":488,"target_power_mw":10,"mode":"combined"}')"
check "POST /power/set read token -> 403" 403 \
  "$(code -X POST "$SET" -H "Authorization: Bearer $READ_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"laser":488,"target_power_mw":10,"mode":"combined"}')"
check "POST /power/set write token -> 200" 200 \
  "$(code -X POST "$SET" -H "Authorization: Bearer $WRITE_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"laser":488,"target_power_mw":10,"mode":"combined"}')"
check "GET /power read token -> 200" 200 \
  "$(code "$BASE_URL/power?laser=488" -H "Authorization: Bearer $READ_TOKEN")"
check "GET /health no token -> 200" 200 "$(code "$BASE_URL/health")"

echo "== safety clamp (C34) =="
# request 999 mW on laser 488 whose ceiling is 40 -> clamped to 40
resp="$(body -X POST "$SET" -H "Authorization: Bearer $WRITE_TOKEN" \
        -H 'Content-Type: application/json' \
        -d '{"laser":488,"target_power_mw":999,"mode":"combined"}')"
echo "  response: $resp"
echo "$resp" | grep -q '"clamped":true' \
  && echo "  PASS  clamped=true" || { echo "  FAIL  clamped flag"; fail=1; }
echo "$resp" | grep -q '"target_power_mw":40' \
  && echo "  PASS  target clamped to ceiling 40" \
  || { echo "  FAIL  target not clamped to 40"; fail=1; }

echo "== invalid input rejected =="
check "unknown mode -> 422" 422 \
  "$(code -X POST "$SET" -H "Authorization: Bearer $WRITE_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"laser":488,"target_power_mw":10,"mode":"bogus"}')"

if [ "$fail" = 0 ]; then echo "ALL SMOKE CHECKS PASSED"; else echo "SMOKE FAILURES"; fi
exit "$fail"
