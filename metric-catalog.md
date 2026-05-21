# Metric & Log Catalog

> Every signal this system exposes. Each entry includes **what it is**, **why it matters**, **what normal looks like**, **what a change implies**. The catalog, the running system, and the AI agent's runtime context all reference these same names — single source of truth.

This file is loaded into the AI observability service at startup via the `get_metric_catalog` tool. The LLM consults it before composing queries so it doesn't hallucinate metric names.

---

## Conventions (summary)

- **Namespaces**: `http_*` (transport), `ecom_*` (business — user journey), `auth_*` (login/JWT), `db_*` (MySQL).
- **Counters** end in `_total`.
- **Durations** are seconds (base unit per Prometheus naming guide).
- **Histogram buckets** (HTTP + DB): `[0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1, 1.5, 2.5, 5]` — tuned to a 25 ms – 5 s envelope, with the payment route's 120–450 ms band well-resolved.
- **No high-cardinality labels.** Forbidden in labels: `user_id`, `order_id`, `payment_id`, `cart_id`, `request_id`, `trace_id`, raw URL paths, free-text error messages, SKUs, email, IP.
- **Route labels** use Express *templates* (`/api/products/:id`), never resolved values.

---

## HTTP metrics

### `http_requests_total{method, route, status_code}`
**Type:** Counter
**What:** Every HTTP request the backend handles, post-routing. Incremented at response time.
**Labels:**
- `method` — `GET|POST|PUT|DELETE` (bounded)
- `route` — Express template, e.g., `/api/products/:id`, `/api/payment`, `/healthz`, `unmatched` for 404s
- `status_code` — numeric string (`200`, `401`, `409`, `500`, …)
**Normal:** request rate scales linearly with traffic; ~95%+ should be `2xx`. Background `/healthz` chatter is constant.
**A change implies:**
- A spike in `5xx` rate → the backend itself is broken (look at logs filtered to `log.level:error`).
- A spike in `4xx` rate on `/api/payment` → likely `payment_declined` (mock provider failing more than baseline).
- A spike in `4xx` on `/api/checkout` → `empty_cart` or `insufficient_stock` (catalog inventory + carts diverging).
- A spike in `401` on `/api/auth/login` → either auth attack or a frontend regression.

### `http_request_duration_seconds_bucket{method, route, status_code, le}`
**Type:** Histogram (buckets enumerated above).
**What:** End-to-end request latency, measured from middleware entry to `res.on('finish')`.
**Use:** `histogram_quantile(0.95, sum by (route, le) (rate(http_request_duration_seconds_bucket[5m])))` for p95 by route.
**Normal:**
- `/api/products` ~ 5–30 ms
- `/api/products/:id` ~ 5–20 ms
- **`/api/products/:id/related` ~ 30–200 ms, climbing with order volume** — this is the deliberate slow query.
- `/api/cart/*` ~ 10–30 ms
- `/api/checkout` ~ 30–80 ms (multi-statement transaction)
- **`/api/payment` p95 ≈ 430 ms** (uniform 120–450 ms mock sleep)
**A change implies:**
- **Payment latency p95 climbing above ~450 ms while checkout p95 stays flat** → the payment provider is degrading independently of our service. Out-of-scope for our team but reportable.
- **Checkout p95 climbing while payment is flat** → DB transaction contention (look at `db_query_duration_seconds{query_name="checkout_create_order"}`).
- **`/related` p95 climbing slowly over hours/days** → the un-indexed self-join hitting more rows; expected growth pattern.
- p95 spike on *every* route at once → host-level problem (CPU, IO, GC pause).

### `http_requests_in_flight{method, route}`
**Type:** Gauge
**What:** Concurrent in-flight requests. Incremented on entry, decremented on response.
**Normal:** ≤ a handful per route at our test traffic levels.
**A change implies:** a climb that does not return to baseline means requests are hanging — pair with latency p99 and DB query duration to localize.

---

## Business metrics (user journey)

> The PDF emphasizes the user journey: browse → cart → checkout → payment. Each transition has a counter so we can compute funnel conversion in PromQL.

### `ecom_cart_items_added_total{product_category}`
**Type:** Counter
**What:** Each successful `POST /api/cart/items`. Labeled by the product's `category` (bounded — 4 categories in seed: `home`, `kitchen`, `books`, `tech`).
**Normal:** Should track upstream traffic to `/api/products`. Conversion from product views to cart adds is the first funnel step.
**A change implies:**
- Drop to zero with `/api/products` rate flat → broken cart endpoint, even if HTTP 200s come back (bug between `addItem` and the response).
- Spike concentrated on one category → marketing event or scraping.

