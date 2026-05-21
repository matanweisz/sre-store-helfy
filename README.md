# AI-Driven Observability for an eCommerce App

**Junior AI SRE Home Assignment — Helfy** · Matan Weisz · May 2026

A Node.js + Express + React eCommerce app, instrumented from scratch, with an observability stack (Prometheus, Elasticsearch + Filebeat, Grafana) and a Python AI service that investigates the live system over multi-turn LLM tool calls.

The goal of the assignment isn't a polished app. It's an observability foundation an AI agent can actually reason about: signals named meaningfully, a catalog the LLM can read at runtime, and a triage procedure the agent follows step by step.

## Architecture

```
                  ┌─────────────────────────────────────────────────────────┐
                  │                       USER JOURNEY                       │
                  │   browse → cart → checkout → payment (mock provider)    │
                  └─────────────────────────────────────────────────────────┘
                                              │
                  ┌───────────────┐    ┌──────┴────────┐   ┌────────────────┐
                  │  Frontend     │    │   Backend     │   │   MySQL 8.4    │
                  │  React + Vite │───▶│  Node 20 +    │──▶│   (orders,     │
                  │  :5173        │    │  Express + TS │   │    products)   │
                  └───────────────┘    │  :4000        │   └────────────────┘
                                       │  /metrics     │
                                       │  pino stdout  │
                                       └──┬─────────┬──┘
                                          │         │
                          metrics scrape  │         │  Filebeat tails container
                                          ▼         ▼
                                   ┌────────────┐  ┌──────────────────┐
                                   │ Prometheus │  │  Elasticsearch   │
                                   │   :9090    │  │      :9200       │
                                   └─────┬──────┘  └────────┬─────────┘
                                         │                  │
                                         ▼                  ▼
                              ┌──────────────────────────────────────┐
                              │             Grafana  :3000           │
                              │   User Journey dashboard (RED+funnel)│
                              └──────────────────────────────────────┘
                                                ▲
                                                │  reads same metrics + logs
                                                │
                              ┌──────────────────────────────────────┐
                              │  AI Observability Service  :8000     │
                              │  FastAPI → OpenRouter (Sonnet 4.6)   │
                              │  Tools: get_metric_catalog,          │
                              │    query_prometheus, search_logs,    │
                              │    get_recent_errors                 │
                              │  Multi-turn agent loop (≤10 iters)   │
                              └──────────────────────────────────────┘
```

The catalog the AI reads, the metrics on `/metrics`, and the panels on the dashboard all use the same set of names. One source of truth for the whole system.

## Run it

> Tested on Rancher Desktop (dockerd/moby engine) on Apple Silicon. Budget ~6 GB of RAM for the 9 containers.

```bash
cp .env.example .env
# paste your OpenRouter API key into .env:
#   OPENROUTER_API_KEY=sk-or-v1-...

docker compose up --build -d
```

| Service | URL | Notes |
|---|---|---|
| Frontend | http://localhost:5173 | `demo@shop.local` / `demopass` |
| Backend | http://localhost:4000 | `/metrics`, `/healthz` |
| Prometheus | http://localhost:9090 | targets at `/targets` |
| Grafana | http://localhost:3000 | anonymous admin — opens straight on the dashboard |
| Elasticsearch | http://localhost:9200 | data stream `logs-app.ecom-dev` |
| Kibana | http://localhost:5601 | optional, for ad-hoc log exploration |
| AI service | http://localhost:8000 | `POST /investigate`, or CLI mode (below) |

### Drive the canonical demo

```bash
# Wave 1 — normal traffic, default 8% payment failure rate
./scripts/drive-traffic.sh 20

# Wave 2 — bump failures and drive again so the AI has something to find
docker compose stop backend
PAYMENT_FAILURE_RATE=0.5 docker compose up -d backend
./scripts/drive-traffic.sh 20

# Wait for Prometheus to scrape and Filebeat to ship
sleep 15

# Ask the AI
curl -s -X POST localhost:8000/investigate \
  -H 'content-type: application/json' \
  -d '{"question":"anything wrong with payments in the last 15 minutes?"}' | jq .

# Or via the CLI
docker compose exec ai-service python -m ai_service.app "anything wrong with payments?"
```

