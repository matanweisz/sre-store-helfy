# AI-Driven Observability for an eCommerce App

**Junior AI SRE Home Assignment — Helfy** · Built by Matan Weisz · May 2026

A Node.js + Express + React eCommerce app instrumented from a deliberately-uninstrumented starter, plus a standalone AI observability service that investigates the running system via multi-turn LLM tool calls.

> **TL;DR:** the goal isn't a polished app. It's a foundation layer that lets an AI investigate logs, surface metrics, and produce real insight about what's happening — not just numbers. Three concerns drive every decision in this repo: signals that match the user journey (browse → cart → checkout → payment), bounded cardinality so Prometheus stays healthy, and a Blueprint (`initial.md` + `guidelines.md` + `metric-catalog.md`) that lets the build be re-run from scratch.

---

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
                                         ├──────────────────┤
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
                              │  Multi-turn agentic loop (iter ≤10)  │
                              └──────────────────────────────────────┘
```

The AI service and Grafana read from the same two backends (Prometheus + Elasticsearch). The catalog file the AI loads, the metrics on `/metrics`, and the panels on the dashboard all reference the **same set of metric names** — single source of truth.

## Run it

> Requires Docker + docker compose. Tested on Rancher Desktop (dockerd/moby engine), Apple Silicon, ~6 GB RAM headroom across the 9 containers.

```bash
cp .env.example .env
# Edit .env, paste your OpenRouter API key:
#   OPENROUTER_API_KEY=sk-or-v1-...
docker compose up --build -d
```

| Service | URL | Notes |
|---|---|---|
| Frontend | http://localhost:5173 | demo@shop.local / demopass |
| Backend | http://localhost:4000 | `/metrics` exposes Prometheus, `/healthz` for liveness |
| Prometheus | http://localhost:9090 | Targets at `/targets`, queries at `/graph` |
| Grafana | http://localhost:3000 | **Anonymous admin, no login** — opens directly to the User Journey dashboard |
| Elasticsearch | http://localhost:9200 | Logs in data stream `logs-app.ecom-dev` |
| Kibana | http://localhost:5601 | Optional, for ad-hoc log exploration |
| AI service | http://localhost:8000 | `POST /investigate` or CLI mode (below) |

### Drive traffic + trigger the canonical demo

```bash
# Wave 1 — normal traffic at the default 8% payment failure rate
./scripts/drive-traffic.sh 20

# Wave 2 — bump the failure rate to 50% and drive again to make the AI's job interesting
docker compose stop backend
PAYMENT_FAILURE_RATE=0.5 docker compose up -d backend
./scripts/drive-traffic.sh 20

# Wait a beat for Prometheus to scrape and Filebeat to ship the new events
sleep 15

# Ask the AI
curl -s -X POST localhost:8000/investigate \
  -H 'content-type: application/json' \
  -d '{"question":"anything wrong with payments in the last 15 minutes?"}' | jq .

