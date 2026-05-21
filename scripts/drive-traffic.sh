#!/usr/bin/env bash
# Drive a user-journey load: login + browse + cart + checkout + pay.
# Usage: ./scripts/drive-traffic.sh [iterations]
set -euo pipefail

BASE="${BASE:-http://localhost:4000}"
EMAIL="${EMAIL:-demo@shop.local}"
PASSWORD="${PASSWORD:-demopass}"
ITERATIONS="${1:-20}"
SLEEP_BETWEEN="${SLEEP_BETWEEN:-0.3}"
READY_TIMEOUT="${READY_TIMEOUT:-30}"

log() { printf '[drive-traffic %s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

# Wait for the backend to be reachable. The container's port binds as soon as
# the process starts, but Express only starts listening after MySQL is ready
# and initSchema() finishes — so a bare curl right after `compose up` can get
# "Empty reply from server".
log "waiting up to ${READY_TIMEOUT}s for $BASE/healthz"
for i in $(seq 1 "$READY_TIMEOUT"); do
  if curl -fsS "$BASE/healthz" >/dev/null 2>&1; then
    log "backend is ready"
    break
  fi
  if [[ "$i" -eq "$READY_TIMEOUT" ]]; then
    log "ERROR: backend never became ready"
    exit 1
  fi
  sleep 1
done

log "logging in as $EMAIL"
TOKEN=$(curl -fsS -X POST "$BASE/api/auth/login" \
  -H 'content-type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}" | jq -r .token)
if [[ -z "$TOKEN" || "$TOKEN" == "null" ]]; then
  log "ERROR: login failed"
  exit 1
fi
AUTH="authorization: Bearer $TOKEN"

# A few bad logins to populate warn-level logs.
log "driving 3 bad logins"
for _ in 1 2 3; do
  curl -fsS -o /dev/null -X POST "$BASE/api/auth/login" \
    -H 'content-type: application/json' \
    -d '{"email":"nope@example.com","password":"wrong"}' 2>/dev/null || true
done

log "running $ITERATIONS user-journey iterations (sleep ${SLEEP_BETWEEN}s between)"
SUCCEEDED=0
FAILED=0
for _ in $(seq 1 "$ITERATIONS"); do
  curl -fsS -o /dev/null "$BASE/api/products" || true
  curl -fsS -o /dev/null "$BASE/api/products?search=mug" || true
  # Exercise the deliberately-slow self-join.
  PROD_FOR_RELATED=$(( (RANDOM % 100) + 1 ))
  curl -fsS -o /dev/null "$BASE/api/products/$PROD_FOR_RELATED/related" || true

  PROD=$(( (RANDOM % 100) + 1 ))
  curl -fsS -o /dev/null -X POST "$BASE/api/cart/items" \
    -H "$AUTH" -H 'content-type: application/json' \
    -d "{\"product_id\":$PROD,\"quantity\":1}" || true

  ORDER=$(curl -fsS -X POST "$BASE/api/checkout" \
    -H "$AUTH" -H 'content-type: application/json' | jq -r .order_id 2>/dev/null || echo "")
  if [[ -z "$ORDER" || "$ORDER" == "null" ]]; then
    continue
  fi

  RESP=$(curl -fsS -X POST "$BASE/api/payment" \
    -H "$AUTH" -H 'content-type: application/json' \
    -d "{\"order_id\":$ORDER,\"card_number\":\"4242424242424242\"}" 2>/dev/null || echo '{"error":"payment_declined"}')
  if [[ "$RESP" == *"paid"* ]]; then
    SUCCEEDED=$((SUCCEEDED + 1))
  else
    FAILED=$((FAILED + 1))
  fi

  sleep "$SLEEP_BETWEEN"
done

TOTAL=$((SUCCEEDED + FAILED))
if [[ "$TOTAL" -gt 0 ]]; then
  RATE_PCT=$(( FAILED * 100 / TOTAL ))
else
  RATE_PCT=0
fi
log "done: $SUCCEEDED payments succeeded, $FAILED failed (failure rate: ${RATE_PCT}%)"
