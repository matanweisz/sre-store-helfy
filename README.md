# AI-Driven Observability for an eCommerce App

**Junior AI SRE Home Assignment — Helfy** · Matan Weisz · May 2026

A Node.js + Express + React eCommerce app, instrumented from scratch, with an observability stack (Prometheus, Elasticsearch + Filebeat, Grafana) and a Python AI service that investigates the live system over multi-turn LLM tool calls.

The goal here isn't a polished app. It's an observability foundation an AI agent can actually reason about: signals named meaningfully, a catalog the LLM can read at runtime, and a triage procedure the agent follows step by step.

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

You need Docker (tested on Rancher Desktop with the dockerd engine on Apple Silicon) and ~6 GB of RAM free for the 9 containers. The runtime AI agent needs an OpenRouter API key.

```bash
cp .env.example .env
# Paste your OpenRouter API key into .env:
#   OPENROUTER_API_KEY=sk-or-v1-...

docker compose up --build -d
```

`docker compose ps` should show all 9 services healthy after about a minute. Here's where each one lives:

| Service | URL | Notes |
|---|---|---|
| Frontend | http://localhost:5173 | `demo@shop.local` / `demopass` |
| Backend | http://localhost:4000 | `/metrics`, `/healthz` |
| Prometheus | http://localhost:9090 | targets at `/targets` |
| Grafana | http://localhost:3000 | anonymous admin — opens straight on the dashboard |
| Elasticsearch | http://localhost:9200 | data stream `logs-app.ecom-dev` |
| Kibana | http://localhost:5601 | optional, for ad-hoc log exploration |
| AI service | http://localhost:8000 | `POST /investigate`, or CLI mode (below) |

### Try it in 4 steps

Now that the stack is up, here's the path that shows what this project actually does: instrument an app you own, watch the dashboards react to real traffic, then ask an AI agent natural-language questions about what's happening inside.

#### Step 1 — Open the dashboard

Visit **http://localhost:3000**. It opens straight on the *User Journey* dashboard (no login). The panels are empty for now because nothing's happening yet. Leave this tab open — we'll come back to it.

#### Step 2 — Drive some traffic

```bash
./scripts/drive-traffic.sh 20
```

The script logs in as the demo user and runs 20 end-to-end user journeys (browse → cart → checkout → pay). It takes about ten seconds. When it finishes, refresh the Grafana tab: every panel now has data. Request rates climb, latency p95 lines fill in, the funnel stats (cart → checkout → payment) show throughput per minute.

This is the "everything is healthy" baseline.

#### Step 3 — Break something on purpose

The payment route has a knob: `PAYMENT_FAILURE_RATE`. The default is `0.08` (8% of payments fail, which is normal). Bump it to half and drive another wave:

```bash
docker compose stop backend
PAYMENT_FAILURE_RATE=0.5 docker compose up -d backend
./scripts/drive-traffic.sh 20
```

Watch Grafana. Within about a minute:

