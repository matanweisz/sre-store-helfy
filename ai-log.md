# AI Log

Honest record of every LLM used during this assignment, what for, and where the AI fell short.

## Model choices

| Phase | Model | Provider | Why |
|---|---|---|---|
| **Build (this repo)** | Claude Code with `claude-opus-4-7[1m]` | Anthropic API (via Claude Code CLI) | Best agentic build quality available to the developer in May 2026; multi-file refactor + iterative verification fit Opus 4.7's strengths |
| **Runtime AI observability service** | `anthropic/claude-sonnet-4.6` | OpenRouter (key provided by Helfy) | Best price/quality for 5–10 turn agentic tool-calling loops on OpenRouter as of May 2026 (per OpenRouter's tool-calling collection rankings). Strong narrative output, the criterion the PDF calls "insight, not numbers." |
| _(Fallback if rate-limited)_ | `anthropic/claude-haiku-4.5` | OpenRouter | Cheaper, sub-second per turn. Would document in this file if triggered. |

The Cline VS Code extension was **not used** in the build phase. The OpenRouter key Helfy provided lives in `.env` and is consumed only by the AI observability service at runtime — its natural architectural place in the deliverable.

## Build-phase prompts and decisions

(Filled in continuously as we work through the blocks.)

### Block 0 — Setup
- Plan reviewed and approved. Plan file at `~/.claude/plans/in-this-directory-we-replicated-boole.md`.
- Decision: work in place in `/Users/matan.weisz/git/sre-assignment/` rather than a sibling dir. Original PDF/email/zip stay locally but are `.gitignore`-d from the published repo.
- Decision: keep `sre-store/` directory as a pristine reference until the build is done (also `.gitignore`-d).

### Block 1 — Blueprint
_(TBD)_

### Block 2 — Prometheus instrumentation
_(TBD)_

### Block 3 — Logs
_(TBD)_

### Block 4 — Grafana
_(TBD)_

### Block 5 — AI service
_(TBD)_

### Block 6 — E2E investigation
_(TBD)_

## AI-gap awareness — where the AI fell short

Two categories, both honest:

### Build phase (where I stepped in by hand)

_(Filled in as it happens — every place the build-phase AI got something wrong that the developer fixed manually.)_

### Runtime phase (limits of the observability agent)

_(Filled in after the Block 6 sample investigation — every place the runtime LLM (Sonnet 4.6) failed to converge, hallucinated a metric name, or needed prompt-tightening to produce a useful answer.)_

## MCP and other tooling

- The AI observability service uses **native OpenAI-compatible function calling**, not MCP. MCP (Model Context Protocol) is a transport for cross-process tool servers — for 4 in-process Python functions sitting next to Prometheus and Elasticsearch, MCP adds JSON-RPC and zero value. **The natural upgrade path is to expose these same tools via an MCP server** once they need to be reusable across multiple agents (e.g., a Claude Desktop integration for on-call). Mentioned here as architectural awareness.
- Build phase used Claude Code's built-in tools (Read, Edit, Write, Bash, the Agent sub-agent for parallel research). No external MCP servers were used during the build.
