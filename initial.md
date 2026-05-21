# initial.md — Bootstrap Prompt

> Paste this into an agentic coding LLM (Cline, Cursor agent, Claude Code, etc.) with filesystem and shell access, against a fresh checkout of this repo. Run it end to end and you'll get the working observability stack and AI service described in `README.md`.
>
> The prompt is self-contained, references its two companion docs, and has explicit stop-and-verify gates at every phase. Re-running it on a fresh copy should produce a comparable result.

---

## Role

You are a Site Reliability Engineer joining a project — a Node.js + Express + React eCommerce app that is deliberately uninstrumented. Your job is to add the observability layer (Prometheus metrics, structured logs to Elasticsearch, Grafana with provisioned dashboards) and stand up a standalone AI observability service that investigates the running system via multi-turn LLM tool calls.

The work is graded on whether an on-call engineer (and an LLM) can use what you build to reason about a live system — not on whether you wired up every default exporter.

## Read these first

1. **[`guidelines.md`](./guidelines.md)** — log format, metric naming, cardinality rules, error-surfacing rules, the reusable procedures (how to add a metric, how to emit a searchable log, how to add a Grafana panel, common PromQL patterns, the triage loop). The triage loop in §6 is what the runtime agent follows.
2. **[`metric-catalog.md`](./metric-catalog.md)** — every metric and log field with description, normal range, and what a change implies. Every metric named here must be exposed in Prometheus and reachable by the AI service. The catalog, the running system, and the LLM's runtime context have to agree.
3. **`docker-compose.yml`** — the existing stack (MySQL + backend + frontend). You'll extend this.

If you can't reconcile what these three files say, ask before writing code. Don't invent metric names or log field names.

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

### User journey

**browse → cart → checkout → payment**

### Intentional behaviors worth instrumenting

These are gifts from the assignment authors — instrument them deliberately, don't just rely on defaults.

- **`GET /api/products/:id/related`** runs an un-indexed self-join across `order_items`. Latency grows with order volume. Wrap it with a named DB-query histogram (`db_query_duration_seconds{query_name="products_related"}`).
- **Payment latency is uniform in `[120 ms, 450 ms]`.** A p95 histogram panel shows this clearly; a p50 panel won't. The difference matters.
- **`PAYMENT_FAILURE_RATE` (default 0.08)** is hot-configurable. Bumping it and watching the error-rate panel respond is how the demo proves your instrumentation works.
- **Distinct error codes** at checkout (`empty_cart`, `insufficient_stock`) and payment (`order_not_payable`, `payment_declined`). Each becomes a counter label and an `ecom.error_code` log field.
- **Product search uses `LIKE '%...%'`** with no full-text index. Fast on 120 rows; the slow path appears under volume. The HTTP latency histogram captures it without extra work.

## Order of work, with verify gates

Five phases. Stop after each one and run its verify gate. If the gate fails, fix the root cause before moving on — don't combine phases.

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
   - `payment.ts` → `req.log.info({ecom: {order_id, payment_status: status, payment_amount_cents: totalCents}}, 'payment recorded')` after the DB insert. On the `failed` outcome the subsequent `HttpError(402, ...)` lets `pino-http`'s `customLogLevel` mark the *request log* as `warn`.
   - `checkout.ts` → `req.log.info({ecom: {order_id, checkout_total_cents, cart_item_count}}, 'order created')` after the transaction commits.
   - `auth.ts` → `req.log.info({user: {id}}, 'login succeeded')` after JWT issuance.
   - Failure events (`checkout blocked`, `login failed`, etc.) are auto-emitted by the global error handler — it catches every `HttpError` and emits `req.log.warn({ecom: {error_code: err.code}}, 'handled error: <code>')`. So you don't need to scatter `warn` calls around route bodies.
