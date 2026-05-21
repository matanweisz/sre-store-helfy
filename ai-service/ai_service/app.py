"""AI observability service.

A standalone FastAPI app that runs a multi-turn LLM tool-calling loop against
the live Prometheus + Elasticsearch stack. The LLM picks what to look at;
this code executes the picks and feeds results back.

Architecture:
  POST /investigate {"question": "..."}
    -> investigate(question) — agent loop, max 10 iterations
       -> client.chat.completions.create(model, messages, tools, ...)
          -> if message has tool_calls: execute each, append role=tool, loop
          -> else (text-only response): return as insight

CLI mode is available via `python -m ai_service.app "your question"`.

Key choices documented in ai-log.md:
  - openai SDK pointed at OpenRouter (OpenRouter is OpenAI-compatible).
  - Native function calling, not MCP. Simpler for an in-process 4-tool set.
  - Hard iter cap (10) + payload cap (8 KB per tool result) prevent runaway
    loops from burning the OpenRouter budget.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from openai import OpenAI
from pydantic import BaseModel, Field

from .prompts import SRE_SYSTEM_PROMPT
from .tools import TOOL_REGISTRY, TOOLS

# ─── logging ────────────────────────────────────────────────────────────────────

# Service-emitted JSON logs to stderr so the CLI mode can pipe pure JSON to
# stdout. Server mode (uvicorn) treats stderr the same as stdout for container
# logging — Filebeat picks both up.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format='{"@timestamp":"%(asctime)s","log.level":"%(levelname)s","service":{"name":"ai-service"},"logger":"%(name)s","message":%(message)s}',
)
log = logging.getLogger("ai_service")


def jlog(message: str, **fields: Any) -> None:
    """Emit a structured log line with extra fields."""
    payload = {"msg": message, **fields}
    log.info(json.dumps(payload, default=str))


# ─── env ────────────────────────────────────────────────────────────────────────

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.6")
MAX_AGENT_ITERATIONS = int(os.environ.get("MAX_AGENT_ITERATIONS", "10"))
TOOL_RESULT_CAP_CHARS = int(os.environ.get("TOOL_RESULT_CAP_CHARS", "8000"))
# Catalog is bigger than per-tool cap — allow more for that one tool.
CATALOG_RESULT_CAP_CHARS = int(os.environ.get("CATALOG_RESULT_CAP_CHARS", "16000"))


# ─── OpenAI client (pointed at OpenRouter) ──────────────────────────────────────

if not OPENROUTER_API_KEY:
    jlog(
        "OPENROUTER_API_KEY not set",
        level="WARN",
        hint="set it in .env before docker compose up",
    )

_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        if not OPENROUTER_API_KEY:
            raise HTTPException(
                status_code=500,
                detail="OPENROUTER_API_KEY env var is empty; cannot reach the LLM.",
            )
        # OpenRouter recommends sending these headers so usage shows up correctly
        # under your account. They're optional but harmless.
        _client = OpenAI(
            api_key=OPENROUTER_API_KEY,
            base_url=OPENROUTER_BASE_URL,
            default_headers={
                "HTTP-Referer": "https://github.com/matan-weisz/sre-assignment",
                "X-Title": "SRE Assignment AI Observability",
            },
            # We do our own retries / loop bounds; let httpx surface errors.
            timeout=httpx.Timeout(60.0, connect=10.0),
            max_retries=1,
        )
    return _client


# ─── pydantic models ────────────────────────────────────────────────────────────


class InvestigateRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)


class ToolCallTrace(BaseModel):
    iter: int
    tool: str
    args: dict[str, Any]
    duration_ms: int
    result_preview: str
    truncated: bool


class InvestigateResponse(BaseModel):
    question: str
    model: str
    insight: str
    trace: list[ToolCallTrace]
    iterations: int
    finish_reason: str  # "answer" | "iteration_cap" | "error"


# ─── the agent loop ─────────────────────────────────────────────────────────────


def _truncate_tool_result(name: str, payload: str) -> tuple[str, bool]:
    cap = CATALOG_RESULT_CAP_CHARS if name == "get_metric_catalog" else TOOL_RESULT_CAP_CHARS
    if len(payload) <= cap:
        return payload, False
    return payload[:cap] + f"\n\n[TRUNCATED at {cap} chars — call with narrower args]", True


def investigate(question: str) -> InvestigateResponse:
    client = get_client()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SRE_SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    trace: list[ToolCallTrace] = []

    for iteration in range(MAX_AGENT_ITERATIONS):
        jlog(
            "agent iteration begin",
            iteration=iteration,
            messages_count=len(messages),
        )
        try:
            resp = client.chat.completions.create(
                model=OPENROUTER_MODEL,
                messages=messages,  # type: ignore[arg-type]
                tools=TOOLS,  # type: ignore[arg-type]
                tool_choice="auto",
                temperature=0.2,
            )
        except Exception as e:  # noqa: BLE001 — surface every LLM failure mode
            jlog("LLM call failed", iteration=iteration, error=str(e))
            return InvestigateResponse(
                question=question,
                model=OPENROUTER_MODEL,
                insight=f"LLM call failed: {e}",
                trace=trace,
                iterations=iteration,
                finish_reason="error",
            )

        msg = resp.choices[0].message
        # Append the assistant message verbatim so subsequent tool roles can
        # reference its tool_call_ids.
        assistant_dump: dict[str, Any] = {"role": "assistant"}
        if msg.content:
            assistant_dump["content"] = msg.content
        if msg.tool_calls:
            assistant_dump["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        messages.append(assistant_dump)

        # Termination: model produced a text-only response.
        if not msg.tool_calls:
            jlog(
                "agent finished",
                iteration=iteration,
                finish_reason="answer",
                content_length=len(msg.content or ""),
            )
            return InvestigateResponse(
                question=question,
                model=OPENROUTER_MODEL,
                insight=msg.content or "(empty response)",
                trace=trace,
                iterations=iteration + 1,
                finish_reason="answer",
            )

        # Execute every tool call the model asked for, in order.
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            fn = TOOL_REGISTRY.get(name)
            t0 = time.monotonic()
            if fn is None:
                result: Any = {"error": f"unknown tool {name!r}"}
            else:
                try:
                    result = fn(**args)
                except TypeError as e:
                    result = {"error": f"bad arguments: {e}", "got": args}
                except Exception as e:  # noqa: BLE001
                    result = {"error": f"tool raised: {type(e).__name__}: {e}"}
            duration_ms = int((time.monotonic() - t0) * 1000)
            payload_raw = json.dumps(result, default=str)
            payload, truncated = _truncate_tool_result(name, payload_raw)

            jlog(
                "tool executed",
                iteration=iteration,
                tool=name,
                args=args,
                duration_ms=duration_ms,
                result_chars=len(payload),
                truncated=truncated,
            )

            trace.append(
                ToolCallTrace(
                    iter=iteration,
                    tool=name,
                    args=args,
                    duration_ms=duration_ms,
                    result_preview=payload_raw[:300],
                    truncated=truncated,
                )
            )

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": payload,
                }
            )

    # Fell out of the loop without a text-only answer.
    jlog("agent hit iteration cap", iterations=MAX_AGENT_ITERATIONS)
    return InvestigateResponse(
        question=question,
        model=OPENROUTER_MODEL,
        insight=(
            f"Investigation hit the {MAX_AGENT_ITERATIONS}-iteration cap without "
            "converging. Trace below shows what was attempted."
        ),
        trace=trace,
        iterations=MAX_AGENT_ITERATIONS,
        finish_reason="iteration_cap",
    )


# ─── FastAPI ────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="SRE AI Observability",
    description=(
        "Natural-language → multi-turn tool-calling agent over Prometheus + Elasticsearch."
    ),
    version="0.1.0",
)


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "model": OPENROUTER_MODEL,
        "api_key_present": bool(OPENROUTER_API_KEY),
        "tools": [t["function"]["name"] for t in TOOLS],
    }


@app.post("/investigate", response_model=InvestigateResponse)
def investigate_endpoint(req: InvestigateRequest) -> InvestigateResponse:
    jlog("investigate request", question=req.question)
    return investigate(req.question)


# ─── CLI mode ───────────────────────────────────────────────────────────────────


def _cli() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m ai_service.app '<question>'", file=sys.stderr)
        return 2
    question = " ".join(sys.argv[1:])
    result = investigate(question)
    print(json.dumps(result.model_dump(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
