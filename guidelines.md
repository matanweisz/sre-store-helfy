# Observability Guidelines

> Conventions and reusable procedures the AI must follow when generating, querying, or investigating this system. Paired with [`metric-catalog.md`](./metric-catalog.md) (every signal documented) and [`initial.md`](./initial.md) (the bootstrap prompt).

The **investigation procedures** at the bottom of this file are the heart of the doc. The PDF: *"investigation procedures matter most — they're what let the AI reason, not just read."*

---

## 1. Log format

All backend logs are emitted by **pino** to stdout as single-line JSON, written one record per line (ndjson). Filebeat tails the container via the docker JSON-file driver and ships to Elasticsearch.

### Required fields on every record

| Field | Notes |
|---|---|
| `@timestamp` | ISO 8601 (`pino.stdTimeFunctions.isoTime`) |
| `log.level` | string: `info`, `warn`, `error`, `debug` |
| `message` | short human-readable event name, present-tense (`"payment recorded"`, `"checkout blocked"`) |
| `service.name` | `shop-backend` |
| `service.version` | from `package.json` |
| `service.environment` | `development` locally; `production` if `NODE_ENV=production` |

### HTTP requests (from `pino-http`)

Every request emits one log line at response time with: `http.request.method`, `url.path`, `http.response.status_code`, `event.duration` (ns), `event.outcome` (`success`/`failure`), `trace.id` (random per-request id), and `user.id` when authenticated.

### Business events

When the route does something interesting beyond returning a response, emit a dedicated log via `req.log.info(...)`:

- `payment_recorded` (after the payment row is inserted, before throwing decline)
- `order_created` (after checkout transaction commits)
- `checkout_blocked` (on `empty_cart` / `insufficient_stock`)
- `login_succeeded` / `login_failed`

Each business event adds the relevant `ecom.*` fields (see the catalog).

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

- **debug** — verbose; off in production.
- **info** — every successful operation worth seeing later. Default level.
- **warn** — 4xx HTTP responses (auto-promoted by `pino-http` `customLogLevel`), business outcomes that aren't errors but are unusual (`insufficient_stock`).
- **error** — 5xx HTTP responses, unhandled exceptions, `payment_declined` (the `failed` outcome is a *business error* even though the call to the provider succeeded).

---

## 2. Metric naming

| Prefix | Domain |
|---|---|
| `http_*` | HTTP transport (request rate, latency, in-flight) |
| `ecom_*` | Business events tied to the user journey |
| `auth_*` | Login / JWT |
| `db_*` | MySQL queries |
| `process_*`, `nodejs_*` | Default Node runtime metrics from `prom-client.collectDefaultMetrics` |

