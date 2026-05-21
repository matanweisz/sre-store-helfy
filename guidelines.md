# Observability Guidelines

Conventions and reusable procedures for instrumenting, querying, and investigating this system. Paired with [`metric-catalog.md`](./metric-catalog.md) (every signal documented) and [`initial.md`](./initial.md) (the bootstrap prompt).

§6 (the triage loop) is the heart of this file. It's what lets the agent reason about the system instead of just reading it.

---

## 1. Log format

Backend logs are emitted by `pino` to stdout, one JSON record per line. Filebeat tails the container via the docker JSON-file driver and ships to Elasticsearch.

### Required fields on every record

| Field | Notes |
|---|---|
| `@timestamp` | ISO 8601 |
| `log.level` | `info`, `warn`, `error`, `debug` |
| `message` | Short, present-tense event name (`"payment recorded"`, `"checkout blocked"`) |
| `service.name` | `shop-backend` |
| `service.version` | From `package.json` |
| `service.environment` | `development` locally; `production` when `NODE_ENV=production` |

### HTTP request fields (emitted by `pino-http`)

Every request emits one record at response time with: `http.request.method`, `url.path`, `http.response.status_code`, `event.duration` (ns), `event.outcome` (`success`/`failure`), `trace.id`, and `user.id` when authenticated.

### Business events

When a route does something interesting beyond returning a response, emit a dedicated log via `req.log.info(...)`. The events we emit today:

- `payment recorded` — after the payment row is inserted, before any decline throws.
- `order created` — after the checkout transaction commits.
- `handled error: <code>` — emitted by the global error handler for any HttpError; level is auto-promoted to warn/error.
- `login succeeded` — emitted after the JWT is issued.

Every business event includes the relevant `ecom.*` fields (see the catalog).

### Example log line

```json
{
  "@timestamp": "2026-05-21T14:03:11.482Z",
  "log": { "level": "info" },
  "message": "payment recorded",
  "service": { "name": "shop-backend", "version": "1.0.0", "environment": "development" },
  "http": { "request": { "method": "POST" }, "response": { "status_code": 200 } },
  "url": { "path": "/api/payment" },
  "event": { "duration": 184000000, "outcome": "success" },
  "trace": { "id": "4bf92f35-77b3-4da6-a3ce-929d0e0e4736" },
  "user": { "id": "1" },
  "ecom": { "order_id": "42", "payment_status": "succeeded", "payment_amount_cents": 15110 }
}
```

### Levels