6. Add to `docker-compose.yml`:
   - `elasticsearch:9.4.1`, single node, `xpack.security.enabled=false`, `discovery.type=single-node`, `ES_JAVA_OPTS=-Xms1g -Xmx1g`. Publish port 9200.
   - **Healthcheck.** The 9.x image ships *without* `curl` AND `wget`. Use bash's built-in `/dev/tcp`: `test: ["CMD-SHELL", "exec 3<>/dev/tcp/localhost/9200 && echo -e 'GET /_cluster/health HTTP/1.0\\r\\n\\r\\n' >&3 && cat <&3 | grep -E '\"status\":\"(yellow|green)\"' || exit 1"]`. Yellow is acceptable for single-node.
   - `kibana:9.4.1` pointed at the ES service. Publish 5601. Optional but useful for log exploration.
   - `filebeat:9.4.1`, run as `root` (needed for the docker socket). The image's ENTRYPOINT is `filebeat`, so your `command:` must include the subcommand explicitly: `["filebeat", "-e", "--strict.perms=false"]`. Mount `/var/run/docker.sock`, `/var/lib/docker/containers:ro`, and `./filebeat/filebeat.yml` into `/usr/share/filebeat/filebeat.yml`. Depend on `elasticsearch: { condition: service_healthy }`.
7. `filebeat/filebeat.yml`:
   - Use the **`filestream` input**, not `container` — `type: container` was deprecated in Filebeat 9.x and will refuse to start. Each filestream input needs a unique `id`; use `shop-backend-${data.docker.container.id}`.
   - Docker autodiscover provider matching `docker.container.name: shop-backend`. Parsers: `container` (to strip the docker JSON-file envelope) then `ndjson` with `target: ""`, `overwrite_keys: true`, `expand_keys: true`, `add_error_key: true`.
   - `output.elasticsearch.hosts: ["http://elasticsearch:9200"]`, `output.elasticsearch.index: "logs-app.ecom-dev"`. Disable ILM and template setup for the demo: `setup.ilm.enabled: false`, `setup.template.enabled: false`, `output.elasticsearch.allow_older_versions: true`.
   - `processors: [{drop_fields: {fields: ["agent", "ecs.version", "input", "log.file", "log.offset", "stream", "container", "docker", "host.name"], ignore_missing: true}}]` keeps the indexed document tight.

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

**Goal:** a Python service in `ai-service/` exposes `POST /investigate` and a CLI. The LLM runs a real multi-turn loop with tool calls against Prometheus and Elasticsearch.

**Pinned choices:**

- Language: **Python 3.12**.
- HTTP framework: **FastAPI**.
- LLM SDK: **`openai`** Python SDK pointed at OpenRouter (`base_url=https://openrouter.ai/api/v1`).
- Primary model: **`anthropic/claude-sonnet-4.6`** — best price/quality for 5–10-turn agentic tool calling on OpenRouter as of May 2026.
- Tool-call style: native OpenAI-compatible function calling. No MCP server (mention it as the natural upgrade path in a docstring).

**Do:**

1. `ai-service/pyproject.toml` — deps: `fastapi`, `uvicorn[standard]`, `openai>=1.50`, `httpx`, `pydantic>=2`. Pin minor versions.
2. `ai-service/Dockerfile` — `python:3.12-slim`, pip install, copy code, `CMD ["uvicorn", "ai_service.app:app", "--host", "0.0.0.0", "--port", "8000"]`.
3. `ai-service/ai_service/tools.py` — four tools:
   - `get_metric_catalog()` — read `metric-catalog.md` (mounted into the container), return its contents as a string. Cap at ~16 KB; the catalog is the agent's main reference.
   - `query_prometheus(promql: str, time_range: Literal["5m","15m","1h","24h"])` — hit `{PROMETHEUS_URL}/api/v1/query_range` with `start = now - range`, `end = now`, `step` chosen to keep result ≤ 10 points per series. Pre-aggregate: top 10 series by max value, each with last/min/max/mean and p50/p95 baked in. When `series_count == 0`, return a hint suggesting `get_metric_catalog` — the agent will probably have misspelled a metric name.
   - `search_logs(query: str, time_range: enum, size: int = 10)` — POST `{ELASTICSEARCH_URL}/logs-app.ecom-dev*/_search` with a `bool` filter on `@timestamp >= now - range` and a `query_string` on the Lucene query. Return hits stripped to ECS essentials: `@timestamp`, `log.level`, `message`, `url.path`, `http.response.status_code`, `event.outcome`, `event.duration_ms`, `ecom.*`, `trace.id`.
   - `get_recent_errors(route: str, time_range: enum)` — convenience ES aggs query that buckets warn+error logs for an exact `url.path` by `ecom.error_code` and `http.response.status_code`. Returns counts plus a few sample lines.
   - Tools never raise. On failure they return `{"error": str, "hint": str}` JSON so the model can self-correct on the next turn.
   - Build the OpenAI-style `tools` JSON-schema list and a `TOOL_REGISTRY` dict by tool name.
4. `ai-service/ai_service/prompts.py` — system prompt. Copy verbatim:

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