**Rules:**
- Counters end in `_total`.
- Durations in seconds (base SI unit per the [Prometheus naming guide](https://prometheus.io/docs/practices/naming/)).
- Sizes in bytes; amounts of money in `_cents` (since the app does).
- Histograms use the bucket array below for HTTP and DB latency; business histograms (`ecom_order_value_cents`) get their own bucket set documented in the catalog.

### Histogram buckets (HTTP + DB latency)

```ts
const LATENCY_BUCKETS = [0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1, 1.5, 2.5, 5];
```

These are tuned to the system's observed envelope: 25 ms minimum resolution for fast endpoints (`/api/products` simple SELECT), good density in 100–500 ms (payment route lives at 120–450 ms), with headroom to 5 s for degraded states.

### Forbidden labels (high cardinality)

Never label with: `user_id`, `order_id`, `payment_id`, `cart_id`, `request_id`, `trace_id`, raw URL paths (use the Express template `route`), free-text error messages (use a bounded `error_code` enum), product SKUs, emails, IPs.

The rule: a label value must come from a small, *bounded* set. If we can't enumerate the possible values, it doesn't belong in a label — it belongs in a log.

---

## 3. Dashboard layout

The "User Journey" dashboard (provisioned via `grafana/dashboards/user-journey.json`) is the single canonical view. Layout, top to bottom:

1. **RED row** — Request rate by route, status family stacked, error rate %, latency p50/p95/p99 by route. Four panels, each scoped by the `$route` variable.
2. **Funnel row** — Three stat panels showing rate of cart adds, successful checkouts, successful payments (last 5 min). Plus one conversion-ratio stat (`paid_orders / cart_adds`).
3. **Logs row** — Elasticsearch Logs panel pinned to `log.level:(error OR warn)`, last 15 min.

Why this order: an on-call engineer scans top-to-bottom. RED tells them *if* something is wrong. Funnel tells them *where in the user journey*. Logs tell them *what specifically*.

---

## 4. Error-surfacing rules

Every backend error must:

1. **Throw `HttpError(status, code, message)`** from the route (the existing pattern in `backend/src/util.ts`).
2. **Increment `http_requests_total`** with the resulting status code (the metrics middleware does this automatically once installed).
3. **Emit a structured log** at the appropriate level:
   - 4xx → `warn` with `ecom.error_code` populated
   - 5xx → `error` with `err.stack` attached
4. For *business* errors (payment declines, stock issues), also increment the relevant `ecom_*_total{outcome=...}` counter so the symptom is countable from metrics alone.

The error handler in `backend/src/index.ts` is the catch-all; route-level code increments the business counter before throwing.

---

## 5. Reusable procedures

### 5a. How to add a metric

1. Add the definition to `backend/src/metrics.ts` using the shared registry. Match the naming rules in §2.
2. **Update [`metric-catalog.md`](./metric-catalog.md)** with the new entry — description, normal range, what a change implies. *Do this in the same commit.* If it's not in the catalog the AI can't reason about it.
3. Find the call site(s) and increment / observe.
4. Restart backend (`docker compose restart backend`). Verify: `curl -s localhost:4000/metrics | grep <new_metric>` shows it.
5. Confirm Prometheus has scraped it: PromQL `<new_metric>` should return a value after one scrape interval (≤15 s).

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
- **First argument is a JSON object**; second is the `message` string. The reverse order silently drops the structured fields.
- **Use ECS field paths.** `event.outcome`, `ecom.order_id`, `service.name` — not invented top-level keys.
- **Keep `message` short and present-tense.** It becomes a searchable token; "payment recorded" beats "Successfully recorded payment for order 42 with status succeeded".

### 5c. How to add a Grafana panel

1. Edit `grafana/dashboards/user-journey.json` (the file Grafana provisioning watches).
2. Add a new entry to the `panels` array. Use `"datasource": {"type": "prometheus", "uid": "prometheus"}` (or `"uid": "elasticsearch"` for logs). The UIDs are pinned in `datasources.yaml`.
3. `docker compose restart grafana` is *not* required — file provisioning auto-reloads at `updateIntervalSeconds: 10`.

### 5d. Common PromQL patterns by symptom

| Symptom | Query |
|---|---|
| *Latency spike on one route* | `histogram_quantile(0.95, sum by (route, le)(rate(http_request_duration_seconds_bucket[5m])))` then filter by `route` |
| *Error rate climbing* | `sum(rate(http_requests_total{status_code=~"5.."}[5m])) / sum(rate(http_requests_total[5m]))` |
| *Payment failure rate spike* | `sum(rate(ecom_payments_total{outcome="failed"}[5m])) / sum(rate(ecom_payments_total[5m]))` — compare to baseline 0.08 |
| *Throughput drop* | `sum(rate(http_requests_total[5m]))` against the prior hour |
| *Specific DB query slow* | `histogram_quantile(0.95, sum by (query_name, le)(rate(db_query_duration_seconds_bucket[5m])))` |
| *Funnel drop* | the four ratio queries at the bottom of the catalog |
| *Event loop saturation* | `nodejs_eventloop_lag_seconds` — anything >50 ms is bad |

### 5e. Common Elasticsearch (Lucene) patterns

| Need | Query |
|---|---|
| All errors in the last 15m | `log.level:error` |
| Failures on a route | `log.level:(error OR warn) AND url.path:"/api/payment"` |
| Declined payments specifically | `ecom.error_code:payment_declined` |
| Slow request examples | `event.duration:>500000000` (ns — that's 500 ms) |
| All events for one order | `ecom.order_id:"42"` |

---

## 6. The triage loop (this is the part that matters)

When the AI is asked an SRE question ("anything wrong with payments in the last 15 min?"), it must follow this loop. The system prompt encodes it; the LLM uses the tools below to execute it.

### The loop

1. **HYPOTHESIZE.** Before any tool call, state in one sentence what's most likely happening given the question. *"User asked about payments — most likely the failure rate has climbed above 8%, or payment latency is up."*
2. **CONFIRM** with the cheapest tool that could falsify the hypothesis. Almost always:
   - First call `get_metric_catalog()` if metric names aren't already in context.
   - Then `query_prometheus(...)` with the right counter or histogram.
3. **NARROW** if the hypothesis is confirmed. Move from aggregate metrics down to specifics:
   - From `ecom_payments_total{outcome="failed"}` to *which orders failed* via `search_logs(query="ecom.error_code:payment_declined", ...)`.
   - From `http_request_duration_seconds` p95 spike to *one specific route* with the histogram broken by `route`.
4. **CHECK NEGATIVE EVIDENCE.** Before concluding, confirm one thing that *should* be true if the hypothesis is right and *false* if not. *"Payment latency is up — but is DB query duration also up? If yes, it's us. If no, it's the provider."* This is what separates strong from weak output.
5. **CONCLUDE.** Write a 3-paragraph narrative for a human on-call:
   - **(a) What's anomalous** — the symptom, with numbers and time window.
   - **(b) Supporting evidence** — the metrics that confirm + the metrics that rule out alternative causes.
   - **(c) Suggested next action** — concrete next step, e.g., "check payment provider status page", "bump pool size", "add an index to order_items".

### Worked example — "payments are slow"

1. **Hypothesize**: latency on `/api/payment` is up.
2. **Confirm**: `query_prometheus("histogram_quantile(0.95, sum by (route, le)(rate(http_request_duration_seconds_bucket[5m])))", "15m")` → see p95 for `/api/payment` is ~800 ms, baseline is ~430 ms. **Confirmed.**
3. **Narrow**: is the slowness in our DB writes or in the external provider call?
   - `query_prometheus("histogram_quantile(0.95, sum by (query_name, le)(rate(db_query_duration_seconds_bucket{query_name=\"payment_record\"}[5m])))", "15m")` → `payment_record` p95 is still ~15 ms. Not the DB.
4. **Negative evidence**: is the rest of the app fine?
   - p95 for `/api/products` and `/api/checkout` flat at ~30 ms and ~50 ms respectively. ✓
   - Payment failure rate is unchanged (8%). ✓
5. **Conclude**: *"Payment p95 latency climbed from ~430 ms to ~800 ms over the last 15 minutes. DB write latency is flat (~15 ms p95), and the rest of the app is unaffected. The added time is entirely in the external mock-stripe provider call. Next action: check provider status — this is upstream of us."*

### Anti-patterns to refuse

- **Single round trip with all data crammed in.** That's not investigation.
- **Hardcoded sequence.** If the LLM always calls the same three tools in the same order, the loop is fake. Each step's tool choice must depend on the previous step's result.
- **Numeric dump without narrative.** "p95 is 800ms" alone is weak. The catalog explains why p95 of 800 ms matters; the conclusion must use that explanation.
- **Hallucinated metric names.** If a metric name isn't in the catalog, the LLM must not invent it. Call `get_metric_catalog()` if uncertain.
- **Conclusions without a time window.** Every conclusion includes the window it was computed over.

### When to give up

If after 8+ tool calls the LLM still can't form a confident hypothesis, the right answer is "inconclusive — here's what I checked and what I'd look at next if I had more tools/data." Honest dead-ends beat fabricated certainty.

---

## 7. Cardinality, sampling, and cost tradeoffs

The PDF asks us to articulate these explicitly:

**Cardinality.** Every label dimension multiplies series count. With our current labels — `route` (~10 values), `method` (~4), `status_code` (~10) — the HTTP histogram is ~400 series before the bucket fan-out (×11 buckets = 4400 active series). Adding a label like `user_id` would multiply that by thousands; we forbid it. The right way to investigate per-user issues is via logs (where each event is a row, not a series).

**Sampling.** Not used. At demo scale every event fits. In production the right pattern would be head-based sampling for traces (which we don't have) and full retention for metrics + warn/error logs.

**Log volume vs cost.** A single index (`logs-app.ecom-dev`) with no ILM is fine for hours; at production scale we'd add hot/warm/cold tiering with rollover. We log every HTTP request — this is acceptable at our QPS. If the app's QPS grew 100x, we'd drop the 2xx GETs from logs and rely on metrics for those.
