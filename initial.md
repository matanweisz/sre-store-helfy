# initial.md — Bootstrap Prompt

> Paste this into Cline (VS Code/Cursor extension) — or any agentic coding LLM with filesystem + shell access — against a fresh checkout of this repo to produce the working observability stack and AI service.
>
> This file is graded on **prompt rigor**: re-running it on a fresh copy should produce a comparable result. It is self-contained, references its companion docs, and has explicit stop-and-verify gates at every phase.

---

## Role

You are a Site Reliability Engineer. You are joining a project that is a Node.js + Express + React eCommerce app, deliberately uninstrumented. Your job is to add the observability layer — Prometheus, structured logs to Elasticsearch, Grafana with provisioned dashboards — and stand up a standalone AI observability service that can investigate the running system via multi-turn LLM tool calls. Your work is graded on whether **an on-call engineer (and an LLM) can use what you build to reason about a live system**, not on whether you wired up every default exporter.

## Read these first

1. **`./guidelines.md`** — log format, metric naming, cardinality rules, error-surfacing rules, the reusable procedures (how to add a metric, how to emit a searchable log, how to add a Grafana panel, common PromQL patterns, the triage loop). **The triage loop is the most important part of this file** — the AI runtime agent must follow it.
2. **`./metric-catalog.md`** — every metric and log field, with one-line description, why it matters, what normal looks like, what a change implies. Every metric in this catalog must be exposed in Prometheus and reachable by the AI service. Catalog ↔ running system ↔ LLM runtime context must agree.
3. **`./docker-compose.yml`** — the existing stack (MySQL + backend + frontend). You will extend this file.

If you can't reconcile what these three files say, **ask before writing code**. Don't invent metric names. Don't invent log field names.

## The app you're instrumenting

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | /api/auth/signup | — | Create account, returns JWT |
| POST | /api/auth/login | — | Login, returns JWT |
| GET | /api/products | — | List products (supports `?search=` and `?category=`) |
| GET | /api/products/:id | — | Get single product |
| GET | /api/products/:id/related | — | **Deliberately slow** — un-indexed self-join across `order_items`. Latency grows with order volume. |
| GET | /api/cart | JWT | Get current user's cart |
| POST | /api/cart/items | JWT | Add item to cart |
| DELETE | /api/cart/items/:productId | JWT | Remove item from cart |
| POST | /api/checkout | JWT | Cart → order. Errors: `empty_cart`, `insufficient_stock` |
| GET | /api/checkout/:orderId | JWT | Get order details |
| POST | /api/payment | JWT | Pay (mock provider). Errors: `order_not_payable`, `payment_declined` |
| GET | /api/orders | JWT | List user's orders |
| GET | /healthz | — | Health check |

### User journey (the thing being measured)

> **browse → cart → checkout → payment**

### Intentional behaviors worth instrumenting (gifts from the assignment authors)

- **`GET /api/products/:id/related`** runs an un-indexed self-join across `order_items`. Latency grows with order volume. → instrument with a named DB query histogram (`db_query_duration_seconds{query_name="products_related"}`).
- **Payment latency is uniformly random in `[120 ms, 450 ms]`.** A p95 histogram panel will show this clearly; a p50 panel will not — that difference matters.
- **`PAYMENT_FAILURE_RATE` env var (default 0.08)** is hot-configurable. Turning it up and watching the error-rate panel respond is how an evaluator proves your instrumentation works. Bake this into the demo.
- **Distinct error codes** at checkout (`empty_cart`, `insufficient_stock`) and payment (`order_not_payable`, `payment_declined`). Each is a counter label and a `ecom.error_code` log field.
- **Product search uses `LIKE '%...%'`** with no full-text index. On 120 rows it's fast; the slow path will appear under volume — your HTTP latency histogram captures it.

## Order of work, with hard verify gates

You implement in **five phases**. After each phase, you stop and run the verify gate. If it fails, fix it before moving on. Do **not** combine phases.

---

### Phase 1 — Backend instrumentation (metrics)

**Goal:** the backend exposes Prometheus metrics on `/metrics` and Prometheus scrapes them.

**Do:**