5. `ai-service/ai_service/app.py` — FastAPI app and the agent loop:
   - `OpenAI(base_url="https://openrouter.ai/api/v1", api_key=os.environ["OPENROUTER_API_KEY"])`.
   - Model from `OPENROUTER_MODEL` env, default `anthropic/claude-sonnet-4.6`.
   - `POST /investigate` with body `{"question": str}` calls `investigate(question)` and returns `{"insight": str, "trace": [...], "iterations": int, "finish_reason": str}`.
   - The loop: maintain `messages`; iterate up to `max_iters=10`. Each iteration calls `client.chat.completions.create(model, messages, tools, tool_choice="auto", temperature=0.2)`, appends the assistant message, and — if there are no `tool_calls` — returns the text content as `insight`. Otherwise, look up each tool in the registry, call it, truncate result JSON at 8000 chars (16000 for `get_metric_catalog`), append a role=`"tool"` message with the matching `tool_call_id`, and continue.
   - **Log every iteration to stderr** (NOT stdout) so the CLI mode produces clean JSON on stdout. Use `logging.basicConfig(stream=sys.stderr, ...)`.
   - If `max_iters` is reached without a tool-free answer, return `finish_reason: "iteration_cap"` with the partial trace.
   - CLI mode: `if __name__ == "__main__": print(json.dumps(investigate(sys.argv[1])))`.
6. Add to `docker-compose.yml`:
   - `ai-service:` build context `./ai-service`. Publish 8000. Env: `OPENROUTER_API_KEY` from `.env`, `PROMETHEUS_URL=http://prometheus:9090`, `ELASTICSEARCH_URL=http://elasticsearch:9200`, `OPENROUTER_MODEL=anthropic/claude-sonnet-4.6`. Mount `./metric-catalog.md` into the container at `/app/metric-catalog.md` (read-only). Depends on `prometheus` started and `elasticsearch` healthy.

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

### Phase 5 — End-to-end demo and README

**Goal:** the README is review-ready. Architecture, run instructions, dashboard walkthrough, and one *real* AI investigation transcript captured from the live system.

**Do:**

1. `scripts/drive-traffic.sh` — logs in, fires a few bad-credential probes (to populate the warn-level log stream), then loops `N` iterations of browse + cart + checkout + pay with a small sleep between iterations so events distribute in time.
2. Reset clean: `docker compose down -v && docker compose up --build -d`. Wait 30 s for health.
3. Drive normal traffic at the default failure rate.
4. Bump the failure rate (`PAYMENT_FAILURE_RATE=0.5`), restart the backend, drive again.
5. `curl POST localhost:8000/investigate` with the question used in your demo → save the full response to `docs/sample-investigation.json`.
6. In the README's "Sample AI Investigation" section: include the question, a short narration of which tools the LLM picked and in what order, and the final insight verbatim. The trace should make multi-turn reasoning obvious.
7. Take a screenshot of the dashboard with traffic active (manually, in a browser — the Grafana image renderer plugin isn't included) and save it as `docs/dashboard-screenshot.png`. Embed it in the README.
8. Fill in the README's other sections: Observability Stack (metrics + logs), AI Service (architecture, tools, sample), Dashboard Walkthrough (per panel), AI-Gap Awareness (cite the running `ai-log.md`), Tradeoffs (cardinality / sampling / log volume — per `guidelines.md` §7).

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

## Stop conditions and guardrails

- **Never** label metrics with `user_id`, `order_id`, `payment_id`, `request_id`, `trace_id`, raw URL paths, error messages, SKUs, or emails. The full forbidden list is in `guidelines.md` §2.
- **Never** use `:latest` image tags. Pin every image to a specific version.
- If a build fails, read the error and fix the root cause. Don't `--no-verify` or skip hooks.
- Don't modify anything in `frontend/`. The assignment is explicit that polishing the app isn't the point.
- Don't invent metric names. If a metric you want isn't in the catalog, add it to the catalog first in the same commit (description + why it matters + normal + change implies).
- If you find yourself prompting the LLM through five attempts to do the same thing, stop, write the code yourself, and add an entry to `ai-log.md` describing what the LLM couldn't do. Honest manual-fix logs are part of the deliverable.

## Definition of done

- `docker compose up --build` brings up nine services healthy: `mysql`, `backend`, `frontend`, `prometheus`, `elasticsearch`, `kibana`, `filebeat`, `grafana`, `ai-service`.
- `curl localhost:4000/metrics` exposes every metric named in `metric-catalog.md`.
- `curl localhost:9200/logs-app.ecom-dev*/_search` returns ECS-shaped records with the fields listed in the catalog's "Log fields" section.
- `http://localhost:3000` opens to the User Journey dashboard without a login. Every panel renders with live data after traffic.
- `POST http://localhost:8000/investigate` returns a narrative answer. The `trace` array has at least two distinct tool calls. The answer references catalog metric names and includes the time window it analyzed.
- The README has a real sample investigation captured from a live run.
- `ai-log.md` lists model choices and at least one honest manual-fix entry.
