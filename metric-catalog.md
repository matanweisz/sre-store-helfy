# Metric & Log Catalog

This file is the contract between the running system, the dashboards, and the AI agent. Each entry says what the signal is, why it matters, what normal looks like, and what a change implies — enough to investigate from, not just observe.

The catalog file is mounted into the AI service at `/app/metric-catalog.md` and loaded by the `get_metric_catalog` tool. The agent consults it before composing PromQL or Lucene queries so it doesn't guess at metric or field names.

---

## Conventions

- **Namespaces.** `http_*` (transport), `ecom_*` (business — user journey), `auth_*` (login/JWT), `db_*` (MySQL).
- **Counters** end in `_total`.
- **Durations** are in seconds (Prometheus naming convention).
- **Histogram buckets** for HTTP and DB latency: `[0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1, 1.5, 2.5, 5]`. Tuned for a 25 ms – 5 s envelope, with density around the payment route's 120–450 ms band.
- **Bounded labels only.** Forbidden in labels: `user_id`, `order_id`, `payment_id`, `cart_id`, `request_id`, `trace_id`, raw URL paths, free-text error messages, SKUs, email, IP. If you can't enumerate the possible values, it belongs in logs, not labels.
- **Route labels** use Express *templates* (`/api/products/:id`), never resolved paths.

---

## HTTP metrics

### `http_requests_total{method, route, status_code}`
- **Type:** Counter
- **What:** Every HTTP request the backend handles, counted at response time.
- **Labels:**
  - `method` — `GET|POST|PUT|DELETE`
  - `route` — Express template (`/api/products/:id`, `/healthz`, `unmatched` for 404s)
  - `status_code` — numeric string (`200`, `401`, `409`, `500`, …)
- **Normal:** Rate scales with traffic. Around 95%+ should be `2xx`. `/healthz` chatter is steady background.
- **A change implies:**
  - 5xx spike — the backend itself is broken. Pivot to `log.level:error`.
  - 4xx spike on `/api/payment` — most likely `payment_declined` (the mock provider is failing more than baseline).
  - 4xx spike on `/api/checkout` — `empty_cart` or `insufficient_stock`: stock vs. cart drift.
  - 401 spike on `/api/auth/login` — credential stuffing, or a frontend regression.

### `http_request_duration_seconds_bucket{method, route, status_code, le}`
- **Type:** Histogram (buckets above).
- **What:** End-to-end request latency, from middleware entry to `res.on('finish')`.
- **Use:** `histogram_quantile(0.95, sum by (route, le) (rate(http_request_duration_seconds_bucket[5m])))` gives p95 by route.
- **Normal:**
  - `/api/products` ≈ 5–30 ms
  - `/api/products/:id` ≈ 5–20 ms
  - `/api/products/:id/related` ≈ 30–200 ms, **climbs with order volume** (the deliberate slow query)
  - `/api/cart/*` ≈ 10–30 ms
  - `/api/checkout` ≈ 30–80 ms (multi-statement transaction)
  - `/api/payment` p95 ≈ 430 ms (uniform 120–450 ms mock-provider sleep)
- **A change implies:**
  - Payment p95 climbing past ~450 ms while checkout p95 stays flat — the payment provider is degrading independently. Out-of-scope for our team, but reportable.
  - Checkout p95 climbing while payment is flat — DB transaction contention. Check `db_query_duration_seconds{query_name="checkout_create_order"}`.
  - `/related` p95 creeping up over hours/days — the un-indexed self-join is hitting more rows. Expected growth pattern; consider an index or a cache when it matters.
  - p95 spike on every route at once — host-level (CPU, IO, GC).

### `http_requests_in_flight{method, route}`
- **Type:** Gauge
- **What:** Concurrent requests in flight. Incremented at entry, decremented at response.
- **Normal:** A handful per route at demo traffic.
- **A change implies:** A climb that doesn't return to baseline means requests are hanging. Pair with p99 and DB query duration to localize.

---

## Business metrics (user journey)

One counter per transition in the funnel: browse → cart → checkout → payment.

### `ecom_cart_items_added_total{product_category}`
- **Type:** Counter
- **What:** Each successful `POST /api/cart/items`. Labeled by the product's category (4 values in the seed: `home`, `kitchen`, `books`, `tech`).
- **Normal:** Tracks upstream `/api/products` traffic. Ratio against it gives the first funnel step.
- **A change implies:**
  - Drops to zero while `/api/products` rate stays normal — broken cart endpoint, even if the HTTP layer returns 200.
  - Spike on one category — marketing event or a scraper.