- The **Payment failure rate** stat turns red (it's well above the 8% baseline now)
- The **Recent error logs** panel at the bottom fills with `ecom.error_code: payment_declined` events
- The funnel shows checkouts continuing but payments dropping off

You've just created a payment incident. Now ask the AI to investigate it.

#### Step 4 — Ask the AI what's wrong

You have two ways to ask. The agent takes 15–45 seconds either way (it makes 5–10 tool calls against Prometheus and Elasticsearch), so it's worth watching its progress in a second terminal:

```bash
# In a second terminal — see what the agent is doing in real time
docker compose logs -f ai-service
```

```bash
# Then ask, in your first terminal:
curl -X POST localhost:8000/investigate \
  -H 'content-type: application/json' \
  -d '{"question":"anything wrong with payments in the last 15 minutes?"}' | jq .

# Or use the CLI (same answer, prints progress to your terminal as it goes):
docker compose exec ai-service python -m ai_service.app "anything wrong with payments?"
```

In the logs terminal you'll see lines arriving as the agent works:

```
[ai] ▶ investigating: anything wrong with payments in the last 15 minutes?
[ai] iter 0  → query_prometheus(promql=sum(rate(ecom_payments_total{outcome=..., time_range=15m)  (7 ms)
[ai] iter 0  → query_prometheus(promql=histogram_quantile(0.95, sum by (le)(..., time_range=15m)  (4 ms)
[ai] iter 1  → get_metric_catalog()  (1 ms)
[ai] iter 1  → get_recent_errors(route=/api/payment, time_range=15m)  (13 ms)
[ai] iter 2  → query_prometheus(promql=histogram_quantile(0.95, sum by (query_name, le)(..., time_range=15m)  (8 ms)
[ai] ✔ done — 4 iters, 5 tool calls, 41725 ms
```

The agent picks each next move based on what the previous one told it — no fixed script. When it's done, you get back a written incident note in plain English: what's wrong, why it's wrong, what's *not* wrong (the negative evidence), and a concrete next action to take. The exact transcript from a live run is captured in the [Sample AI Investigation](#sample-ai-investigation) section below.

That's the loop the project enables: instrument the user journey → see anomalies on the dashboard → ask the AI for the *reasoning*, not the numbers.

---

## How this was built — Claude Code as a force multiplier

The assignment says *"show us how you lead the machine to see a system, not just how you build one."* This section is that.

### The two LLMs and what each one does

| Where | Model | What for |
|---|---|---|
| **Build time** (this session) | `claude-opus-4-7[1m]` via **Claude Code** (CLI) | Writing code and configs across multiple files, planning, reviewing, verifying each step against a running stack |
| **Runtime** (the deliverable) | `anthropic/claude-sonnet-4.6` via **OpenRouter** | The AI observability service — investigates the live Prometheus + Elasticsearch with multi-turn tool calls |

I used Claude Code as a pair-programmer with a tight feedback loop, not as a one-shot code generator. The OpenRouter key Helfy provided is consumed only at runtime, by the deliverable.

### How I led Claude Code

I worked in **eight discrete blocks** (one commit per block — see `git log --oneline`). Each block looked the same:

1. **Plan the block before writing any code.** I used Claude Code's plan mode to describe what I wanted (e.g. "instrument the backend with prom-client per `metric-catalog.md`, add a Prometheus service, verify with these `curl`s"). The model produced a step-by-step plan that I reviewed and approved before any file was touched.
2. **Execute against an explicit verify gate.** Every block had a *concrete* end condition — usually a `curl` against the running stack that had to return the expected shape. I didn't move to the next block until that gate passed.
3. **Capture every manual fix honestly.** When Claude Code produced something that didn't work — like the Express `req.baseUrl` middleware that mislabeled error-path requests — I logged the diagnosis and the fix into [`ai-log.md`](./ai-log.md) before moving on. Twelve such fixes are recorded.

### What Claude Code's built-in tools did, concretely

- **Read / Edit / Write** for all file operations. No external editors.
- **Bash** for running the stack, driving `curl` verifications, and checking `git status` between commits.
- **The Explore and general-purpose sub-agents** for parallel research during planning — I sent four independent research queries in one message (Prometheus best practices, Filebeat/ECS, LLM tool-calling patterns, Grafana provisioning) and they came back concurrently. That's how I built the four-research-report foundation in under 15 minutes.
- **AskUserQuestion** at decision points where I needed your input (language choice for the AI service, primary LLM, GitHub vs. email submission).
- **Plan mode (`ExitPlanMode`)** at the top of each major block, so we agreed on what was being done before any change landed.
- **Todo tracking (`TaskCreate`/`TaskUpdate`)** to keep the eight-block structure visible as we worked.

A **project-scoped memory file** ([`CLAUDE.md`](./CLAUDE.md) at the repo root) captures the architectural decisions and load-bearing comments that future Claude Code sessions need to know about — for example, *don't "fix" the bash `/dev/tcp` healthcheck because ES 9.x strips both `curl` and `wget`*. It's how I made the project re-resumable.

### MCP servers and plugins

**None at runtime.** The AI observability service uses **native OpenAI-compatible function calling** (not MCP) because the four tools live in the same Python process as the LLM client — MCP would add a JSON-RPC layer with no behavioral change. MCP becomes the right answer once these tools need to be reused across multiple agents (a Claude Desktop on-call integration is the obvious case); documented as the natural upgrade path in `ai-service/ai_service/{app,tools}.py`.

At build time, Claude Code's own built-in tools were enough. No third-party MCP servers were used.

### Where Claude Code fell short

The honest list is in [`ai-log.md`](./ai-log.md). The pattern of mistakes Opus 4.7 made under my direction:

- **Subtle framework timing.** The Express `req.baseUrl`-on-error-path bug and the `req.url` vs. `req.originalUrl` bug are the same shape: confidently-written code that looks right but doesn't survive the error path. The first one took three iterations to diagnose because each "obvious" hook had its own timing failure.
- **Out-of-date library knowledge.** The planning-phase research correctly named Filebeat 9.4 and ES 9.4 but missed that 9.x deprecated `container` input and stripped `wget` from the base image. The LLM didn't know what it didn't know; Filebeat and Docker's healthcheck told me directly on first start.
- **Naive API shapes.** pino-http puts custom-serializer output under the key name (so my fields landed at `req.url.path` instead of `url.path`). pino's `bindings()` formatter is incompatible with `base: undefined` if you read `bindings.pid` inside it. Both surfaced only on first boot.

All caught at the verify gate of the block where they happened. None leaked across block boundaries.

---

> Tooling at a glance: **Claude Code (Opus 4.7)** at build time, directed by me block by block; **`anthropic/claude-sonnet-4.6`** via OpenRouter at runtime for the live observability agent. Full breakdown including all manual fixes in [`ai-log.md`](./ai-log.md).

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

Snapshot from a live demo run, captured the moment before the AI investigation below (full panel data in [`docs/dashboard-state.json`](./docs/dashboard-state.json)):

```
Error rate 5xx           0.00 %        # internal stack healthy
Payment failure rate    40.0 %         # baseline is 8% — that's the canary
Payments succeeded/min   3.11
Checkouts succeeded/min  5.18

Latency p95 by route
  /api/payment/         468.7 ms       # right at the 450 ms ceiling
  /api/cart/items        23.8 ms
  /api/checkout/         23.8 ms
  /api/products/         23.8 ms
  /api/products/:id/related   23.8 ms  # slow query is fast on small data
  /api/auth/login         90.0 ms      # bcrypt dominates this one
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

**What the agent did** — 4 iterations, 5 tool calls, ~42 seconds total, no fixed sequence:

| # | Iter | Tool | What for | Latency |
|---|---|---|---|---|
| 1 | 0 | `get_metric_catalog` | ground field names | 1 ms |
| 2 | 0 | `query_prometheus` | failure-rate ratio | 7 ms |
| 3 | 1 | `query_prometheus` | p95 latency by route | 11 ms |
| 4 | 1 | `get_recent_errors` | `/api/payment` breakdown | 13 ms |
| 5 | 2 | `query_prometheus` | DB p95 by query_name | 8 ms |

The agent picks its next move from what it just learned: catalog first to confirm names, then the headline ratio, then the per-route latency picture, then the qualitative log breakdown, finally DB latency as negative evidence to rule out an internal cause. Iteration 3 is the conclusion turn — text only, no tools.

**What it said:**

> **🚨 Incident Note — Payment Failure Rate Critically Elevated**
> *Window analyzed: last 15 minutes*
>
> **What is anomalous.** The payment failure rate is **~40–42%** — more than **5× the 8% baseline**. Roughly 4 in every 10 payment attempts are being declined by the mock-stripe provider right now. This is a direct revenue impact: a large fraction of users who reach the payment step are being turned away.
>
> **Supporting evidence.** `ecom_payments_total` failure ratio: last value **0.40**, max **0.42** over the window. Baseline is 0.08. Not noise — sustained across the entire 15-minute window. Logs show a steady stream of `payment_declined` errors on `POST /api/payment`, returning HTTP 402. All 10 sampled warn-level events carry `ecom.error_code: payment_declined`; no other error codes are present. Clean declines from the provider, not crashes or timeouts.
>
> **Negative evidence (what rules out internal causes).** `/api/payment` p95 latency is **~469 ms** — essentially at the normal ~430 ms baseline. The provider is responding promptly; it's just saying "no" far more often. `db_query_duration_seconds` p95 for `payment_record` is **~24 ms**, well within the normal 10–25 ms range. Our database is healthy. `checkout_create_order` DB p95 is also flat at ~24 ms. All other routes show normal latency. The problem is isolated entirely to the payment provider decision layer.
>
> **Concrete next action.** Check the `PAYMENT_FAILURE_RATE` environment variable on the backend service immediately. It should be `0.08`; if someone bumped it (e.g., for a load test and forgot to revert), restart the service with the corrected value. If the env var is correct, treat this as a mock-stripe provider incident.

The agent uses catalog metric names exactly as written (`db_query_duration_seconds{query_name="payment_record"}`, `ecom.error_code: payment_declined`) and quotes the catalog's own diagnostic guidance back. The catalog is functioning as a runtime contract, not just documentation.

## AI-Gap Awareness

The full record is in [`ai-log.md`](./ai-log.md). The honest summary:

**During the build** (Claude Code Opus 4.7, with me directing):

- *Express clears `req.baseUrl` on the error path.* My first metrics middleware labeled 401 failed-logins as `route="/login"` instead of `/api/auth/login`. Took three iterations of hook placement to find a working fix — a per-router `stampRouteTemplate` middleware that captures baseUrl while it's still correct.
- *Filebeat 9.x deprecated the `container` input.* The research I'd done in the planning phase was out of date. Filebeat told me on first start; one config edit to `type: filestream`.
- *ES 9.x strips both `curl` AND `wget`.* My initial `wget --spider` healthcheck silently failed and ES looked unhealthy from outside, even though it was green inside. Switched to a bash `/dev/tcp` probe.
- *pino-http serializers put fields under the key name.* My first run indexed `req.url.path` instead of `url.path`. Fixed by using `customProps` (which merges into the record root) and suppressing the serializers.
- *`req.url` is router-local; `req.originalUrl` is the full path.* Same family as the baseUrl bug. The logger had to switch.
- *Environment leftovers, not LLM mistakes:* a stale `osxkeychain` credsStore from Docker Desktop, and zoxide overriding `cd` in the shell snapshot. Both fixed via system config. Listed here for honesty.

**At runtime** (the AI agent investigating live):

- *Sonnet 4.6 hallucinated a metric name once.* It guessed `ecom_auth_attempts_total` (correct is `auth_login_attempts_total`). My `query_prometheus` originally returned `series_count: 0` with no explanation, so the model had to infer the name was wrong. I added an explicit "zero series — likely misspelled, call get_metric_catalog" hint. Next time around it pivoted correctly: log search → wrong-name query → hint → catalog → corrected query → conclusion. That's the multi-turn loop earning its keep.
- *No MCP layer.* Built natively. MCP is the right answer when these tools need to be reused across processes or agents (a Claude Desktop integration for on-call would be the obvious case). For four functions co-located with the LLM client, native function calling is simpler and equivalent.

## Tradeoffs

**Cardinality.** Every label dimension multiplies series count. With current labels — `route` (~10 values) × `method` (~4) × `status_code` (~10) — the HTTP histogram is ~400 series before the bucket fan-out (×11 buckets ≈ 4,400 active series). Adding a `user_id` label would multiply that by the user count and kill Prometheus. We forbid it. Per-user investigation belongs in logs, not metrics. The do-not-label list is in [`guidelines.md`](./guidelines.md) §2.

**Sampling.** None. Every HTTP request emits one log line and increments one counter. Fine at demo QPS, fine at moderate production QPS for a single instance. At 100× the QPS I'd drop 2xx GET logs and rely on metrics for them, then head-sample traces if we had any. We don't — tracing isn't in scope.

**Log volume.** Single ES data stream, no ILM, no rollover. Acceptable for a single-node demo. In production this becomes hot/warm/cold tiering with a rollover policy; here it would be ceremony without benefit.

**MCP vs. native function calling.** Native. The four tools live in the same process as the LLM client, so MCP's JSON-RPC layer would add complexity without changing behavior. MCP becomes the right answer when the same tools need to be reused across multiple agents.

**LLM choice.** `anthropic/claude-sonnet-4.6` via OpenRouter. The best price/quality on OpenRouter's tool-calling collection rankings as of May 2026 for 5–10-turn agent loops. Haiku 4.5 and Gemini Flash work for the loop mechanics but produce flatter narratives — and "insight, not numbers" is what matters here. Opus 4.7 (via Claude Code) was only used at build time, never at runtime.

## Reproducing this Build

`initial.md` is a self-contained bootstrap prompt. Paste it into any agentic coding LLM with filesystem + shell access, against a fresh checkout of the starter app (`sre-store.zip`), and it produces an equivalent working stack. It references [`guidelines.md`](./guidelines.md) and [`metric-catalog.md`](./metric-catalog.md), and the prompts and models I used during the build are recorded in [`ai-log.md`](./ai-log.md).
