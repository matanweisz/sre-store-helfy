# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A personal learning project in AI-driven observability. A deliberately-uninstrumented Node.js + Express + React eCommerce app (`backend/`, `frontend/`) is wrapped in an observability stack (Prometheus, Elasticsearch+Filebeat, Grafana) plus a standalone Python AI service (`ai-service/`) that investigates the running system via multi-turn LLM tool calls against OpenRouter (`anthropic/claude-sonnet-4.6`). I built it to learn how to instrument a system an AI agent can actually reason about.

The build was driven by three **blueprint** docs at the repo root that are the contract every other artifact must agree with — read these before touching anything substantive:

- `metric-catalog.md` — every metric and log field, with normal ranges and what changes imply. **Single source of truth.** The catalog file is mounted into the AI service container at `/app/metric-catalog.md` and loaded by the `get_metric_catalog` tool.
- `guidelines.md` — log format, metric naming, cardinality rules, the **triage loop** (§6) the runtime LLM follows. The triage loop is the most important part of this file.
- `initial.md` — the bootstrap prompt that produced the stack; five phases with hard verify gates.

`ai-log.md` is the honest record of LLM usage and manual fixes. `sre-store/` is the pristine pre-instrumentation reference — gitignored, do not edit.

**Quick orientation:** `git log --oneline` gives one commit per block (chore: baseline → blueprint → prom-client → pino+filebeat → grafana → AI service → docs+investigation). Each commit message documents what was added, why, and any manual fixes during that block.

## Run / dev commands

```bash
# Full stack (requires .env with OPENROUTER_API_KEY)
docker compose up --build

# Drive traffic into the stack (used to make AI investigations interesting)
PAYMENT_FAILURE_RATE=0.5 docker compose up -d backend   # bump failures, then drive traffic
./scripts/drive-traffic.sh 20                           # 20 user-journey iterations

# Backend (Node 20, TS via tsx — no build step in dev)
cd backend && npm install
npm run dev           # tsx watch src/index.ts
npm run typecheck     # tsc --noEmit
npm run seed          # populate MySQL with seed products

# Frontend (Vite + React)
cd frontend && npm install
npm run dev           # vite on :5173
npm run typecheck

# AI service (Python 3.12, FastAPI + openai SDK pointed at OpenRouter)
# Run via docker compose; pyproject.toml is the install manifest.
cd ai-service && pip install -e '.[dev]'   # install with test deps
pytest                                      # unit tests (httpx mocked via respx)
```

Service ports: backend `:4000` (+`/metrics`), frontend `:5173`, prometheus `:9090`, elasticsearch `:9200`, kibana `:5601`, grafana `:3000` (anonymous admin, no login), ai-service `:8000` (`POST /investigate`). Demo creds: `demo@shop.local / demopass`.

## Architecture — the parts that span multiple files

**Catalog ↔ code ↔ AI runtime must agree.** Three places reference the same metric/log-field names:
1. Definitions in `backend/src/metrics.ts` (and emit sites in `backend/src/routes/*.ts` and `backend/src/logger.ts`).
2. Documentation in `metric-catalog.md`.
3. Loaded into the LLM's context at runtime via `ai_service/tools.py::get_metric_catalog`.

If you add a metric you MUST update the catalog in the same change — the AI hallucinates without it. See `guidelines.md` §5a for the full procedure.

**Backend HTTP labeling has a non-obvious wrinkle.** `backend/src/metrics.ts` exports a `stampRouteTemplate` middleware that every router in `routes/*.ts` installs via `router.use(stampRouteTemplate)` at the top. This exists because on the error path Express clears `req.baseUrl` before the global error handler runs, so reading `req.baseUrl + req.route.path` at `res.on('finish')` returns only the local path (`/login`) instead of the full template (`/api/auth/login`). The stamp captures `baseUrl` while it's still correct. Don't remove the stamp pattern — the full reasoning is in `metrics.ts` ~lines 121–157 and `ai-log.md` Block 2.

**Logging targets ECS field paths, not pino defaults.** `backend/src/logger.ts` reshapes pino-http output so logs land at `url.path`, `http.response.status_code`, `event.duration`, `trace.id` (root-level), not nested under `req`/`res`. `@timestamp` is forced via a custom `timestamp` fn. Filebeat does no field remapping — what the backend emits is what lands in Elasticsearch.

**Filebeat → Elasticsearch is intentionally minimal.** ILM and templates are disabled (`setup.ilm.enabled: false`, `setup.template.enabled: false`) to avoid the data-stream auto-creation dance; index is the literal `logs-app.ecom-dev`. The AI service queries the wildcard `logs-app.ecom-dev*` (the `es_log_index` setting in `ai_service/config.py`) so it works whether or not a data stream gets created.