1. `cd backend && npm install prom-client@^15`.
2. Create `backend/src/metrics.ts`. It must:
   - Construct a dedicated `Registry` (not the default global).
   - Call `client.collectDefaultMetrics({ register })` (Node process metrics).
   - Define every metric named in `metric-catalog.md`: `http_requests_total`, `http_request_duration_seconds`, `http_requests_in_flight`, `ecom_cart_items_added_total`, `ecom_checkouts_total`, `ecom_payments_total`, `ecom_payment_amount_cents_total`, `ecom_order_value_cents`, `auth_login_attempts_total`, `db_query_duration_seconds`.
   - Use the latency buckets defined in `guidelines.md` §2 for HTTP and DB histograms.
   - Export an Express middleware `metricsMiddleware` that increments `http_requests_total` and observes `http_request_duration_seconds` on every response, labeled `method, route, status_code`. Capture `req.route?.path` post-routing — use the Express **template**, never the resolved path. For unmatched paths label `route="unmatched"`.
   - Export a `getMetrics()` helper that returns the registry's text exposition.
3. Wire it in `backend/src/index.ts`:
   - Install the middleware after `express.json()`, before the route mounts.
   - Expose `app.get('/metrics', ...)` returning `register.contentType` + `getMetrics()`. **Do not** wrap the metrics endpoint itself in the timing middleware.
4. Add business-counter increments at the right route call sites (the existing routes already throw `HttpError` with the right codes — you just count by outcome):
   - `routes/auth.ts` — `auth_login_attempts_total{outcome: "succeeded"|"invalid_credentials"}`.
   - `routes/cart.ts` — `ecom_cart_items_added_total{product_category}` after the cart insert.
   - `routes/checkout.ts` — `ecom_checkouts_total{outcome: "success"|"empty_cart"|"insufficient_stock"}`; on success also observe `ecom_order_value_cents`.
   - `routes/payment.ts` — `ecom_payments_total{outcome: "succeeded"|"failed", provider: "mock-stripe"}` and `ecom_payment_amount_cents_total{provider, outcome}`.
5. Wrap DB timing only for the three named queries from the catalog: `products_related`, `checkout_create_order`, `payment_record`. Use a small `time(query_name, fn)` helper around the call site — do **not** rewrite the whole `db.ts` layer.
6. Create `prometheus/prometheus.yml`. Single scrape job named `shop-backend`, target `backend:4000`, scrape interval `15s`. No remote_write.
7. Extend `docker-compose.yml`:
   - Add a `prometheus` service using `prom/prometheus:v3.6.0` (or the current stable v3.x at the time of build — pin to a digest if possible).
   - Mount `./prometheus/prometheus.yml` to `/etc/prometheus/prometheus.yml`.
   - Publish port `9090:9090`.
   - Attach to the existing `shop` network.

**Verify (must all pass before Phase 2):**

```bash
docker compose up --build -d
sleep 10
curl -s localhost:4000/metrics | grep -E '^(http_requests_total|http_request_duration_seconds_bucket|ecom_payments_total|nodejs_eventloop_lag_seconds)' | head -5
# Drive some traffic
for i in {1..30}; do curl -s localhost:4000/api/products > /dev/null; done
# Confirm Prometheus is scraping the target
curl -s 'localhost:9090/api/v1/targets' | jq -r '.data.activeTargets[] | "\(.labels.job) \(.health)"'
# Should print: shop-backend up
curl -s 'localhost:9090/api/v1/query?query=rate(http_requests_total[1m])' | jq '.data.result | length'
# Should be > 0
```

**Commit:** `feat(backend): prom-client instrumentation + prometheus service`.

---

### Phase 2 — Structured logs to Elasticsearch

**Goal:** every backend HTTP request and every business event produces a single JSON log line on stdout; Filebeat tails the container and indexes into Elasticsearch data stream `logs-app.ecom-dev`.

**Do:**

1. `cd backend && npm install pino pino-http`.
2. Create `backend/src/logger.ts`:
   - `pino` configured with `timestamp: pino.stdTimeFunctions.isoTime`, `messageKey: 'message'`, and `formatters.level` to emit the level **string name** (not the integer).
   - A `base` object with `service.name`, `service.version` (from `process.env.npm_package_version || package.json.version`), `service.environment` (from `process.env.NODE_ENV || 'development'`).
   - Export both the bare `logger` and a `pinoHttp` middleware. The middleware uses `customLogLevel` to auto-promote 4xx → `warn`, 5xx → `error`. Custom `genReqId` produces UUID-style `trace.id` (`crypto.randomUUID()`). Wire `user.id` from `req.user?.id` once auth has set it.
