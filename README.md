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

> Requires Docker + docker compose. Tested on Rancher Desktop (dockerd/moby engine), Apple Silicon. ~6 GB RAM headroom needed.

```bash
cp .env.example .env
# Edit .env, paste OPENROUTER_API_KEY=...
docker compose up --build
```

| Service | URL | Notes |
|---|---|---|
| Frontend | http://localhost:5173 | demo@shop.local / demopass |
| Backend | http://localhost:4000 | `/metrics` exposes Prometheus |
| Prometheus | http://localhost:9090 | Targets at /targets |
| Grafana | http://localhost:3000 | Anonymous admin, no login |
| Elasticsearch | http://localhost:9200 | Logs in `logs-app.ecom-dev` |
| Kibana | http://localhost:5601 | Optional, for log exploration |
| AI service | http://localhost:8000 | `POST /investigate` |

To trigger something interesting for the AI to find:

```bash
# Bump payment failure rate (default 0.08) to 0.5 and restart backend
PAYMENT_FAILURE_RATE=0.5 docker compose up -d backend

# Drive traffic
./scripts/drive-traffic.sh

# Ask the AI
curl -X POST localhost:8000/investigate \
  -H 'content-type: application/json' \
  -d '{"question":"anything wrong with payments in the last 5 minutes?"}' | jq .
```

---

## Sections (filled in as we build)

- [Observability Stack](#observability-stack) — metrics, logs, shippers
- [AI Service](#ai-service) — architecture, tools, sample invocation
- [Dashboard Walkthrough](#dashboard-walkthrough) — what each panel tells on-call
- [Sample AI Investigation](#sample-ai-investigation) — real transcript from a real run
- [AI-Gap Awareness](#ai-gap-awareness) — honest list of what the AI couldn't do
- [Tradeoffs](#tradeoffs) — cardinality, sampling, log volume
- [Reproducing this Build](#reproducing-this-build) — pointer at the Blueprint

> **Note on the AI driver:** the build phase used Claude Code (Opus 4.7) with the developer (Matan) directing each block. The **runtime AI observability service** runs on `anthropic/claude-sonnet-4.6` via the OpenRouter API key Helfy provided. Detail in [`ai-log.md`](./ai-log.md).

## Observability Stack

_To be filled in after Block 2 (metrics) and Block 3 (logs)._

## AI Service

_To be filled in after Block 5._

## Dashboard Walkthrough

_To be filled in after Block 4._

## Sample AI Investigation

_To be filled in after Block 6._

## AI-Gap Awareness

_To be filled in continuously in [`ai-log.md`](./ai-log.md) and summarized here at the end._

## Tradeoffs

_To be filled in after Block 7._

## Reproducing this Build

The three Blueprint files at the repo root — [`initial.md`](./initial.md), [`guidelines.md`](./guidelines.md), [`metric-catalog.md`](./metric-catalog.md) — were designed so that running `initial.md` against a fresh copy of the provided app (in `sre-store.zip`) produces an equivalent working stack. See [`ai-log.md`](./ai-log.md) for the prompts and models used.
