# AI Log

An honest record of which LLMs were used, for what, and where they fell short. I kept it because the interesting part of building with an agent is being honest about where you stop prompting and fix things by hand.

## Models used

| Phase | Model | Provider | Why |
|---|---|---|---|
| Build (this repo) | Claude Code with `claude-opus-4-7[1m]` | Anthropic API via Claude Code CLI | Best agentic build quality available in May 2026. Multi-file refactor with iterative verification fits Opus 4.7's strengths. |
| Runtime observability agent | `anthropic/claude-sonnet-4.6` | OpenRouter (my key) | Best price/quality on OpenRouter's tool-calling collection rankings for 5–10-turn agent loops as of May 2026. Strong narrative output. |
| Fallback if rate-limited | `anthropic/claude-haiku-4.5` | OpenRouter | Cheaper and faster per turn. Would be logged here if triggered. Wasn't. |

The Cline VS Code extension was not used during the build. My OpenRouter key lives in `.env` and is consumed only by the AI observability service at runtime — its natural place in the architecture.

Build-phase tools were Claude Code's built-ins (Read, Edit, Write, Bash, plus the Agent sub-agent for parallel research during planning). No external MCP servers were used.

---

## Per-block notes

### Block 0 — Setup

Worked in place at the repo root rather than a sibling directory. The pristine starter app (`sre-store/`, `sre-store.zip`) stays locally but is `.gitignore`-d from the published repo, and is kept as a reference until the build is done.

**Manual fix #1 — `osxkeychain` credsStore leftover.** Docker config had `"credsStore": "osxkeychain"` left over from a prior Docker Desktop install. That binary isn't on Rancher Desktop's PATH, so the first `docker compose up` failed at image pull. Removed the line from `~/.docker/config.json` (backup saved). Public Docker Hub images don't need a credential helper. Not an LLM failure — environment issue — but logged for completeness.

Verified the baseline app worked end-to-end via curl before changing anything: login → cart → checkout → payment → frontend HTTP 200. App is uninstrumented as advertised. Ready for Block 1.

### Block 1 — Blueprint

Three files authored by Claude Code (Opus 4.7), in this order: catalog → guidelines → initial.md. The catalog is the contract everything else must match, so it goes first.

- **`metric-catalog.md`** — every metric and log field with description, why it matters, normal range, and what a change implies. Followed the "strong vs weak" pattern: every entry explains what a change *means*, not just what the metric measures. Forbidden-label list is explicit, funnel queries enumerated at the bottom.
- **`guidelines.md`** — log format, metric naming, error-surfacing rules, plus the reusable procedures section. The triage loop is §6, with a worked example.
- **`initial.md`** — bootstrap prompt with five phases, each with explicit verify gates. Encodes the SRE system prompt verbatim so the runtime agent and the build-time instructions stay aligned.

Verified consistency via regex cross-check: every metric name in `initial.md` is documented in `metric-catalog.md`. No manual fixes — Blueprint writing was straightforward synthesis from the planning-phase research.

### Block 2 — Prometheus instrumentation

Wired `prom-client@15` into the backend. Single dedicated `Registry`. Default Node metrics on. HTTP middleware records `http_requests_total` + `http_request_duration_seconds` + `http_requests_in_flight` per request. Business counters at six call sites in the route files. DB-query timing via a small `time(name, fn)` helper applied only at the three named queries from the catalog.

**Manual fix #2 — Express `req.baseUrl` clearing on the error path.** First implementation labeled failed-login 401s as `route="/login"` instead of `/api/auth/login`. Root cause: when a route handler calls `next(err)`, control passes to the global error handler, and by that point Express has restored `req.baseUrl` to the parent app's mount (empty), so `req.baseUrl + req.route.path` reads only the local path. I tried three increasingly desperate hooks before finding a working pattern:

1. Read on `res.on('finish')` — too late; baseUrl was already cleared.
2. Hook `res.end()` — same problem.
3. Hook `res.status()` and `res.json()` — same problem; the error handler calls these *after* Express has unwound baseUrl.
4. **Working fix:** a tiny exported middleware `stampRouteTemplate` that captures `req.baseUrl` into `res.locals.stampedBaseUrl` at the moment each router's first middleware runs, when baseUrl is correct. One `router.use(stampRouteTemplate)` line per router. The metrics middleware prefers the stamped value over the live `req.baseUrl` when it resolves the template.

The kind of bug an LLM that confidently writes Express middleware will produce and not notice until production. Took two redeploy cycles to nail because each "obvious" hook had subtle Express timing issues.