3. Wire `pinoHttp` middleware into `backend/src/index.ts` — **after** CORS, **before** the metrics middleware, so every later log inherits `req.log` with the trace id.
4. Replace `console.error('unhandled_error', err)` in the error handler with `req.log.error({err, ecom: {error_code: err.code}}, 'unhandled error')`.
5. Emit structured business events at the same call sites you added counter increments:
   - payment.ts → `req.log.info({ecom: {order_id, payment_status: status, payment_amount_cents: totalCents}}, 'payment recorded')` after the DB insert; on `failed` outcome the log level is `warn` (because the `HttpError(402, ...)` that follows will promote it to warn via `customLogLevel`).
   - checkout.ts → `'order_created'` (info) and `'checkout blocked'` (warn) with `ecom.error_code`.
   - auth.ts → `'login_succeeded'` (info) and `'login failed'` (warn).
6. Add to `docker-compose.yml`:
   - `elasticsearch:9.4.1`, single node, `xpack.security.enabled=false`, `discovery.type=single-node`, `ES_JAVA_OPTS=-Xms1g -Xmx1g`. Publish port 9200. Add a `healthcheck` that hits `/_cluster/health` until status is at least yellow.
   - `kibana:9.4.1` pointed at the ES service. Publish 5601. Optional but useful for the evaluator.
   - `filebeat:9.4.1`. Run as `root` (required for the docker socket). Mount `/var/run/docker.sock`, `/var/lib/docker/containers:ro`, and `./filebeat/filebeat.yml` to `/usr/share/filebeat/filebeat.yml`. Depend on `elasticsearch` healthy.
7. `filebeat/filebeat.yml`:
   - `filebeat.autodiscover.providers: [{type: docker, hints.enabled: false, templates: [{condition: {contains: {docker.container.name: "shop-backend"}}, config: [{type: container, paths: ["/var/lib/docker/containers/${data.docker.container.id}/*.log"], parsers: [{container: {format: docker, stream: all}}, {ndjson: {target: "", overwrite_keys: true, expand_keys: true, add_error_key: true}}]}]}]}]`.
   - `output.elasticsearch.hosts: ["http://elasticsearch:9200"]`, `output.elasticsearch.index: "logs-app.ecom-dev"`. Disable ILM for the demo to avoid the data-stream auto-creation dance: `setup.ilm.enabled: false`, `setup.template.enabled: false` — and explicitly set `output.elasticsearch.allow_older_versions: true`.
   - `processors: [{add_host_metadata: ~}, {drop_fields: {fields: ["agent", "ecs", "host", "input", "log.file", "stream", "container"], ignore_missing: true}}]` to keep the indexed document tight.

**Verify:**

```bash
docker compose up --build -d
# wait for ES health
until curl -sf localhost:9200/_cluster/health > /dev/null; do sleep 2; done
# generate at least one failed payment (deterministic): bump rate, drive 5 payments
docker compose stop backend
PAYMENT_FAILURE_RATE=0.8 docker compose up -d backend
sleep 5
# (log in, cart, checkout, pay 5x via curl — see README "Run it")
sleep 10
curl -s 'localhost:9200/logs-app.ecom-dev/_search?size=3' | jq '.hits.hits[0]._source | {at: ."@timestamp", lvl: .log.level, msg: .message, path: .url.path, ecom}'
# Should show ECS-keyed records including ecom fields
```

**Commit:** `feat: structured logging via pino + filebeat -> elasticsearch`.

---

### Phase 3 — Grafana with provisioned dashboard

**Goal:** Grafana on port 3000 opens straight to a User Journey dashboard with live data from Prometheus + Elasticsearch. No login.

**Do:**

1. `grafana/provisioning/datasources/datasources.yaml`:
   - `apiVersion: 1`, `prune: true`.
   - Two datasources with **pinned UIDs**: `uid: prometheus`, `uid: elasticsearch`.
   - Prometheus: `url: http://prometheus:9090`, `isDefault: true`, `jsonData.httpMethod: POST`, `jsonData.timeInterval: 15s`.
   - Elasticsearch: `url: http://elasticsearch:9200`, `jsonData.index: "logs-app.ecom-dev"`, `jsonData.timeField: "@timestamp"`, `jsonData.logMessageField: "message"`, `jsonData.logLevelField: "log.level"`.