# Or via the CLI (useful inside docker)
docker compose exec ai-service python -m ai_service.app "anything wrong with payments?"
```

The Grafana dashboard's *Payment failure rate* panel goes red within a minute of wave 2; the *Recent error logs* row fills with `ecom.error_code:payment_declined` events. See the [Sample AI Investigation](#sample-ai-investigation) section below for a captured transcript from this exact run.

---

## Contents

- [Observability Stack](#observability-stack) — Prometheus metrics + Elasticsearch logs, signals chosen for the user journey
- [AI Service](#ai-service) — architecture, 4 tools, the agent loop, system prompt
- [Dashboard Walkthrough](#dashboard-walkthrough) — what each panel tells on-call, with a snapshot from the demo
- [Sample AI Investigation](#sample-ai-investigation) — real transcript captured from this stack
- [AI-Gap Awareness](#ai-gap-awareness) — honest list of build-phase manual fixes + runtime LLM gaps
- [Tradeoffs](#tradeoffs) — cardinality, sampling, log volume, MCP-vs-native, model choice
- [Reproducing this Build](#reproducing-this-build) — the Blueprint trio at the repo root

> **Note on the AI driver:** the build phase used Claude Code (Opus 4.7) with the developer (Matan) directing each block. The **runtime AI observability service** runs on `anthropic/claude-sonnet-4.6` via the OpenRouter API key Helfy provided. Full breakdown in [`ai-log.md`](./ai-log.md).

## Observability Stack

### Metrics — Prometheus

The backend exposes `/metrics` in Prometheus exposition format via [`prom-client@15`](https://github.com/siimon/prom-client). A dedicated `Registry` holds:

| Namespace | Metrics | Source |
|---|---|---|
| `http_*` | `requests_total{method,route,status_code}`, `request_duration_seconds_bucket{...}` (histogram), `requests_in_flight{method,route}` | A single Express middleware in [`backend/src/metrics.ts`](./backend/src/metrics.ts) records every request post-routing. |
| `ecom_*` | `cart_items_added_total{product_category}`, `checkouts_total{outcome}`, `payments_total{outcome,provider}`, `payment_amount_cents_total{provider,outcome}`, `order_value_cents` (histogram) | Hand-placed at the exact route call sites where the business event happens (one increment per outcome branch). |
| `auth_*` | `login_attempts_total{outcome}` | Hand-placed in [`backend/src/routes/auth.ts`](./backend/src/routes/auth.ts). |
| `db_*` | `query_duration_seconds{query_name}` (histogram) | A `time(query_name, fn)` wrapper applied at exactly the three named queries in the catalog: `products_related`, `checkout_create_order`, `payment_record`. |
| `process_*`, `nodejs_*` | CPU, RSS, heap, **`eventloop_lag_seconds`** (the canary for "Node is overloaded") | `prom-client.collectDefaultMetrics()` — pure runtime hygiene. |

**Histogram buckets** for HTTP + DB latency: `[25, 50, 100, 200, 300, 500, 750, 1000, 1500, 2500, 5000] ms`. Tuned to the observed envelope: 25 ms minimum resolution for fast endpoints; high density in 100–500 ms (where the payment route's 120–450 ms uniform sleep lives); headroom to 5 s for degraded states.

**Bounded labels only.** Forbidden in labels (full list in [`guidelines.md` §2](./guidelines.md)): `user_id`, `order_id`, `payment_id`, `request_id`, `trace_id`, raw URL paths (templates only), error messages, SKUs.

Prometheus scrapes the backend every 15 seconds — config in [`prometheus/prometheus.yml`](./prometheus/prometheus.yml).

### Logs — pino → Filebeat → Elasticsearch

[`pino@10`](https://github.com/pinojs/pino) + [`pino-http`](https://github.com/pinojs/pino-http) write one JSON record per line to stdout. Filebeat tails the container via the docker JSON-file driver and ships to Elasticsearch data stream `logs-app.ecom-dev` with ECS-aligned field names.

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

Custom `ecom.*` namespace is the bridge between business logic and searchability — the AI agent finds *specific orders* by `ecom.order_id`, *failing payments* by `ecom.payment_status:failed`, *checkout-blocking errors* by `ecom.error_code:insufficient_stock`. Levels auto-promote 4xx → warn, 5xx → error via `pino-http`'s `customLogLevel`.

Filebeat config in [`filebeat/filebeat.yml`](./filebeat/filebeat.yml) uses the **`filestream` input** (the `container` input was deprecated in Filebeat 9.x — see [`ai-log.md`](./ai-log.md) for the bumpy migration).

## AI Service

### Architecture

The AI observability service is a Python 3.12 FastAPI app in [`ai-service/`](./ai-service/). It exposes one endpoint (`POST /investigate`) and one CLI mode (`python -m ai_service.app "<question>"`). The agent loop is in [`ai-service/ai_service/app.py:investigate`](./ai-service/ai_service/app.py) — ~50 lines, deliberately explicit so a reviewer can read it top to bottom:

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

The LLM is the [`openai`](https://github.com/openai/openai-python) Python SDK pointed at OpenRouter's OpenAI-compatible API. Native function calling, **not MCP** — MCP is the right transport for cross-process tool servers, but for four in-process Python functions co-located with the LLM client it adds JSON-RPC and zero behavioral value. Documented as the natural upgrade path in the module docstrings.

### The four tools

| Tool | Args | Returns | When to call |
|---|---|---|---|
| `get_metric_catalog` | — | The full `metric-catalog.md` (~14 KB) | First, if metric or field names are uncertain. The catalog documents what *normal* looks like for each signal. |
| `query_prometheus` | `promql`, `time_range: 5m\|15m\|1h\|24h` | Top-10 series sorted by max, each with last/min/max/mean/p50/p95 baked in; samples capped at 10 points | All numeric questions: rates, latencies, histograms, ratios. |
| `search_logs` | `query` (Lucene), `time_range`, `size≤50` | ECS-stripped log hits | To find *specific events* explaining a metric anomaly. Never for counting. |
| `get_recent_errors` | `route`, `time_range` | Aggregated `{error_code: count}` + sample lines for one route | Convenience: cheaper than two `search_logs` calls for the common "what's the error breakdown on /X?" question. |

Errors are returned as `{"error": str, "hint": str}` JSON, never raised — so the LLM can self-correct on the next turn instead of crashing the loop. Empty Prometheus results carry an explicit hint to consult the catalog (this directly fixed a metric-name hallucination during testing — see [`ai-log.md` Block 5](./ai-log.md)).

### System prompt

Encodes the SRE triage loop ([`guidelines.md` §6](./guidelines.md)) verbatim:

```
1. HYPOTHESIZE — state your initial suspicion in one sentence before any tool call.
2. CONFIRM — call the cheapest tool that could falsify the hypothesis.
3. NARROW — if confirmed, drill into the specific route, query_name, or error_code.
4. CHECK NEGATIVE EVIDENCE — confirm one thing that should be true if your
   hypothesis is right and false if not. Separates strong from weak output.
