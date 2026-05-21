#!/usr/bin/env bash
# Drive realistic traffic through the user journey so the dashboards light up
# and the AI agent has something to investigate.
#
# Usage:
#   ./scripts/drive-traffic.sh                    # 20 iterations at default pace
#   ./scripts/drive-traffic.sh 50                 # 50 iterations
#   BASE=http://localhost:4000 ./scripts/drive-traffic.sh
#
# Requires: curl, jq.

set -euo pipefail

BASE="${BASE:-http://localhost:4000}"
EMAIL="${EMAIL:-demo@shop.local}"
PASSWORD="${PASSWORD:-demopass}"
ITERATIONS="${1:-20}"
SLEEP_BETWEEN="${SLEEP_BETWEEN:-0.3}"   # seconds between user-journey iterations

log() { printf '[drive-traffic %s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

# 1. Log in
log "logging in as $EMAIL"
TOKEN=$(curl -fsS -X POST "$BASE/api/auth/login" \
  -H 'content-type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}" | jq -r .token)
if [[ -z "$TOKEN" || "$TOKEN" == "null" ]]; then
  log "ERROR: login failed — is the backend up at $BASE?"
  exit 1
fi
AUTH="authorization: Bearer $TOKEN"

# 2. A couple of bad logins to populate the warn-level log stream too.
log "driving 3 bad logins"
for i in 1 2 3; do
  curl -fsS -o /dev/null -X POST "$BASE/api/auth/login" \
    -H 'content-type: application/json' \
    -d '{"email":"nope@example.com","password":"wrong"}' || true
done

# 3. Browse + cart + checkout + pay, iterations times.
log "running $ITERATIONS user-journey iterations (sleep ${SLEEP_BETWEEN}s between)"
SUCCEEDED=0
FAILED=0
for i in $(seq 1 "$ITERATIONS"); do
  # Browse the catalog (general + with search + the slow related endpoint).
  curl -fsS -o /dev/null "$BASE/api/products" || true
  curl -fsS -o /dev/null "$BASE/api/products?search=mug" || true
  # The deliberate slow query — exercise it so its DB histogram gets data.
  PROD_FOR_RELATED=$(( (RANDOM % 100) + 1 ))
  curl -fsS -o /dev/null "$BASE/api/products/$PROD_FOR_RELATED/related" || true

  # Random product to cart.
  PROD=$(( (RANDOM % 100) + 1 ))
  curl -fsS -o /dev/null -X POST "$BASE/api/cart/items" \
    -H "$AUTH" -H 'content-type: application/json' \
    -d "{\"product_id\":$PROD,\"quantity\":1}" || true

  # Checkout.
  ORDER=$(curl -fsS -X POST "$BASE/api/checkout" \
    -H "$AUTH" -H 'content-type: application/json' | jq -r .order_id 2>/dev/null || echo "")
  if [[ -z "$ORDER" || "$ORDER" == "null" ]]; then
    continue
  fi

  # Pay — backend's mock provider succeeds or fails per PAYMENT_FAILURE_RATE.
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

log "done: $SUCCEEDED payments succeeded, $FAILED failed (rate: $(awk "BEGIN{printf \"%.1f\", $FAILED * 100 / ($SUCCEEDED + $FAILED + 0.001)}")%)"