2. `grafana/provisioning/dashboards/dashboards.yaml` — file provider, path `/var/lib/grafana/dashboards`, `updateIntervalSeconds: 10`, `allowUiUpdates: true`.
3. `grafana/dashboards/user-journey.json` — the dashboard JSON. Six panels (see `guidelines.md` §3). Variables: `route = label_values(http_requests_total, route)` (multi, includeAll). Every panel references its datasource by **uid** (`{"type": "prometheus", "uid": "prometheus"}` or `..."elasticsearch"`).
   - Panel 1: Time series. `sum by (route)(rate(http_requests_total{route=~"$route"}[1m]))`.
   - Panel 2: Stacked bars. `sum by (status_class)(label_replace(rate(http_requests_total{route=~"$route"}[1m]), "status_class", "${1}xx", "status_code", "(.).."))`.
   - Panel 3: Stat. `100 * sum(rate(http_requests_total{status_code=~"5..", route=~"$route"}[5m])) / sum(rate(http_requests_total{route=~"$route"}[5m]))`. Thresholds: 0 green, 1 yellow, 5 red.
   - Panel 4: Time series, three queries. `histogram_quantile(0.50/0.95/0.99, sum by (route, le)(rate(http_request_duration_seconds_bucket{route=~"$route"}[5m])))`. Unit: seconds.
   - Panel 5: row of three Stat panels — cart adds rate, successful checkouts rate, successful payments rate, plus a fourth stat panel for conversion (`paid_orders / cart_adds`).
   - Panel 6: Logs panel, Elasticsearch datasource. Query: `log.level:(error OR warn)`. Show time, level, message, `url.path`, `ecom.error_code`.
4. `docker-compose.yml`:
   - `grafana/grafana:11.4.0` (or current stable 11.x or 13.x). Publish 3000.
   - Envs: `GF_AUTH_ANONYMOUS_ENABLED=true`, `GF_AUTH_ANONYMOUS_ORG_ROLE=Admin`, `GF_AUTH_DISABLE_LOGIN_FORM=true`, `GF_SECURITY_ALLOW_EMBEDDING=true`.
   - Mount `./grafana/provisioning` to `/etc/grafana/provisioning`, `./grafana/dashboards` to `/var/lib/grafana/dashboards`.

**Verify:**

```bash
docker compose up -d grafana
sleep 5
# anonymous access works
curl -s -o /dev/null -w '%{http_code}\n' localhost:3000/api/health
# datasources provisioned
curl -s 'localhost:3000/api/datasources' | jq '.[] | {name, uid, type}'
# dashboard exists
curl -s 'localhost:3000/api/search?type=dash-db' | jq '.[].title'
# should include "User Journey"
```

Open `http://localhost:3000`, navigate to the User Journey dashboard, generate some traffic, see the panels react.

**Commit:** `feat: grafana with provisioned User Journey dashboard`.

---

### Phase 4 — AI observability service

**Goal:** a Python service in `ai-service/` exposes `POST /investigate` and a CLI. The LLM runs a real multi-turn loop with tool calls against Prometheus + Elasticsearch.

**Tooling pinned by the project owner:**

- Language: **Python 3.12**.
- HTTP framework: **FastAPI**.
- LLM SDK: **`openai`** Python SDK pointed at OpenRouter (`base_url=https://openrouter.ai/api/v1`).
- Primary model: **`anthropic/claude-sonnet-4.6`** (best price/quality for 5–10-turn agentic tool calling on OpenRouter as of May 2026).
- Tool-call style: **native OpenAI-compatible function calling**. No MCP server (mention as an upgrade path in comments).

**Do:**

1. `ai-service/pyproject.toml` — deps: `fastapi`, `uvicorn[standard]`, `openai>=1.50`, `httpx`, `pydantic>=2`. Pin minor versions.
2. `ai-service/Dockerfile` — `python:3.12-slim`, pip install, copy code, `CMD ["uvicorn", "ai_service.app:app", "--host", "0.0.0.0", "--port", "8000"]`.
3. `ai-service/ai_service/tools.py` — exposes **four tools**:
   - `get_metric_catalog()` — read `metric-catalog.md` (mounted into the container), return as a string. Cap at ~6 KB.
   - `query_prometheus(promql: str, time_range: Literal["5m","15m","1h","24h"])` — hit `{PROMETHEUS_URL}/api/v1/query_range` with `start = now - range`, `end = now`, `step` chosen to keep result ≤ 100 points. Return: `{"resultType": ..., "series": [{"labels": {...}, "samples": [[t, v], ...], "p50": ..., "p95": ...} for the top 10 series]}`. **Pre-aggregate** — if there are >10 series, return the top 10 by sum and a count of dropped series.
   - `search_logs(query: str, time_range: enum, size: int = 20)` — POST `{ELASTICSEARCH_URL}/logs-app.ecom-dev/_search` with a `bool` filter on `@timestamp >= now - range` and a Lucene `query_string` on the user's query. Return `{total, hits: [{"@timestamp", "log.level", "message", "url.path", "http.response.status_code", "ecom"}]}` — strip everything else to keep payload small.
   - `get_recent_errors(route: str, time_range: enum)` — convenience wrapper: ES aggs query that buckets the last `time_range` of log records by `ecom.error_code` for the given `url.path`. Returns `{"counts": {"payment_declined": 12, ...}, "total": N}`.
   - Each tool returns errors as `{"error": "...", "hint": "..."}` JSON, never raises — the LLM should self-correct on the next turn.
   - Build the `tools` JSON-schema list (OpenAI tool schema) and a `TOOL_REGISTRY` dict by tool name.