5. CONCLUDE — write a 3-paragraph incident note for a human on-call:
   (a) what's anomalous + time window, (b) supporting + negative evidence,
   (c) one concrete next action.
```

Full prompt: [`ai-service/ai_service/prompts.py`](./ai-service/ai_service/prompts.py).

## Dashboard Walkthrough

`http://localhost:3000` opens directly to the **User Journey** dashboard (anonymous admin enabled) in the `SRE` folder. Three rows, top to bottom:

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

**Why this layout** — top row tells you *if* something is wrong; middle tells you *where in the journey*; bottom tells you *what specifically*. A `$route` variable drives the top row so you can scope the RED panels to a single endpoint when investigating.

**Snapshot from the canonical demo run** ([`docs/dashboard-state.json`](./docs/dashboard-state.json), captured immediately before the sample AI investigation below):

```
Error rate 5xx           0.00 %        # internal stack healthy
Payment failure rate    37.9 %         # ⚠ way above 8% baseline — that's the canary
Payments succeeded/min   3.81
Checkouts succeeded/min  6.35

Latency p95 by route
  /api/payment/         477.7 ms       # at the catalog's uniform 120–450ms ceiling
  /api/cart/items        24.6 ms
  /api/checkout/         23.8 ms
  /api/products/         23.8 ms
  /api/products/:id/related   23.8 ms  # the deliberate slow query is fast on small data

Payments cumulative:  succeeded=7   failed=12
```

That panel snapshot is the input the AI agent reasons about. Read on.

## Sample AI Investigation

Captured live from this exact stack while it was running with `PAYMENT_FAILURE_RATE=0.5`. Full raw response (with the OpenRouter tool-call audit trail) is in [`docs/sample-investigation.json`](./docs/sample-investigation.json).

### The question

```bash
curl -X POST localhost:8000/investigate \
  -H 'content-type: application/json' \
  -d '{"question":"Anything wrong with payments in the last 15 minutes? Walk me through your reasoning and give me a triage-style writeup with a concrete next action."}'
```

### The trace (what the LLM actually did)

The agent ran **3 iterations / 7 tool calls** — *not* a single round-trip:

| # | Iter | Tool | Args | Latency |
|---|---|---|---|---|
| 1 | 0 | `query_prometheus` | failure-rate ratio | 6 ms |
| 2 | 0 | `query_prometheus` | payment p95 latency | 5 ms |
| 3 | 0 | `query_prometheus` | total payment request rate | 5 ms |
| 4 | 1 | `get_metric_catalog` | (confirm field names) | 1 ms |
| 5 | 1 | `get_recent_errors` | `/api/payment` | 15 ms |
| 6 | 2 | `query_prometheus` | payment p95 (re-check) | 10 ms |
| 7 | 2 | `query_prometheus` | DB `payment_record` p95 | 4 ms |

Notice the **parallel calls in iter 0** (three queries in one turn), the **catalog consult in iter 1** to ground the next query against documented field names, and the **negative-evidence check in iter 2** (DB latency) before concluding. That's the SRE triage loop in motion — pick from prior results, not a hardcoded sequence.

### The insight (verbatim from the live run)

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

That's the strong-output form from `metric-catalog.md` — symptom + supporting + **negative evidence** + suggested action. Notice it references catalog metric names by exact spelling and quotes the catalog's own diagnostic guidance. Single source of truth, end to end.

## AI-Gap Awareness

Build-phase and runtime-phase gaps, in the spirit of the assignment's "tell us honestly" criterion. Complete and continuously-updated list in [`ai-log.md`](./ai-log.md).

**Build phase** (Claude Code Opus 4.7 with Matan directing each block):

