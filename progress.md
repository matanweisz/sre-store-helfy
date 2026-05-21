# Build Journey

A short summary of how this repo was built. The full per-block detail (including every manual fix) lives in [`ai-log.md`](./ai-log.md). For run instructions and the final architecture, see [`README.md`](./README.md).

The build was split into eight blocks of ~15–45 minutes each, run sequentially with a `docker compose up` verify gate at the end of every block. The commit log mirrors the block structure — one commit per block — so `git log --oneline` is the fastest way to navigate the history.

| Block | What it produced | Commit |
|---|---|---|
| 0 — Setup | Repo init, baseline app booted as-is to confirm a known-good starting point. | `9307667 chore: baseline imported` |
| 1 — Blueprint | `metric-catalog.md`, `guidelines.md`, `initial.md` — the three contract files. | `4387ba2 feat: blueprint` |
| 2 — Metrics | `prom-client` instrumentation, Prometheus service, business counters at the route call sites, DB-query timing. | `9c1c073 feat(backend): prom-client + prometheus` |
| 3 — Logs | `pino` + `pino-http` writing ECS JSON, Filebeat shipping to Elasticsearch, Kibana for ad-hoc exploration. | `0f76204 feat: pino + filebeat -> elasticsearch` |
| 4 — Dashboard | Grafana with provisioned datasources (pinned UIDs) and the User Journey dashboard. | `843b265 feat: grafana User Journey dashboard` |
| 5 — AI service | FastAPI + the four tools + the agent loop + the SRE triage system prompt. | `bfbc8a7 feat: AI observability service` |
| 6 — Demo capture | Traffic generator, canonical investigation transcript, README filled in. | `0007be5 docs: sample investigation + README polish` |
| 7 — Polish | This block. Documentation tightened across the repo before submission. | _(pending)_ |

The `ai-log.md` records twelve named manual fixes across the build — most of them on stack-specific gotchas (Filebeat 9.x deprecations, Express `req.baseUrl` clearing on the error path, Elasticsearch 9.x's stripped image, pino-http's nested serializer shape). All caught at the verify gate of the block where they happened.