4. `ai-service/ai_service/prompts.py` — system prompt **copy this verbatim** (it encodes the triage loop):

   ```
   You are an SRE doing live triage on the eCommerce app described in metric-catalog.md.
   Follow this loop on every question:

   1. HYPOTHESIZE — state your initial suspicion in one sentence before any tool call.
   2. CONFIRM — call the cheapest tool that could falsify the hypothesis. If unsure about
      metric or field names, call get_metric_catalog first.
   3. NARROW — if confirmed, drill into the specific route, query_name, or error_code.
   4. CHECK NEGATIVE EVIDENCE — confirm one thing that should be true if your hypothesis
      is right and false if it is wrong. This is what separates strong from weak output.
   5. CONCLUDE — write a 3-paragraph incident note for a human on-call:
      (a) what is anomalous, with numbers and time window,
      (b) supporting evidence including what is NOT anomalous (the negative evidence),
      (c) one concrete next action.

   Hard rules:
   - Never invent metric or field names. If unsure, call get_metric_catalog.
   - Always include the time window in your conclusion.
   - Prefer 2–4 tool calls. If 8+ calls without a confident hypothesis, report inconclusive
     and list what you would check next.
   - Output is an incident note for a human on-call. Not JSON. Not bullet lists of numbers.
   - Strong: "checkout p95 is 800ms, driven entirely by the payment step (p95 1.2s); DB
     query latency is flat, so it's not us — likely the payment provider."
     Weak: "checkout p95 is 800ms."
   ```

5. `ai-service/ai_service/app.py` — the FastAPI app + agent loop:
   - `OpenAI(base_url="https://openrouter.ai/api/v1", api_key=os.environ["OPENROUTER_API_KEY"])`.
   - Model from `OPENROUTER_MODEL` env, default `anthropic/claude-sonnet-4.6`.
   - `POST /investigate` body `{"question": str}`. Calls `investigate(question)`, returns `{"insight": str, "trace": [...], "iterations": int}`.
   - The loop: maintain `messages`, iterate up to `max_iters=10`. Each iteration: `client.chat.completions.create(model, messages, tools, tool_choice="auto", temperature=0.2)`. Append the assistant message. If no `tool_calls`, return the text content as `insight`. Else for each tool call, look up in registry, call, **truncate result JSON at 8000 chars**, append role=`"tool"` message with `tool_call_id`. Log every iteration to stdout with iter number, tool name, arg summary, and result preview (first 300 chars).
   - On `max_iters` reached without a tool-free answer, return `{"insight": "Hit iteration cap (10). Partial findings: ..." + last_text_content, "trace": [...], "iterations": max_iters}`.
   - CLI mode at the bottom: `if __name__ == "__main__": print(json.dumps(investigate(sys.argv[1])))`.
6. Add to `docker-compose.yml`:
   - `ai-service:` build context `./ai-service`. Publish 8000. Env: `OPENROUTER_API_KEY` from `.env`, `PROMETHEUS_URL=http://prometheus:9090`, `ELASTICSEARCH_URL=http://elasticsearch:9200`, `OPENROUTER_MODEL=anthropic/claude-sonnet-4.6`. Mount `./metric-catalog.md` into the container at `/app/metric-catalog.md` (read-only). Depends on `prometheus` and `elasticsearch` healthy.

**Verify:**