**Manual fix #3 — zoxide override in the shell snapshot.** Before this block could typecheck cleanly, the Claude Code harness's captured zsh snapshot had `cd ()` overridden to zoxide's `__zoxide_z`, which made `cd backend && npm run typecheck` fail with "no match found" because zoxide had no history. Fixed by editing `~/.zshrc` (`zoxide init --cmd cd zsh` → `zoxide init zsh`) AND patching the captured snapshot at `~/.claude/shell-snapshots/snapshot-zsh-*.sh` to remove the `cd () { __zoxide_z "$@"; }` function. Environment issue, not LLM behavior, but logged for completeness.

Verified the `/metrics` endpoint exposes every catalog metric with non-zero values after one user-journey drive. Prometheus target `shop-backend` shows `up`. PromQL `sum by (route)(rate(http_requests_total[1m]))` returns labeled series per route.

### Block 3 — Logs

Wired `pino@10` + `pino-http@11` writing JSON to stdout, with Filebeat tailing the container via the docker JSON-file driver and shipping to Elasticsearch. Single-node ES, security off, Kibana for ad-hoc exploration. Field schema is ECS-aligned. This block had the most manual fixes — Filebeat 9.x and pino-http both have non-trivial gotchas.

**Manual fix #4 — pino `base: undefined` vs. `bindings()`.** Set `base: undefined` to suppress pino's default `{pid, hostname}` AND added a `bindings(bindings) { ... bindings.pid }` formatter. The combination meant `bindings()` was called with `{}` and reading `.pid` from undefined crashed the container at startup. Dropped the `process.pid` field from `bindings()` — `base: undefined` already does what I wanted.

**Manual fix #5 — Elasticsearch 9.x strips curl AND wget.** My compose healthcheck used `wget --spider` based on a research-agent report that said the ES image strips curl. The 9.4.1 image strips *both*. ES was reporting green internally, but Docker marked the container `unhealthy` and downstream services (Filebeat, Kibana) refused to start with `depends_on: condition: service_healthy`. Fixed by using bash's built-in `/dev/tcp` probe in `CMD-SHELL` — no external binary required.

**Manual fix #6 — Filebeat command syntax.** First `command: ["-e", "-strict.perms=false"]` made Filebeat print its help text and exit 1. The image ENTRYPOINT is `filebeat` itself (not a wrapper), so my args bypassed it. Right form: `["filebeat", "-e", "--strict.perms=false"]`.

**Manual fix #7 — Filebeat `container` input deprecated in 9.x.** First config used `type: container` per the research-agent report. Filebeat 9.x has fully deprecated that input — the first start spat out a clear error pointing at `type: filestream`. One config edit, plus `id: shop-backend-${data.docker.container.id}` because filestream requires a unique id per input.

**Manual fix #8 — pino-http req/res serializers put fields under the key name.** Configured `serializers: { req: ..., res: ... }` to reshape into ECS, but pino-http keeps serializer output under the key name (`req: {...}`, `res: {...}`), so my fields landed at `req.url.path`, not `url.path`. Fixed by switching to `customProps` (which merges into the record root) and returning `undefined` from `req`/`res` serializers to suppress the nested versions entirely.

**Manual fix #9 — `url.path` was router-local.** First indexed docs showed `url.path: "/login"` for failed-login requests and `url.path: "/1/related"` for the related-products call — because Express's `req.url` is router-local. Same root cause as the metrics-middleware baseUrl bug from Block 2. Fixed by preferring `req.originalUrl` over `req.url` in `customProps`.

Verified by indexing 42 docs across info/warn levels. All four business events (`payment recorded`, `order created`, `login succeeded`, `handled error: <code>`) carry full `ecom.*` payloads. `url.path`, `http.request.method`, `http.response.status_code`, `event.duration`, `event.outcome`, `trace.id` all land at their ECS-canonical root paths.

### Block 4 — Grafana

`grafana:11.4.0` with anonymous admin access. File-based provisioning for both datasources and dashboards. Pinned datasource UIDs (`prometheus`, `elasticsearch`) referenced by the dashboard JSON's `datasource: { type, uid }` blocks — this is the canonical fix for the "Datasource not found" gotcha because auto-generated UIDs differ across container recreates.

Dashboard layout matches `guidelines.md` §3:

- Row 1 (RED): request rate by route, status family stacked, error rate %, latency p50/p95/p99 by route.
- Row 2 (Funnel): cart-adds/min, checkouts/min, payments/min, payment failure rate (thresholded green/yellow/red).
- Row 3 (Logs): warn+error logs panel pointed at Elasticsearch.
- Variable `$route` drives the top-row filters, populated from `label_values(http_requests_total, route)`.

**No manual fixes this block.** First boot worked on the first try. The combination of pinned UIDs, Grafana 11's stable schemaVersion 39, and explicitly typed datasource references in every panel meant nothing surprised me.