### `ecom_checkouts_total{outcome}`
- **Type:** Counter
- **What:** Each `POST /api/checkout`, labeled by business outcome.
- **Outcomes:** `success`, `empty_cart`, `insufficient_stock` (the route's own error codes — see `backend/src/routes/checkout.ts`).
- **Normal:** `success` dominates. `empty_cart` is near zero (the frontend prevents it). `insufficient_stock` is rare unless stock drifts.
- **A change implies:**
  - `insufficient_stock` rising — a popular item is sold out and blocking checkouts.
  - `empty_cart` rising — a frontend bug lets users hit checkout with no items.
  - `success` falling while cart rate is flat — start with the two failure outcomes above.

### `ecom_payments_total{outcome, provider}`
- **Type:** Counter
- **What:** Each `POST /api/payment` attempt. Outcome is the mock provider's decision, not the HTTP code. (HTTP returns 402 on decline, but the metric tells the business story.)
- **Outcomes:** `succeeded`, `failed`.
- **Provider:** `mock-stripe` (only one in scope).
- **Normal:** `failed` ≈ 8% (the default `PAYMENT_FAILURE_RATE=0.08`).
- **A change implies:**
  - `failed` rate above ~10% — either someone bumped `PAYMENT_FAILURE_RATE`, or the (real) provider is degrading. The agent should compute the rate and compare to the 8% baseline.
  - Both `succeeded` and `failed` drop to zero while checkouts still happen — `/api/payment` is broken upstream of the provider call.

### `ecom_payment_amount_cents_total{provider, outcome}`
- **Type:** Counter
- **What:** Sum of `amount_cents` from each payment attempt, by provider and outcome.
- **Use:** `rate(ecom_payment_amount_cents_total{outcome="succeeded"}[5m]) * 60` is revenue per minute.
- **Normal:** Tracks `ecom_payments_total{outcome="succeeded"}` × average order value. Average order in the seed data is roughly $50–$150.
- **A change implies:** Revenue drops without a payment-count drop — average order value has fallen (bulk discount, cart-size regression).

### `ecom_order_value_cents_bucket{le}`
- **Type:** Histogram
- **Buckets:** `[1000, 2500, 5000, 10000, 25000, 50000, 100000, 250000]` (cents).
- **What:** Distribution of order totals at checkout.
- **Normal:** Median is $70–$150 with the seed product set.
- **A change implies:**
  - p50 dropping — average basket size is shrinking.
  - p99 climbing sharply — either fraud or a single power user. Pivot to logs to find *which* orders.

---

## Auth metrics

### `auth_login_attempts_total{outcome}`
- **Type:** Counter
- **What:** Each `POST /api/auth/login`. Outcome reflects the route's logic, not the HTTP code.
- **Outcomes:** `succeeded`, `invalid_credentials`.
- **Normal:** Successes dominate. Some `invalid_credentials` from typos but it's a steady trickle.
- **A change implies:**
  - `invalid_credentials` spike — credential stuffing or a password rotation in progress.
  - `succeeded` falls to zero — either JWT signing is broken, or no one is trying to log in. Correlate with `http_requests_total{route="/api/auth/login"}`.

---

## Database metrics

### `db_query_duration_seconds_bucket{query_name, le}`
- **Type:** Histogram (same buckets as HTTP latency).
- **What:** Duration of named, instrumented MySQL queries.
- **`query_name` values:**
  - `products_related` — the deliberately slow self-join in `/api/products/:id/related`.
  - `checkout_create_order` — the multi-statement transaction in `/api/checkout`.
  - `payment_record` — the transaction in `/api/payment`.
- **Normal:**
  - `products_related` ≈ 20–80 ms on the seed dataset. Grows non-linearly with `order_items` row count.
  - `checkout_create_order` ≈ 15–40 ms.
  - `payment_record` ≈ 10–25 ms.
- **A change implies:**
  - `products_related` p95 doubling — `order_items` is large enough that the un-indexed join is biting. Add an index or cache.
  - `checkout_create_order` p95 climbing while `payment_record` stays flat — row-lock contention on `products` (the `UPDATE stock` inside the same transaction).
  - Any query over 1 s — connection pool exhaustion is likely next. Check `http_requests_in_flight`.

---

## Process / runtime metrics

From `prom-client`'s `collectDefaultMetrics`:

- `process_cpu_user_seconds_total`, `process_cpu_system_seconds_total`
- `process_resident_memory_bytes`
- `nodejs_eventloop_lag_seconds` — the canary for "Node is overloaded"
- `nodejs_active_handles`, `nodejs_active_requests`
- `nodejs_heap_size_used_bytes`, `nodejs_heap_size_total_bytes`

If event-loop lag climbs above ~50 ms, JavaScript is starving — usually from a sync hot path or GC pressure. Pair with CPU.

---

## Log fields (ECS-aligned)

Backend logs are JSON, one record per line, written to stdout by `pino` and `pino-http`. Filebeat tails the container, parses the docker wrapper plus our ndjson, and ships to Elasticsearch data stream `logs-app.ecom-dev`. Field names follow ECS so Elasticsearch's default mappings work and the agent can search by familiar paths.

### Always present

| Field | Type | Meaning |
|---|---|---|
| `@timestamp` | date (ISO 8601) | Event time |
| `log.level` | keyword | `info`, `warn`, `error`, `debug` |
| `message` | text | Short, present-tense event name (e.g., `"payment recorded"`) |
| `service.name` | keyword | `shop-backend` |
| `service.version` | keyword | From `package.json` |
| `service.environment` | keyword | `development` in the local stack |

### HTTP request fields (emitted by `pino-http`)

| Field | Type | Meaning |
|---|---|---|
| `http.request.method` | keyword | `GET`, `POST`, … |
| `url.path` | keyword | Resolved request path (e.g., `/api/products/42`). This is the resolved path, not the template — for aggregation use the Prom `route` label. |
| `http.response.status_code` | long | 200, 401, 500, … |
| `event.duration` | long (nanoseconds, ECS standard) | Request duration |
| `event.outcome` | keyword | `success` for 2xx–3xx, `failure` for 4xx–5xx |
| `trace.id` | keyword | Random ID per request, propagated through child logs |
| `user.id` | keyword | Numeric user id when the request is authenticated |

### Custom `ecom.*` namespace

| Field | Type | Where it appears | Meaning |
|---|---|---|---|
| `ecom.order_id` | keyword | checkout, payment events | The order number the event refers to |
| `ecom.payment_status` | keyword | payment events | `succeeded` or `failed` |
| `ecom.payment_amount_cents` | long | payment events | Amount the user attempted to pay |
| `ecom.error_code` | keyword | handled-error events | The route's HttpError code: `empty_cart`, `insufficient_stock`, `order_not_payable`, `payment_declined`, `invalid_credentials`, `invalid_input`, `product_not_found`, `order_not_found`, `email_taken`, `not_found` |
| `ecom.cart_item_count` | long | cart events | Items in the cart at the time of the event |
| `ecom.checkout_total_cents` | long | checkout events | Order total |

### Why metrics and logs both

Metrics give you the rate and shape of what's happening. Logs give you the why for a specific event. The triage loop reflects this: confirm a symptom in Prometheus (rate up, p95 spike), then narrow to Elasticsearch to find the example events that explain it.

Use the right surface. Don't try to count events from log search — sampling and indexing delay make counts unreliable. Don't try to inspect specifics from metrics — labels are bounded by design.

---

## Funnel queries

Four ratios in PromQL. Read them left to right; the leftmost ratio that breaks tells you where the user drops out of the journey.

```promql
# Browse → cart
sum(rate(ecom_cart_items_added_total[5m]))
  /
sum(rate(http_requests_total{route="/api/products"}[5m]))

# Cart → checkout
sum(rate(ecom_checkouts_total{outcome="success"}[5m]))
  /
sum(rate(ecom_cart_items_added_total[5m]))

# Checkout → payment (success-on-success)
sum(rate(ecom_payments_total{outcome="succeeded"}[5m]))
  /
sum(rate(ecom_checkouts_total{outcome="success"}[5m]))

# Visits → paid (end-to-end conversion)
sum(rate(ecom_payments_total{outcome="succeeded"}[5m]))
  /
sum(rate(http_requests_total{route="/api/products"}[5m]))
```

---

## Strong vs. weak output

The shape of a useful insight, as a reminder for both the agent and a human writing one:

> **Weak:** "checkout p95 is 800 ms."
>
> **Strong:** "checkout p95 is 800 ms, driven entirely by the payment step (p95 1.2 s); DB query latency is flat, so it's not us — likely the payment provider."

Every entry in this catalog is written so the agent can produce that kind of narrative: what is anomalous, the metric that isolates the cause, the metric that rules out alternatives (negative evidence), and a concrete next action.