```bash
docker compose up --build -d ai-service
sleep 8
# 1. Catalog reachable
curl -s -X POST localhost:8000/investigate -H 'content-type: application/json' \
  -d '{"question":"is the metric catalog reachable? what metrics does it document?"}' \
  | jq '{insight, tools_called: [.trace[].tool] | unique}'
# Should show get_metric_catalog was called and the insight references real metric names

# 2. Real investigation — bump failure rate first
docker compose stop backend
PAYMENT_FAILURE_RATE=0.5 docker compose up -d backend
sleep 3
# Drive 20 payments
./scripts/drive-traffic.sh
sleep 30
# Ask
curl -s -X POST localhost:8000/investigate -H 'content-type: application/json' \
  -d '{"question":"anything wrong with payments in the last 5 minutes?"}' \
  | jq .
# Expect:
#   - trace has >= 2 distinct tool calls (not a single round-trip)
#   - insight is narrative, includes a number for the failure rate, includes the time window
#   - insight references at least one metric name from the catalog
```

**Commit:** `feat: AI observability service with multi-turn tool calling`.

---

### Phase 5 — End-to-end demo capture + README

**Goal:** the README is review-ready. It includes the architecture, run instructions, dashboard walkthrough with a screenshot, and one **real** AI investigation transcript captured from the live system.

**Do:**

1. Write `scripts/drive-traffic.sh` — logs in via the API, browses, carts, checks out, and pays in a 20-iteration loop with small sleeps so events are distributed in time.
2. Reset clean: `docker compose down -v && docker compose up --build -d`. Wait 30 s for health.
3. Drive normal traffic at default failure rate.
4. Bump failure rate (`PAYMENT_FAILURE_RATE=0.5`), restart backend, drive again.
5. `curl POST localhost:8000/investigate` with the question above → save full response to `docs/sample-investigation.json`.
6. Embed in README "Sample AI Investigation" section: the question, a short narration of which tools the LLM chose and why, then the final insight verbatim. The trace should show clear multi-turn reasoning.
7. Take a screenshot of the User Journey dashboard with traffic active → `docs/dashboard-screenshot.png`. Embed in README.
8. Fill in README sections that were placeholders: Observability Stack (metrics + logs), AI Service (architecture + tools + sample), Dashboard Walkthrough (per-panel), AI-Gap Awareness (cite the running `ai-log.md` notes), Tradeoffs (cardinality / sampling / log volume — per `guidelines.md` §7).

**Verify:**

```bash
# Fresh clone smoke test
rm -rf /tmp/clone-test && git clone . /tmp/clone-test
cp .env /tmp/clone-test/.env
cd /tmp/clone-test && docker compose up --build -d
sleep 60
curl -s -X POST localhost:8000/investigate -H 'content-type: application/json' \
  -d '{"question":"is everything healthy right now?"}' | jq .insight
```

If that returns a coherent narrative, you're done.

**Commit:** `docs: README polished + ready for review`.

---

## Stop conditions / guardrails

- **Never** label metrics with `user_id`, `order_id`, `payment_id`, `request_id`, `trace_id`, raw URL paths, error messages, SKUs, emails. The forbidden list is in `guidelines.md` §2.
- **Never** use `:latest` image tags. Pin every image.
- If a build fails: read the error, fix the root cause. Never `--no-verify` or skip hooks.
- Do not modify anything in `frontend/`. The PDF explicitly says polishing the app is not the point.
- Do not invent metric names. If a metric you want isn't in the catalog, **add it to the catalog first** in the same commit, with the four-line annotation (description, why, normal, change implies).
- If you find yourself prompting the LLM through five attempts to do the same thing, stop, write the code yourself, and add an entry to `ai-log.md` describing what the AI couldn't do. That honesty is itself graded.

## Definition of done (whole assignment)

- `docker compose up --build` boots all 7 services healthy: `mysql`, `backend`, `frontend`, `prometheus`, `elasticsearch`, `kibana`, `filebeat`, `grafana`, `ai-service`.
- `curl localhost:4000/metrics` exposes every metric named in `metric-catalog.md`.
- `curl localhost:9200/logs-app.ecom-dev/_search` returns ECS-shaped records with the fields named in `metric-catalog.md` "Log fields" section.
- `http://localhost:3000` opens to the User Journey dashboard without login; every panel renders with live data after traffic.
- `POST http://localhost:8000/investigate` returns a narrative answer; the `trace` array shows ≥ 2 distinct tool calls; the answer references catalog metric names and includes the time window.
- README has a real sample investigation captured from a live run.
- `ai-log.md` lists model choices and at least one honest manual-fix entry (the assignment explicitly grades this).