One subtle thing worth knowing for the next time someone scripts against the Grafana proxy: the Elasticsearch datasource proxy refuses raw POSTs against any path other than `_msearch` (security: prevents abuse of the proxy as a write channel). Panel queries naturally use `_msearch`, so this is invisible from the UI.

Verified by querying through the Grafana proxy: Prometheus returns `/api/payment/` p95 ≈ 490 ms (matches the catalog's "uniform 120–450 ms → p95 ≈ 450 ms" prediction). ES via `_msearch` returns warn/error logs with `ecom.error_code` and `url.path` populated.

### Block 5 — AI observability service

Python 3.12 + FastAPI + the `openai` SDK pointed at OpenRouter. OpenRouter is OpenAI-compatible so the `tools` parameter works as-is with no translation layer. Native function calling, not MCP — documented as the natural upgrade path in the module docstrings.

The model is `anthropic/claude-sonnet-4.6`, via the OpenRouter key.

Tool design rules I followed:

- One verb-noun per tool. Description leads with WHEN to use it, not WHAT it returns.
- `time_range` is an enum (`5m | 15m | 1h | 24h`) so the model can't invent "1.5 hours" and break the query.
- Pre-aggregate everything. `query_prometheus` returns top-10 series with last/min/max/mean and p50/p95 baked in; samples capped at 10 points per series. Log hits are stripped to ECS essentials. Full `_source` would be too noisy for the model.
- Errors come back as `{"error": str, "hint": str}`, never raised. The model self-corrects on the next turn instead of crashing the loop.

The agent loop in `app.py:investigate` is short on purpose: max 10 iterations, 8 KB tool-result cap (16 KB for the catalog), temperature 0.2. It terminates when the model emits a message with no `tool_calls`. Every iteration is logged to stderr as JSON with iter#, tool name, args, duration, and result preview.

**Manual fix #10 — logger output polluted CLI stdout.** First CLI invocation produced `INFO: ...` log lines mixed with the JSON payload, breaking `json.loads()` downstream. Fixed by setting `logging.basicConfig(stream=sys.stderr, ...)` so server logs still go where Filebeat could pick them up, but CLI stdout stays pure JSON.

**Manual fix #11 — LLM hallucinated a metric name, tool returned silent zero.** First CLI test asked about login failures. Sonnet 4.6 tried `ecom_auth_attempts_total` (the real name is `auth_login_attempts_total`). My `query_prometheus` returned `series_count: 0` with no explanation, and the model had to *infer* that meant "metric doesn't exist." Improved the tool to add an explicit hint whenever the result set is empty:

> *"Zero series matched. Most likely a misspelled metric name — call get_metric_catalog."*

After the hint, the model pivoted cleanly: searched logs → queried with the wrong name → got the hint → called the catalog → re-queried with the right name → wrapped up. That's "follow-up tool calls based on prior results, not a fixed sequence," earned by a single targeted tool-output change.

### Block 6 — End-to-end demo capture

`scripts/drive-traffic.sh` is a small load generator: logs in, fires three bad-credential probes (to populate the warn stream), then loops N iterations of browse + cart + checkout + pay with a small sleep between iterations so events distribute in time.

The canonical demo:

1. Drove 20 iterations at the default 8% failure rate → wave 1 of healthy traffic.
2. Set `PAYMENT_FAILURE_RATE=0.5`, restarted backend, drove 20 more → wave 2 with elevated failures.
3. After ingestion settled, Prometheus showed `sum(rate(ecom_payments_total{outcome="failed"}[5m])) / sum(rate(ecom_payments_total[5m])) = 0.375` (37.5%) — well above the documented 8% baseline.

The investigation: `curl POST /investigate` with `"Anything wrong with payments in the last 15 minutes? Walk me through your reasoning and give me a triage-style writeup with a concrete next action."` The agent ran 3 iterations and 7 tool calls:

- **Iter 0** ran three `query_prometheus` calls in parallel — failure-rate ratio, payment route p95, total payment request rate. The model batched its initial confirmation set.
- **Iter 1** called `get_metric_catalog` to ground the next query in real field names, then `get_recent_errors` for `/api/payment` to get the breakdown by error code.
- **Iter 2** queried payment p95 again (re-confirmation) and DB `payment_record` p95 — the negative-evidence check that rules out an internal cause.
- **Iter 3** was the text-only conclusion.

The final insight (in `docs/sample-investigation.json` and quoted verbatim in the README) hits every goal I set for it: narrative form, references catalog metric names exactly, explicitly cites the catalog line *"failed rate climbing above ~10% → either someone bumped PAYMENT_FAILURE_RATE"*, and ends with a concrete next action. The catalog is being used as a runtime contract, not just documentation.

**Manual fix #12 — Grafana image renderer not installed.** Tried to capture a dashboard PNG via `/render?d=user-journey`. Grafana returned a "No image renderer available/installed" placeholder image (478×208). The renderer plugin is a separate ~250 MB container; not worth the dependency for the time I'd boxed for this. Compromise: dumped panel data via the Prometheus API to `docs/dashboard-state.json` so the README has a concrete numerical snapshot of what the live dashboard shows. A real browser screenshot would be a nice-to-have follow-up.

The README was filled in across all six previously-placeholder sections in this block while the demo context was fresh, rather than in Block 7 as originally planned.

---

## Summary: where the AI fell short

### Build phase — places I stepped in by hand

The full list is above, but to summarize the *types* of mistake Opus 4.7 made under my direction:

- **Subtle framework timing.** The Express `req.baseUrl` bug and the `req.url` vs. `req.originalUrl` bug are the same shape — both come from confidently writing what looks right but doesn't survive the error path. Took multiple iterations to diagnose the first one because each "obvious" hook had its own timing failure.
- **Out-of-date library knowledge.** The research-agent reports written during planning correctly named Filebeat 9.4 and ES 9.4 but missed that 9.x deprecated `container` input and stripped `wget` from the base image. The LLM didn't know what it didn't know; Filebeat and Docker's healthcheck told me directly.
- **Naive API shapes.** pino-http puts serializer output under the key name; pino's `bindings()` formatter is incompatible with `base: undefined` if you read `bindings.pid` inside it. Both surface only on first boot.

All caught at the verify gate of the block where they happened. No problems leaked across block boundaries.

### Runtime phase — limits of the observability agent

Only one real gap, and the fix that resolved it ended up improving the agent's behaviour overall:

- **Metric-name hallucination on first attempt.** Sonnet 4.6 guessed `ecom_auth_attempts_total` instead of `auth_login_attempts_total`. The model couldn't tell from `series_count: 0` whether the metric existed and was simply quiet, or whether the name was wrong. Adding an explicit hint on empty Prometheus results made it pivot to `get_metric_catalog` and try again with the right name. That same fix is what produced the multi-turn, hypothesis-driven trace shown in the README.

### Architectural awareness — no MCP layer

The service uses native OpenAI-compatible function calling, not MCP. MCP is the right transport when these tools need to be reused across multiple agents or processes — a Claude Desktop integration for on-call would be the obvious case. For four in-process Python functions co-located with the LLM client, MCP would add a JSON-RPC layer without changing behavior. The natural upgrade path is documented in `ai_service/app.py` and `ai_service/tools.py` docstrings.

---

## Self-assessment

Walking the goals I set for myself against the current state, with concrete evidence:

| Goal | Evidence |
|---|---|
| **Autonomy & tooling** — real tools + context loop, not a dressed-up script | `docs/sample-investigation.json` shows the agent making 7 tool calls across 3 iterations on a single question. Iteration 0 issues three parallel `query_prometheus` calls, iteration 1 consults `get_metric_catalog` to ground the next query, iteration 2 fetches DB latency as negative evidence — the next move depends on the previous result every time. |
| **Quality of insights** — actionable narrative, not raw numbers | The captured insight identifies the payment provider as the root cause, quotes the metric catalog's own diagnostic guidance verbatim ("failed rate climbing above ~10% → either someone bumped PAYMENT_FAILURE_RATE"), and ends with a concrete next action (check the env var on the backend container). |
| **Prompt rigor** — `initial.md` should reproduce comparable output on a fresh copy | `initial.md` runs in five phases, each with an explicit verify gate. Metric names cross-checked against `metric-catalog.md`. Filebeat 9.x deprecation and ES 9.x healthcheck quirks are now called out in the prompt itself so the next re-run doesn't repeat my mistakes. |
| **Observability design** — dashboards answer on-call questions | The User Journey dashboard's three rows match the on-call mental model: RED metrics for *is something wrong*, the funnel for *where in the user journey*, and a recent-errors log panel for *what specifically*. The `$route` variable scopes the RED panels for focused investigation. |
| **Honesty & tradeoffs** — manual fixes documented, cardinality & cost articulated | This file lists twelve named manual fixes. The README's Tradeoffs section explicitly addresses cardinality (with arithmetic for current label dimensions), sampling (none, with the scale at which we'd start dropping 2xx GETs), log volume (single data stream, no ILM), MCP vs. native function calling, and model choice. |

Honest gaps worth flagging:

- No dashboard screenshot. The Grafana image-renderer plugin isn't included (it's a ~250 MB extra container), so `docs/dashboard-state.json` captures the panel values numerically instead.
- No retroactive squash of commits. The per-block history reflects how the build actually went and is left intact.
- The frontend wasn't touched — I scoped it deliberately so that polishing the app isn't the point.
