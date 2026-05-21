# Progress Tracker

Live checklist of every step in the 4-hour build. Tick as we go.

## Block 0 — Setup + repo init (15 min)
- [x] `git init` on the working dir
- [x] `.gitignore` excluding original PDF/email/zip + sre-store/
- [x] Copy `backend/`, `frontend/`, `docker-compose.yml` up to root
- [x] Save original README as `README.original.md`
- [x] Seed `README.md` (with skeleton sections)
- [x] Seed `progress.md` (this file)
- [x] Seed `ai-log.md`
- [x] Seed `.env.example`
- [x] First commit (`9307667`)
- [x] **Verify PASSED**: full user journey via curl — login → cart → checkout → payment → frontend HTTP 200. See Block 0 details in `ai-log.md`.

## Block 1 — Blueprint authoring (45 min)
- [x] `metric-catalog.md` — every metric + log field, opinionated (237 lines)
- [x] `guidelines.md` — conventions + reusable procedures + triage loop (232 lines)
- [x] `initial.md` — single bootstrap prompt (339 lines)
- [x] **Verify**: cross-reference check passed — every metric in `initial.md` documented in `metric-catalog.md`; all internal file refs resolve
- [ ] Commit: `feat: blueprint (initial.md + guidelines + catalog)`

## Block 2 — Prometheus + backend instrumentation (25 min)
- [x] `npm install prom-client@^15`
- [x] `backend/src/metrics.ts` — registry + metric definitions + middleware + `time()` DB wrapper + `stampRouteTemplate` per-router middleware
- [x] Wire middleware into `index.ts`, expose `/metrics` (before middleware so it's not self-labeled)
- [x] Business counters in `payment.ts`, `checkout.ts`, `cart.ts`, `auth.ts`
- [x] DB query timing for `checkout_create_order`, `products_related`, `payment_record`
- [x] `prometheus/prometheus.yml` + `prometheus` service in compose (pinned `prom/prometheus:v3.6.0`)
- [x] **Verify PASSED**: every catalog metric live; Prometheus shows `shop-backend up`; PromQL `sum by (route)(rate(http_requests_total[1m]))` returns per-route rates. Route labels correctly capture Express templates for 200/201/401/404 paths (the 401-on-baseUrl bug was the one real fix this block).
- [ ] Commit: `feat(backend): prom-client instrumentation + prometheus service`

## Block 3 — Elasticsearch + Filebeat + structured logs (20 min)
- [ ] `npm install pino pino-http`
- [ ] `backend/src/logger.ts` — ECS-aligned pino
- [ ] Wire `pinoHttp` middleware; replace `console.*` in error handler
- [ ] Event logs in payment.ts, checkout.ts, auth.ts
- [ ] `elasticsearch` + `filebeat` services in compose
- [ ] `filebeat/filebeat.yml` with docker autodiscover + ndjson parser
- [ ] **Verify**: `curl localhost:9200/logs-*/_search?size=1` shows ECS fields
- [ ] Commit: `feat: structured logging via pino + filebeat -> elasticsearch`

## Block 4 — Grafana + dashboard (25 min)
- [ ] `grafana/provisioning/datasources/datasources.yaml` (pinned UIDs)
- [ ] `grafana/provisioning/dashboards/dashboards.yaml`
- [ ] `grafana/dashboards/user-journey.json` (6 panels)
- [ ] `grafana` service in compose with anonymous admin envs
- [ ] **Verify**: localhost:3000 opens, dashboard renders with live data
- [ ] Commit: `feat: grafana with provisioned User Journey dashboard`

## Block 5 — AI observability service (45 min)
- [ ] `ai-service/` skeleton (Dockerfile, pyproject.toml)
- [ ] `tools.py` — 4 tools + JSON schemas + registry
- [ ] `prompts.py` — SRE triage system prompt
- [ ] `app.py` — FastAPI + agent loop + CLI mode
- [ ] `ai-service` in compose with env vars
- [ ] **Verify**: `POST /investigate` returns narrative answer using ≥2 tool calls
- [ ] Commit: `feat: AI observability service with multi-turn tool calling`

## Block 6 — E2E drill + sample investigation (30 min)
- [ ] `scripts/drive-traffic.sh`
- [ ] Run clean, drive normal traffic, bump failure rate, drive again
- [ ] Capture investigation into `docs/sample-investigation.json`
- [ ] Embed in README "Sample AI Investigation" section
- [ ] Commit: `docs: sample AI investigation transcript`

## Block 7 — README polish + push (20 min)
- [ ] Fill in all README placeholder sections
- [ ] Capture dashboard screenshot
- [ ] Fresh-clone verification in /tmp/test-clone
- [ ] Push to GitHub, flip to public
- [ ] Final commit: `docs: README polished + ready for review`

## Block 8 — Submit (5 min)
- [ ] Reply email sent to hadar.d@helfy.co

---

## Running notes

- **Started**: TBD when Block 0 verify gate passes
- **Environment**: Rancher Desktop on macOS (Apple Silicon), dockerd engine, VM 5.7 GB RAM (target was 8 GB; full Rancher restart may be needed if anything stalls)