**AI-service config is centralized and overridable.** All tunables live in `ai_service/config.py` as a `pydantic-settings` `Settings` object (`get_settings()`, cached) — backend URLs, the ES index, agent caps, and a **log field map** (`es_error_code_field`, `es_url_path_field`, `es_level_field`, `log_domain_namespace`, ...). Defaults reproduce the eCommerce stack exactly, but every value is env-overridable, which is what lets the agent point at a different system without code edits. Don't reintroduce scattered `os.environ` reads in `app.py`/`tools.py` — add a field to `Settings` instead. Unit tests are in `ai-service/tests/` (`pytest`, httpx mocked via `respx`); the `conftest.py` autouse fixture clears the settings + catalog caches between tests.

**Don't "fix" these — they're load-bearing:**
- `docker-compose.yml` ES healthcheck uses bash `/dev/tcp/localhost/9200`. The ES 9.x image strips both `curl` AND `wget`; this is the canonical no-binary probe.
- `filebeat/filebeat.yml` uses `type: filestream` with a `container` parser. `type: container` was deprecated in Filebeat 9.x and will refuse to start.
- `docker-compose.yml` Filebeat `command: ["filebeat", "-e", "--strict.perms=false"]`. The image ENTRYPOINT is `filebeat` (expects a subcommand) — omitting the subcommand prints help and exits 1.
- `ai_service/app.py` configures `logging.basicConfig(stream=sys.stderr, ...)`. CLI mode (`python -m ai_service.app "<question>"`) must keep stdout pure JSON so callers can `json.loads()` it.
- `ai_service/tools.py::query_prometheus` returns an explicit `hint` when `series_count == 0`. This is what makes the LLM pivot from a hallucinated metric name to calling `get_metric_catalog` instead of guessing again.

**Grafana datasource UIDs are pinned** in `grafana/provisioning/datasources/datasources.yaml` to `prometheus` and `elasticsearch`. The dashboard JSON (`grafana/dashboards/user-journey.json`) references these UIDs directly — do not let Grafana auto-generate new UIDs or panels will silently break. The dashboard layout (RED → funnel → logs) is documented in `guidelines.md` §3.

**AI agent loop** (`ai_service/app.py:investigate`): OpenAI SDK pointed at OpenRouter, model `anthropic/claude-sonnet-4.6`, native function-calling, max 10 iterations, tool results truncated to ~8000 chars (catalog gets 16000). The four tools live in `ai_service/tools.py`: `get_metric_catalog`, `query_prometheus`, `search_logs`, `get_recent_errors`. The system prompt in `prompts.py` is copied verbatim from `initial.md` Phase 4 — it encodes the triage loop. **Never edit the prompt without updating `guidelines.md` §6** (they must stay aligned).

## Project-specific rules

- **Cardinality is sacred.** Forbidden labels (per `guidelines.md` §2): `user_id`, `order_id`, `payment_id`, `cart_id`, `request_id`, `trace_id`, raw URL paths, free-text error messages, SKUs, emails, IPs. Use logs for per-entity drill-down.
- **Don't touch `frontend/`.** Polishing the app isn't the point of this project.
- **`sre-store/` is read-only reference.** It's the pristine pre-instrumentation copy and gitignored — don't edit, don't commit changes inside it.
- **Pin all image tags.** No `:latest`. Current pins: prometheus `v3.6.0`, ES/Kibana/Filebeat `9.4.1`, Grafana `11.4.0`, MySQL `8.4`.
- **Don't invent metric or log-field names.** If a name isn't in `metric-catalog.md`, add it to the catalog in the same commit (description + why + normal + change implies).
- **The Blueprint files are first-class artifacts.** `metric-catalog.md`, `guidelines.md`, `initial.md` are not just docs — re-running `initial.md` against a fresh checkout of the starter app should reproduce the stack. Keep them in sync with the code.

## Environment / Rancher Desktop gotchas

- `~/.docker/config.json` with `"credsStore": "osxkeychain"` (leftover from Docker Desktop) breaks every `docker compose pull` on Rancher. Remove the line; Rancher needs no credential helper for public Docker Hub images.
- Rancher Settings → Container Engine = **dockerd (moby)**. Compose won't see the daemon under containerd.
- VM defaults to 4 GB RAM / 2 CPUs — too small for the 9-container stack. Bump to 8 GB / 4 CPUs in Preferences → Virtual Machine, then **Quit & Restart Rancher** (not "reset Kubernetes") for changes to take effect.
- Shell snapshot quirk: if `cd backend` fails with `zoxide: no match found` inside `Bash` tool calls, the harness's captured zsh snapshot has zoxide overriding `cd`. Fix `~/.zshrc` (`zoxide init zsh`, not `zoxide init --cmd cd zsh`) AND patch the snapshot at `~/.claude/shell-snapshots/snapshot-zsh-*.sh` to remove the `cd () { __zoxide_z "$@"; }` function.