### `ecom_checkouts_total{outcome}`
**Type:** Counter
**What:** Each `POST /api/checkout`. Labeled with the *business outcome* (not HTTP code).
**Outcomes:** `success`, `empty_cart`, `insufficient_stock`. (These are the exact error codes the route emits — see `backend/src/routes/checkout.ts`.)
**Normal:** `success` dominates; `empty_cart` near zero (frontend prevents it); `insufficient_stock` rare unless seed data drifts.
**A change implies:**
- `insufficient_stock` climbing → product seed needs refresh OR a popular item is sold out, blocking checkout for many users.
- `empty_cart` climbing → frontend bug letting users hit checkout with no items.
- `success` rate dropping while HTTP 200s for /api/cart stay flat → look at the two error outcomes first.

### `ecom_payments_total{outcome, provider}`
**Type:** Counter
**What:** Each `POST /api/payment` attempt. Outcome is the mock provider result, not the HTTP code (HTTP is 402 for declines but the metric tells the *business* story).
**Outcomes:** `succeeded`, `failed` (the two states the mock provider emits).
**Provider:** `mock-stripe` (only one in scope).
**Normal at default config:** `failed` ≈ 8% of payments (`PAYMENT_FAILURE_RATE=0.08`).
**A change implies:**
- `failed` rate climbing above ~10% → either someone bumped `PAYMENT_FAILURE_RATE` (the assignment hint to make failures visible) OR the real provider is degrading. The AI should compute the rate and compare to baseline 8%.
- Both `succeeded` and `failed` dropping to zero while checkout still works → /api/payment broken upstream of the provider call.

### `ecom_payment_amount_cents_total{provider, outcome}`
**Type:** Counter
**What:** Sum of `amount_cents` from each payment attempt, labeled by provider and outcome.
**Use:** `rate(ecom_payment_amount_cents_total{outcome="succeeded"}[5m]) * 60` for revenue-per-minute.
**Normal:** Tracks `ecom_payments_total{outcome="succeeded"}` × average order value. Average order in seed data ≈ $50-150.
**A change implies:** Revenue drop without payment-count drop → average order value has fallen (bulk discounting, cart-size regression).

### `ecom_order_value_cents_bucket{le}`
**Type:** Histogram
**Buckets:** `[1000, 2500, 5000, 10000, 25000, 50000, 100000, 250000]` — cents.
**What:** Distribution of order totals at checkout time.
**Normal:** Median ≈ 7000–15000 cents ($70–$150) with the seed product set.
**A change implies:**
- p50 dropping → average basket size shrinking (worth a product-team ping).
- p99 climbing dramatically → either a fraud signal or a single power user; the AI should pivot to logs to see *which* orders.

---

## Auth metrics

### `auth_login_attempts_total{outcome}`
**Type:** Counter
**What:** Each `POST /api/auth/login`. Outcome from the route's logic, not HTTP code.
**Outcomes:** `succeeded`, `invalid_credentials`.
**Normal:** Heavy `succeeded` dominance once users settle in. Some `invalid_credentials` from typos but a steady stream.
**A change implies:**
- `invalid_credentials` spike → credential-stuffing attack or password rotation in progress.
- `succeeded` dropping to zero → either the JWT-signing path is broken or no one is trying to log in (correlate with `http_requests_total{route="/api/auth/login"}`).

---

## Database metrics

### `db_query_duration_seconds_bucket{query_name, le}`
**Type:** Histogram (same buckets as HTTP latency).
**What:** Duration of named, instrumented MySQL queries.
**Bounded `query_name` enum:**
- `products_related` — the deliberate slow self-join in `/api/products/:id/related`.
- `checkout_create_order` — the multi-statement transaction in `/api/checkout`.
- `payment_record` — the transaction in `/api/payment`.
**Normal:**
- `products_related` ≈ 20–80 ms on the seed dataset, grows non-linearly with `order_items` row count.
- `checkout_create_order` ≈ 15–40 ms.
- `payment_record` ≈ 10–25 ms.
**A change implies:**
- `products_related` p95 doubling → order_items table grew enough that the un-indexed join is biting; add an index OR cache.
- `checkout_create_order` p95 climbing while `payment_record` stays flat → row-lock contention on `products` (the `UPDATE stock` in the same transaction).
- Any query >1 s → connection pool exhaustion likely follows; check `http_requests_in_flight`.

---

## Process / runtime metrics

`prom-client`'s `collectDefaultMetrics` exposes Node.js process metrics under the standard names:

- `process_cpu_user_seconds_total`, `process_cpu_system_seconds_total`
- `process_resident_memory_bytes`
- `nodejs_eventloop_lag_seconds` — **the canary for "Node is overloaded"**
- `nodejs_active_handles`, `nodejs_active_requests`
- `nodejs_heap_size_used_bytes`, `nodejs_heap_size_total_bytes`

**Watch event-loop lag.** If it climbs above ~50 ms, JavaScript is starving — usually from a sync hot path or GC pressure. Pair with CPU.

---

## Log fields (ECS-aligned)

> Backend logs are JSON, written to stdout by `pino` and `pino-http`. Filebeat tails the container, parses the docker wrapper + ndjson, and ships to Elasticsearch data stream `logs-app.ecom-dev`. ECS field names are used so Elasticsearch's default mappings work and the AI agent can search by familiar key paths.

### Always present

| Field | Type | Meaning |
|---|---|---|
| `@timestamp` | date (ISO 8601) | Event time |
| `log.level` | keyword | `info\|warn\|error\|debug` |
| `message` | text | Human-readable event description (e.g., `"payment recorded"`) |
| `service.name` | keyword | `shop-backend` |
| `service.version` | keyword | From `package.json` |
| `service.environment` | keyword | `development` for local stack |

### HTTP-request logs (emitted by `pino-http`)

| Field | Type | Meaning |
|---|---|---|
| `http.request.method` | keyword | `GET`, `POST`, … |
| `url.path` | keyword | Resolved request path (e.g., `/api/products/42`) — **note this is the resolved path, not the template**; for aggregation use the Prom `route` label instead |
| `http.response.status_code` | long | 200, 401, 500, … |
| `event.duration` | long (nanoseconds, ECS standard) | Request duration |
| `event.outcome` | keyword | `success` for 2xx-3xx, `failure` for 4xx-5xx (pino-http customLogLevel maps this) |
| `trace.id` | keyword | Random ID per request, propagated through child logs |
| `user.id` | keyword | Numeric user id if request authenticated, else absent |

### Custom `ecom.*` namespace

| Field | Type | Where it appears | Meaning |
|---|---|---|---|
| `ecom.order_id` | keyword | checkout/payment events | The order number the event refers to |
| `ecom.payment_status` | keyword | payment events | `succeeded`, `failed` |
| `ecom.payment_amount_cents` | long | payment events | The amount the user attempted |
| `ecom.error_code` | keyword | error logs | The route's HttpError code: `empty_cart`, `insufficient_stock`, `order_not_payable`, `payment_declined`, `invalid_input`, `product_not_found`, `order_not_found`, `unauthorized` |
| `ecom.cart_item_count` | long | cart events | Items in the cart at the time of the event |
| `ecom.checkout_total_cents` | long | checkout events | Order total |

### Why two surfaces (metrics + logs)?

Metrics tell us **the rate and shape** of what's happening. Logs tell us **the why** for a specific event. The AI agent's triage loop reflects this: it queries Prometheus first to confirm a symptom (rate climbing, p95 spike), then drops to Elasticsearch with a narrow filter to find the *example* events that explain it. Don't try to count events using log search — counts in logs are misleading because of sampling and indexing delay. Don't try to inspect specifics using metrics — labels are bounded by design.

---

## Funnel queries (canonical)

The user journey funnel is the single most important multi-metric view. The AI should know these by heart:

```promql
# Browse → cart conversion (5m window)
sum(rate(ecom_cart_items_added_total[5m]))
  /
sum(rate(http_requests_total{route="/api/products"}[5m]))

# Cart → checkout conversion
sum(rate(ecom_checkouts_total{outcome="success"}[5m]))
  /
sum(rate(ecom_cart_items_added_total[5m]))

# Checkout → payment conversion (success-on-success)
sum(rate(ecom_payments_total{outcome="succeeded"}[5m]))
  /
sum(rate(ecom_checkouts_total{outcome="success"}[5m]))

# Overall: visits → paid
sum(rate(ecom_payments_total{outcome="succeeded"}[5m]))
  /
sum(rate(http_requests_total{route="/api/products"}[5m]))
```

Read the funnel left to right. The leftmost ratio that breaks tells you where the user dropped out.

---

## Examples (the strong-vs-weak test)

> Weak: "checkout p95 is 800ms."
> Strong: "checkout p95 is 800ms, driven entirely by the payment step (p95 1.2s); DB query latency is flat, so it's not us — likely the payment provider."

Every entry above is written to enable that kind of narrative. The AI should consume the catalog, query the relevant signals, then write conclusions in the strong-form pattern: **what is anomalous + which metric isolates the cause + what's NOT anomalous (the negative evidence) + recommended next action.**