Within a minute of wave 2, the Grafana *Payment failure rate* panel goes red and the *Recent error logs* row fills with `ecom.error_code:payment_declined` events. A transcript from this exact run is below in [Sample AI Investigation](#sample-ai-investigation).

---

## Contents

- [Observability Stack](#observability-stack) — what's instrumented and why
- [AI Service](#ai-service) — agent loop, the four tools, system prompt
- [Dashboard Walkthrough](#dashboard-walkthrough) — what each panel is for
- [Sample AI Investigation](#sample-ai-investigation) — a real captured run
- [AI-Gap Awareness](#ai-gap-awareness) — where the AI fell short and I stepped in
- [Tradeoffs](#tradeoffs) — cardinality, sampling, log volume, MCP, model choice
- [Reproducing this Build](#reproducing-this-build) — the Blueprint trio

> The build was done with Claude Code (Opus 4.7) directed by me block by block. The runtime AI agent uses `anthropic/claude-sonnet-4.6` via the OpenRouter key Helfy provided. Full breakdown in [`ai-log.md`](./ai-log.md).

## Observability Stack

### Metrics — Prometheus

The backend exposes `/metrics` via [`prom-client@15`](https://github.com/siimon/prom-client), with a dedicated registry holding:

| Namespace | Metrics | Source |
|---|---|---|
| `http_*` | `requests_total{method,route,status_code}`, `request_duration_seconds` (histogram), `requests_in_flight` | One Express middleware in [`backend/src/metrics.ts`](./backend/src/metrics.ts) records every request post-routing. |
| `ecom_*` | `cart_items_added_total{product_category}`, `checkouts_total{outcome}`, `payments_total{outcome,provider}`, `payment_amount_cents_total`, `order_value_cents` (histogram) | Hand-placed at each route's outcome branch — one increment per business outcome. |
| `auth_*` | `login_attempts_total{outcome}` | [`backend/src/routes/auth.ts`](./backend/src/routes/auth.ts). |
| `db_*` | `query_duration_seconds{query_name}` (histogram) | A `time(name, fn)` wrapper applied at the three named queries in the catalog: `products_related`, `checkout_create_order`, `payment_record`. |
| `process_*`, `nodejs_*` | CPU, RSS, heap, **`eventloop_lag_seconds`** | `collectDefaultMetrics()`. The event-loop lag is the canary for "Node is overloaded." |

Histogram buckets for HTTP and DB latency: `[25, 50, 100, 200, 300, 500, 750, 1000, 1500, 2500, 5000] ms`. Dense around 100–500 ms (where the payment route's 120–450 ms uniform sleep lives), with headroom out to 5 s for degraded states.

Labels are bounded by design. Forbidden in labels (full list in [`guidelines.md`](./guidelines.md)): `user_id`, `order_id`, `payment_id`, `request_id`, `trace_id`, raw URL paths (templates only), error messages, SKUs. Per-entity questions belong in logs, not series.

Prometheus scrapes the backend every 15 seconds — config in [`prometheus/prometheus.yml`](./prometheus/prometheus.yml).

### Logs — pino → Filebeat → Elasticsearch

The backend writes one JSON record per line to stdout via [`pino`](https://github.com/pinojs/pino) + [`pino-http`](https://github.com/pinojs/pino-http). Filebeat tails the container and ships to Elasticsearch data stream `logs-app.ecom-dev` with ECS field names.

A typical record:

```json
{
  "@timestamp": "2026-05-21T11:09:28.636Z",
  "log": { "level": "info" },
  "service": { "name": "shop-backend", "version": "1.0.0", "environment": "development" },
  "message": "payment recorded",
  "url": { "path": "/api/payment" },
  "http": { "request": { "method": "POST" }, "response": { "status_code": 200 } },
  "event": { "duration": 312000000, "outcome": "success" },
  "trace": { "id": "657bd100-b8ac-44fb-bdca-94e92fa26804" },
  "user": { "id": "1" },
  "ecom": { "order_id": "5", "payment_status": "succeeded", "payment_amount_cents": 9586 }
}
```

The `ecom.*` namespace is what makes logs searchable for the AI: a specific order by `ecom.order_id`, failing payments by `ecom.payment_status:failed`, blocked checkouts by `ecom.error_code:insufficient_stock`. 4xx responses are auto-promoted to `warn`, 5xx to `error`, via `pino-http`'s `customLogLevel`.

Filebeat config: [`filebeat/filebeat.yml`](./filebeat/filebeat.yml). Uses the `filestream` input (`container` was deprecated in Filebeat 9.x — see [`ai-log.md`](./ai-log.md) for the migration).

## AI Service

### Architecture

A Python 3.12 FastAPI app in [`ai-service/`](./ai-service/), with `POST /investigate` and a CLI mode (`python -m ai_service.app "<question>"`). The agent loop is in [`ai-service/ai_service/app.py:investigate`](./ai-service/ai_service/app.py) — kept short and readable on purpose:

```python
for iteration in range(MAX_AGENT_ITERATIONS):           # cap = 10
    resp = client.chat.completions.create(              # OpenRouter, Sonnet 4.6
        model=OPENROUTER_MODEL,
        messages=messages, tools=TOOLS, tool_choice="auto", temperature=0.2,
    )
    msg = resp.choices[0].message
    messages.append(assistant_dump_of(msg))
    if not msg.tool_calls:                              # text-only -> done
        return InvestigateResponse(insight=msg.content, ...)
    for tc in msg.tool_calls:
        result = TOOL_REGISTRY[tc.function.name](**json.loads(tc.function.arguments))
        payload = json.dumps(result)[:TOOL_RESULT_CAP_CHARS]   # 8 KB cap
        messages.append({"role": "tool", "tool_call_id": tc.id, "content": payload})
```

The LLM client is the [`openai`](https://github.com/openai/openai-python) Python SDK pointed at OpenRouter's OpenAI-compatible API. Native function calling, not MCP — for four in-process Python functions co-located with the LLM client, MCP would add a JSON-RPC layer without buying anything. The natural upgrade path is in the module docstrings.

### The four tools

| Tool | Args | Returns | When to call |
|---|---|---|---|
| `get_metric_catalog` | — | The full `metric-catalog.md` (~14 KB) | First, if metric or field names are unclear. The catalog also documents what *normal* looks like for each signal. |
| `query_prometheus` | `promql`, `time_range: 5m\|15m\|1h\|24h` | Top-10 series by max, each with last/min/max/mean/p50/p95; raw samples capped at 10 points | Any numeric question: rates, latencies, histograms, ratios. |
| `search_logs` | `query` (Lucene), `time_range`, `size≤50` | ECS-stripped log hits | To find *specific events* explaining a metric anomaly. Don't use it for counting. |
| `get_recent_errors` | `route`, `time_range` | Aggregated `{error_code: count}` plus sample lines | Convenience for "what's the error breakdown on /X?" — cheaper than two `search_logs` calls. |

Tools never raise. Errors come back as `{"error": str, "hint": str}` so the LLM can self-correct on the next turn. Empty Prometheus results carry an explicit hint to consult the catalog — this fixed a metric-name hallucination during testing ([`ai-log.md`](./ai-log.md), Block 5).

### System prompt

Encodes the triage loop from [`guidelines.md`](./guidelines.md) §6:

```
1. HYPOTHESIZE — state your suspicion in one sentence before any tool call.
2. CONFIRM — call the cheapest tool that could falsify the hypothesis.
3. NARROW — drill into the specific route, query_name, or error_code.
4. CHECK NEGATIVE EVIDENCE — confirm one thing that should be true if your
   hypothesis is right and false if not.
5. CONCLUDE — write a 3-paragraph incident note for a human on-call:
   (a) what's anomalous + time window, (b) supporting + negative evidence,
   (c) one concrete next action.
```

Full prompt in [`ai-service/ai_service/prompts.py`](./ai-service/ai_service/prompts.py).

## Dashboard Walkthrough

`http://localhost:3000` opens directly on the **User Journey** dashboard in the `SRE` folder (anonymous admin is on). Three rows, top to bottom:

```
┌────────────────────────────────────────────────────────────────────────────┐
│ Row 1 — RED  (rate, errors, duration — what an on-call scans first)        │
│  ┌─────────────────────────┐  ┌──────────────────────────────────────┐    │
│  │ Request rate by route   │  │ Status family stacked (2xx/4xx/5xx)  │    │
│  │   timeseries (1m rate)  │  │   bar chart                          │    │
│  └─────────────────────────┘  └──────────────────────────────────────┘    │
│  ┌──────────┐  ┌──────────────────────────────────────────────────────┐   │
│  │ Err 5xx %│  │ Latency p50 / p95 / p99 by route                     │   │
│  │  stat    │  │   timeseries, one line per quantile×route            │   │
│  └──────────┘  └──────────────────────────────────────────────────────┘   │
├────────────────────────────────────────────────────────────────────────────┤
│ Row 2 — User journey funnel (cart → checkout → payment)                    │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────────────────────┐  │
│  │ cart adds│ │checkouts │ │ payments │ │ Payment failure rate         │  │
│  │   /min   │ │   /min   │ │   /min   │ │  threshold green→yellow→red  │  │
│  └──────────┘ └──────────┘ └──────────┘ └──────────────────────────────┘  │
├────────────────────────────────────────────────────────────────────────────┤
│ Row 3 — Recent error logs (Elasticsearch)                                  │
│  Logs panel, last 15m, log.level:(warn OR error). Click any row to        │
│  expand the structured fields: ecom.error_code, url.path, trace.id ...    │
└────────────────────────────────────────────────────────────────────────────┘
```

The layout follows what an on-call engineer actually does: row 1 says *if* something's wrong, row 2 says *where in the user journey*, row 3 says *what specifically*. A `$route` variable scopes the RED panels to one endpoint when you want to focus.

Snapshot from the canonical demo run, taken immediately before the AI investigation below (full panel data in [`docs/dashboard-state.json`](./docs/dashboard-state.json)):

```
Error rate 5xx           0.00 %        # internal stack healthy
Payment failure rate    37.9 %         # baseline is 8% — that's the canary
Payments succeeded/min   3.81
Checkouts succeeded/min  6.35

Latency p95 by route
  /api/payment/         477.7 ms       # at the catalog's 450ms ceiling
  /api/cart/items        24.6 ms
  /api/checkout/         23.8 ms
  /api/products/         23.8 ms
  /api/products/:id/related   23.8 ms  # slow query is fast on small data
```

That's the state the AI agent reasons about in the next section.

## Sample AI Investigation

Captured live from this stack with `PAYMENT_FAILURE_RATE=0.5` set. Full raw response is in [`docs/sample-investigation.json`](./docs/sample-investigation.json).

**The question:**

```bash
curl -X POST localhost:8000/investigate \
  -H 'content-type: application/json' \
  -d '{"question":"Anything wrong with payments in the last 15 minutes? Walk me through your reasoning and give me a triage-style writeup with a concrete next action."}'
```

**What the agent did** — 3 iterations, 7 tool calls, no fixed sequence:

| # | Iter | Tool | What for | Latency |
|---|---|---|---|---|
| 1 | 0 | `query_prometheus` | failure-rate ratio | 6 ms |
| 2 | 0 | `query_prometheus` | payment p95 latency | 5 ms |
| 3 | 0 | `query_prometheus` | total payment request rate | 5 ms |
| 4 | 1 | `get_metric_catalog` | confirm field names | 1 ms |
| 5 | 1 | `get_recent_errors` | `/api/payment` breakdown | 15 ms |
| 6 | 2 | `query_prometheus` | payment p95 re-check | 10 ms |
| 7 | 2 | `query_prometheus` | DB `payment_record` p95 | 4 ms |

Iter 0 sends three Prometheus queries in parallel. Iter 1 consults the catalog before drilling in. Iter 2 grabs DB latency as negative evidence before concluding. The agent is picking its next move from what it just learned, not running a hardcoded script.

**What it said:**

> **🚨 Incident Note — Payment Failure Rate Critically Elevated**
> *Window analyzed: last 15 minutes*
>
> **What is anomalous.** The payment failure rate has spiked dramatically above the 8% baseline. Over the last 15 minutes, the `ecom_payments_total{outcome="failed"}` ratio peaked at **67% and averaged ~44%** — roughly **5–8× the normal 8% baseline**. Every single failed payment carries the error code `payment_declined` (HTTP 402), meaning the mock-stripe provider is rejecting payments at a massively elevated rate. In the log sample, 19 of the 38 warn/error events in the window are `payment_declined` — a clean 50/50 split of success vs. failure, consistent with the metric.
>
> **Supporting evidence.** Failure rate max 67%, mean ~44% vs. the 8% baseline. 100% of failures are `ecom.error_code: payment_declined` with HTTP 402. There are no `order_not_payable`, `invalid_input`, or 5xx errors — the payment route itself is healthy; the provider is declining. **DB query latency is flat and normal:** `db_query_duration_seconds{query_name="payment_record"}` p95 is ~24 ms, well within the 10–25 ms baseline. Our internal database is not the cause.
>
> **Negative evidence (what rules out internal causes).** The DB `payment_record` query is perfectly healthy at ~24 ms. If this were a backend bug (broken payment logic, DB contention, code regression), we'd expect 5xx errors or elevated DB latency. We see neither. The failure mode is entirely at the **provider decision layer** — mock-stripe is declining cards, not our code failing to call it.
>
> **Concrete next action.** The mock-stripe provider (`PAYMENT_FAILURE_RATE` env var) is almost certainly configured well above the default 8%. This is the exact failure mode the catalog calls out: *"`failed` rate climbing above ~10% → either someone bumped `PAYMENT_FAILURE_RATE`."* Check the env var on the backend container; if it's been bumped, reset to `0.08` and redeploy.

Two things worth pointing out. The agent uses catalog metric names exactly as written (`db_query_duration_seconds{query_name="payment_record"}` — no typos, no guesses), and it quotes the catalog's own diagnostic guidance back at us. That's the catalog functioning as a runtime contract, not just docs.

## AI-Gap Awareness

The full record is in [`ai-log.md`](./ai-log.md). The honest summary:

**During the build** (Claude Code Opus 4.7, with me directing):

- *Express clears `req.baseUrl` on the error path.* My first metrics middleware labeled 401 failed-logins as `route="/login"` instead of `/api/auth/login`. Took three iterations of hook placement to find a working fix — a per-router `stampRouteTemplate` middleware that captures baseUrl while it's still correct.
- *Filebeat 9.x deprecated the `container` input.* The research I'd done in the planning phase was out of date. Filebeat told me on first start; one config edit to `type: filestream`.
- *ES 9.x strips both `curl` AND `wget`.* My initial `wget --spider` healthcheck silently failed and ES looked unhealthy from outside, even though it was green inside. Switched to a bash `/dev/tcp` probe.
- *pino-http serializers put fields under the key name.* My first run indexed `req.url.path` instead of `url.path`. Fixed by using `customProps` (which merges into the record root) and suppressing the serializers.
- *`req.url` is router-local; `req.originalUrl` is the full path.* Same family as the baseUrl bug. The logger had to switch.
- *Environment leftovers, not LLM mistakes:* a stale `osxkeychain` credsStore from Docker Desktop, and zoxide overriding `cd` in the shell snapshot. Both fixed via system config. Worth listing because the assignment asks for honesty.

**At runtime** (the AI agent investigating live):

- *Sonnet 4.6 hallucinated a metric name once.* It guessed `ecom_auth_attempts_total` (correct is `auth_login_attempts_total`). My `query_prometheus` originally returned `series_count: 0` with no explanation, so the model had to infer the name was wrong. I added an explicit "zero series — likely misspelled, call get_metric_catalog" hint. Next time around it pivoted correctly: log search → wrong-name query → hint → catalog → corrected query → conclusion. That's the multi-turn loop earning its keep.
- *No MCP layer.* Built natively. MCP is the right answer when these tools need to be reused across processes or agents (a Claude Desktop integration for on-call would be the obvious case). For four functions co-located with the LLM client, native function calling is simpler and equivalent.

## Tradeoffs

**Cardinality.** Every label dimension multiplies series count. With current labels — `route` (~10 values) × `method` (~4) × `status_code` (~10) — the HTTP histogram is ~400 series before the bucket fan-out (×11 buckets ≈ 4,400 active series). Adding a `user_id` label would multiply that by the user count and kill Prometheus. We forbid it. Per-user investigation belongs in logs, not metrics. The do-not-label list is in [`guidelines.md`](./guidelines.md) §2.

**Sampling.** None. Every HTTP request emits one log line and increments one counter. Fine at demo QPS, fine at moderate production QPS for a single instance. At 100× the QPS I'd drop 2xx GET logs and rely on metrics for them, then head-sample traces if we had any. We don't — tracing isn't in scope.

**Log volume.** Single ES data stream, no ILM, no rollover. Acceptable for a single-node demo. In production this becomes hot/warm/cold tiering with a rollover policy; here it would be ceremony without benefit.

**MCP vs. native function calling.** Native. The four tools live in the same process as the LLM client, so MCP's JSON-RPC layer would add complexity without changing behavior. MCP becomes the right answer when the same tools need to be reused across multiple agents.

**LLM choice.** `anthropic/claude-sonnet-4.6` via OpenRouter. The best price/quality on OpenRouter's tool-calling collection rankings as of May 2026 for 5–10-turn agent loops. Haiku 4.5 and Gemini Flash work for the loop mechanics but produce flatter narratives — and "insight, not numbers" is what gets graded. Opus 4.7 (via Claude Code) was only used at build time, never at runtime.

## Reproducing this Build

The three Blueprint files at the repo root — [`initial.md`](./initial.md), [`guidelines.md`](./guidelines.md), [`metric-catalog.md`](./metric-catalog.md) — are the bootstrap contract. `initial.md` is a self-contained prompt for an agentic coding LLM; run against a fresh checkout of the provided app in `sre-store.zip`, it produces an equivalent working stack. The prompts and models I used during the build are in [`ai-log.md`](./ai-log.md).