- `debug` — verbose; off in normal operation.
- `info` — successful operations worth seeing later. Default.
- `warn` — 4xx HTTP responses (auto-promoted by `pino-http`'s `customLogLevel`), and business outcomes that aren't bugs but are unusual (`insufficient_stock`).
- `error` — 5xx HTTP responses and unhandled exceptions. (Note: `payment_declined` arrives via HTTP 402, so it lands as `warn`, even though it's a business failure. The metric `ecom_payments_total{outcome="failed"}` is the canonical count.)

---

## 2. Metric naming

| Prefix | Domain |
|---|---|
| `http_*` | HTTP transport (rate, latency, in-flight) |
| `ecom_*` | Business events on the user journey |
| `auth_*` | Login / JWT |
| `db_*` | Named MySQL queries |
| `process_*`, `nodejs_*` | Default Node runtime metrics from `collectDefaultMetrics` |

Rules:
- Counters end in `_total`.
- Durations in seconds (Prometheus naming convention).
- Sizes in bytes; money in `_cents` (the app deals in cents).
- HTTP and DB histograms share the same bucket array (below). Business histograms (`ecom_order_value_cents`) get their own buckets documented in the catalog.

### Histogram buckets (HTTP + DB latency)

```ts
const LATENCY_BUCKETS = [0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1, 1.5, 2.5, 5];
```

25 ms minimum resolution for fast endpoints, density around 100–500 ms (the payment route's 120–450 ms band), 5 s ceiling for degraded states.

### Forbidden labels

Never label with: `user_id`, `order_id`, `payment_id`, `cart_id`, `request_id`, `trace_id`, raw URL paths (use the Express template `route`), free-text error messages (use the bounded `error_code` enum), product SKUs, emails, IPs.

The rule: a label value must come from a small, bounded set. If you can't enumerate the possible values, it doesn't belong in a label — it belongs in a log field.

---

## 3. Dashboard layout

The "User Journey" dashboard ([`grafana/dashboards/user-journey.json`](./grafana/dashboards/user-journey.json)) is the canonical view. Layout, top to bottom:

1. **RED row.** Request rate by route, status family stacked (2xx/4xx/5xx), 5xx error rate %, latency p50/p95/p99 by route. Four panels, each scoped by the `$route` variable.
2. **Funnel row.** Three stat panels for the rate of cart adds, successful checkouts, and successful payments over the last 5 min. Plus one threshold-coloured stat for payment failure rate.
3. **Logs row.** Elasticsearch Logs panel pinned to `log.level:(warn OR error)`, last 15 min.

The order matches how on-call actually reads it. RED tells you if something is wrong. Funnel tells you where in the journey. Logs tell you what specifically.

---

## 4. Error-surfacing rules

Every backend error must:

1. **Throw `HttpError(status, code, message)`** from the route (existing pattern in `backend/src/util.ts`).
2. **Be counted** in `http_requests_total{status_code=...}`. The metrics middleware does this automatically.
3. **Emit a structured log** at the right level. 4xx → `warn`, 5xx → `error`. `pino-http`'s `customLogLevel` handles the request log; the global error handler emits a separate `handled error: <code>` log with `ecom.error_code` populated.
4. **For business errors** (payment declines, stock issues), also increment the matching `ecom_*_total{outcome=...}` counter at the call site, *before* throwing.

The error handler in `backend/src/index.ts` is the catch-all. Route-level code does the business-counter increment.

---

## 5. Reusable procedures

### 5a. How to add a metric

1. Define it in `backend/src/metrics.ts` on the shared registry. Follow the naming rules in §2.
2. Update [`metric-catalog.md`](./metric-catalog.md) in the **same commit** with the new entry: description, normal range, what a change implies. If it's not in the catalog the agent can't reason about it.
3. Find the call site(s) and increment / observe.
4. Restart the backend (`docker compose restart backend`). Confirm with `curl -s localhost:4000/metrics | grep <new_metric>`.
5. Confirm Prometheus scraped it: PromQL `<new_metric>` should return a value within one scrape interval (≤15 s).

### 5b. How to emit a searchable log

```ts
req.log.info(
  {
    ecom: { order_id: orderId, payment_status: status, payment_amount_cents: total }
  },
  'payment recorded'
);
```

Three rules:
- First argument is the JSON object; second is the message string. The reverse order silently drops the structured fields.
- Use ECS field paths (`event.outcome`, `ecom.order_id`, `service.name`), not invented top-level keys.
- Keep `message` short and present-tense. It becomes a searchable token. "payment recorded" beats "Successfully recorded payment for order 42 with status succeeded".

### 5c. How to add a Grafana panel

1. Edit `grafana/dashboards/user-journey.json` (the file the file-provider watches).
2. Add an entry to the `panels` array. Reference the datasource by **uid**: `{"type": "prometheus", "uid": "prometheus"}` or `{"type": "elasticsearch", "uid": "elasticsearch"}`. The UIDs are pinned in `datasources.yaml`.
3. No restart needed — provisioning auto-reloads at `updateIntervalSeconds: 10`.

### 5d. Common PromQL patterns by symptom

| Symptom | Query |
|---|---|
| Latency spike on one route | `histogram_quantile(0.95, sum by (route, le)(rate(http_request_duration_seconds_bucket[5m])))` then filter by `route` |
| Error rate climbing | `sum(rate(http_requests_total{status_code=~"5.."}[5m])) / sum(rate(http_requests_total[5m]))` |
| Payment failure rate spike | `sum(rate(ecom_payments_total{outcome="failed"}[5m])) / sum(rate(ecom_payments_total[5m]))` — compare to baseline 0.08 |
| Throughput drop | `sum(rate(http_requests_total[5m]))` against the prior hour |
| Specific DB query slow | `histogram_quantile(0.95, sum by (query_name, le)(rate(db_query_duration_seconds_bucket[5m])))` |
| Funnel drop | The four ratio queries at the bottom of the catalog |
| Event-loop saturation | `nodejs_eventloop_lag_seconds` — anything above ~50 ms is bad |

### 5e. Common Elasticsearch (Lucene) patterns

| Need | Query |
|---|---|
| All errors in the last 15m | `log.level:error` |
| Failures on a route | `log.level:(error OR warn) AND url.path:"/api/payment"` |
| Declined payments specifically | `ecom.error_code:payment_declined` |
| Slow request examples | `event.duration:>500000000` (ns — 500 ms) |
| All events for one order | `ecom.order_id:"42"` |

---

## 6. The triage loop

When the agent is asked an SRE question ("anything wrong with payments in the last 15 min?"), it follows this loop. The system prompt encodes it verbatim.

### The loop

1. **HYPOTHESIZE.** In one sentence, state what's most likely happening given the question. *"User asked about payments — probably the failure rate is up, or payment latency is up."*
2. **CONFIRM** with the cheapest tool that could falsify the hypothesis. Usually:
   - `get_metric_catalog()` first if metric names aren't already in context.
   - Then `query_prometheus(...)` with the right counter or histogram.
3. **NARROW** if the hypothesis is confirmed. Move from aggregate metrics down to specifics:
   - From `ecom_payments_total{outcome="failed"}` to *which orders failed*, via `search_logs(query="ecom.error_code:payment_declined", ...)`.
   - From a route-level p95 spike to *one specific route* using the histogram broken by `route`.
4. **CHECK NEGATIVE EVIDENCE.** Before concluding, confirm one thing that should be true if the hypothesis is right and false if it isn't. *"Payment latency is up — is DB query duration also up? If yes, it's us. If no, it's the provider."* This is what separates a strong conclusion from a weak one.
5. **CONCLUDE.** Write a short narrative for a human on-call:
   - (a) What's anomalous — the symptom, with numbers and a time window.
   - (b) Supporting evidence — the metrics that confirm it, and the metrics that rule out alternatives.
   - (c) One concrete next action — e.g., "check the payment provider's status page", "bump pool size", "add an index to `order_items`".

### Worked example — "payments are slow"

1. **Hypothesize.** Latency on `/api/payment` is up.
2. **Confirm.** `query_prometheus("histogram_quantile(0.95, sum by (route, le)(rate(http_request_duration_seconds_bucket[5m])))", "15m")` → p95 for `/api/payment` is ~800 ms vs. a baseline of ~430 ms. Confirmed.
3. **Narrow.** Is it our DB or the external provider call? `query_prometheus("histogram_quantile(0.95, sum by (query_name, le)(rate(db_query_duration_seconds_bucket{query_name=\"payment_record\"}[5m])))", "15m")` → `payment_record` p95 is ~15 ms. Not the DB.
4. **Negative evidence.** Is the rest of the app fine? p95 for `/api/products` and `/api/checkout` flat at ~30 ms and ~50 ms. Payment failure rate unchanged at 8%.
5. **Conclude.** *"Payment p95 latency climbed from ~430 ms to ~800 ms over the last 15 minutes. DB write latency is flat (~15 ms p95) and the rest of the app is unaffected. The added time is entirely in the external mock-stripe provider call. Next: check provider status — this is upstream of us."*

### Anti-patterns

- A single round trip with all the data crammed into the prompt. That's not investigation; it's a summary.
- A hardcoded sequence of the same three tools every time. If the agent isn't reacting to results, the loop is fake.
- A numeric dump without narrative. "p95 is 800 ms" doesn't mean anything on its own — the catalog explains why p95 of 800 ms matters, and the conclusion should use that.
- Invented metric names. If a name isn't in the catalog, call `get_metric_catalog()` before guessing.
- Conclusions without a time window. Always state the window the observation came from.

### When to give up

If you've made 8+ tool calls and still can't form a confident hypothesis, the right answer is "inconclusive — here's what I checked and what I'd look at next." An honest dead end beats fabricated certainty.

---

## 7. Cardinality, sampling, and cost

**Cardinality.** Every label dimension multiplies series count. With the current labels — `route` (~10 values), `method` (~4), `status_code` (~10) — the HTTP histogram is ~400 series before the bucket fan-out (×11 buckets ≈ 4,400 active series). Adding a `user_id` label would multiply that by however many users; we forbid it. Per-user investigation belongs in logs, where each event is a row, not a series.

**Sampling.** None. Every HTTP request emits one log line and increments one counter. Fine at demo scale; fine at moderate production scale. In production with much higher QPS, the right pattern is head-sampled traces, full retention for metrics, and selective dropping of 2xx GET logs (rely on metrics for those).

**Log volume.** Single ES data stream `logs-app.ecom-dev`, no ILM, no rollover. Acceptable for a single-node demo. At production scale this becomes hot/warm/cold tiering with policy-driven rollover.