- *Express `req.baseUrl` clearing on error paths.* First prom-client middleware labeled failed-login 401s with `route="/login"` instead of `/api/auth/login` because Express clears baseUrl during error propagation. Required three iterations to fix; resolved by a tiny per-router `stampRouteTemplate` middleware that captures baseUrl before any error can happen.
- *Filebeat 9.x deprecated the `container` input.* The research-agent report I'd run during planning was out of date; on first Filebeat startup it told me to switch to `type: filestream` with the same parser chain. One config edit.
- *ES 9.x slim image strips both `curl` AND `wget`.* My initial healthcheck `wget --spider` failed silently, marking the ES container "unhealthy" even though `_cluster/health` returned green from inside. Fixed by using bash's built-in `/dev/tcp` probe in `CMD-SHELL`.
- *pino-http serializers vs. customProps.* First implementation indexed fields at `req.url.path` (nested under the serializer's key name) instead of `url.path`. Switched to `customProps` (which merges into the record root) and returned `undefined` from req/res serializers to suppress the nested versions.
- *url.path used router-local Express URL.* `req.url` is router-local; `req.originalUrl` is full. Same family as the baseUrl bug.
- *Environment leftovers, not LLM bugs:* osxkeychain credsStore from a prior Docker Desktop install broke image pulls; zoxide `--cmd cd` override broke `cd` inside non-interactive shells. Both fixed via system config; recorded because the assignment asked for honesty about manual fixes.

**Runtime phase** (the live AI service investigating the stack):

- *Metric-name hallucination, self-corrected via tool hint.* During a CLI test on login failures, Sonnet 4.6 guessed the metric was `ecom_auth_attempts_total` (correct name: `auth_login_attempts_total`). The first `query_prometheus` returned `series_count: 0` with no explanation, and the LLM had to infer the name was wrong. I added an explicit empty-result hint (`"Zero series matched. Most likely a misspelled metric name — call get_metric_catalog"`) to `query_prometheus`. After the hint, the LLM cleanly pivoted: searched logs → queried with wrong name → got hint → called catalog → re-queried with correct name → finished. This is exactly the "follow-up tool calls based on prior results, not a fixed sequence" the assignment grades.
- *No MCP layer.* Built natively (function calling). Documented as the upgrade path in `app.py` / `tools.py` docstrings — MCP is the right transport for cross-process / cross-agent tool servers, but for four in-process Python functions co-located with the LLM client it adds JSON-RPC and zero behavioral value. Worth mentioning to demonstrate the awareness, but not worth implementing in the 4-hour timebox.

## Tradeoffs

The PDF specifically asks for the explicit articulation. Three knobs and why we picked where we picked:

**Cardinality vs. observability.** Every label dimension multiplies time-series count. With our current labels — `route` (~10 values) × `method` (~4) × `status_code` (~10) — the HTTP histogram is ~400 series before the bucket fan-out (×11 buckets = ~4,400 active series). Adding a label like `user_id` would multiply that by however many users we have; we forbid it. The right way to investigate per-user issues is via logs (one row per event, not a series). The do-not-label list is explicit in [`guidelines.md` §2](./guidelines.md): `user_id`, `order_id`, `payment_id`, `request_id`, `trace_id`, raw URL paths, error messages, SKUs.

**Sampling vs. completeness.** Not sampled. Every HTTP request emits one log line and increments one counter. Acceptable at demo QPS and even at moderate production QPS for a single-instance service. At 100× the QPS we'd drop 2xx GET logs (rely on metrics for those) and head-sample traces. We deliberately do **not** have tracing in this build — the assignment lists Prometheus + Elasticsearch + Grafana but not OpenTelemetry, so adding tracing would burn time without scoring.

**Log volume vs. cost.** Single ES data stream `logs-app.ecom-dev`, no ILM, no rollover policy. Acceptable at single-node + demo retention. In production the right pattern is hot/warm/cold tiering with policy-driven rollover; for this 4-hour timebox the auto-created data stream is enough.

**MCP vs. native function calling.** Native. The 4 tools live in the same Python process as the LLM client; an MCP server would add a JSON-RPC transport that buys nothing here. The architectural awareness *is* worth recording (see AI-Gap Awareness above) — MCP becomes the right answer once these tools need to be reused across multiple agents (e.g., a Claude Desktop integration for on-call).

**LLM choice.** `anthropic/claude-sonnet-4.6` via OpenRouter. Best price/quality for 5–10-turn agentic tool-calling per OpenRouter's tool-calling collection rankings (May 2026). Cheaper options like Haiku 4.5 or Gemini Flash would have worked for the loop mechanics but produced weaker narrative quality — the "insight, not numbers" criterion the assignment grades most heavily. Used Opus 4.7 (via Claude Code) for the build phase only — never for runtime.

## Reproducing this Build

The three Blueprint files at the repo root — [`initial.md`](./initial.md), [`guidelines.md`](./guidelines.md), [`metric-catalog.md`](./metric-catalog.md) — were designed so that running `initial.md` against a fresh copy of the provided app (in `sre-store.zip`) produces an equivalent working stack. See [`ai-log.md`](./ai-log.md) for the prompts and models used.
