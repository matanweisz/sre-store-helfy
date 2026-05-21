"""AI observability service — `POST /investigate` and a CLI."""

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

# ─── env ───────────────────────────────────────────────────────────────────────

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.6")
MAX_AGENT_ITERATIONS = int(os.environ.get("MAX_AGENT_ITERATIONS", "10"))
TOOL_RESULT_CAP_CHARS = int(os.environ.get("TOOL_RESULT_CAP_CHARS", "8000"))
CATALOG_RESULT_CAP_CHARS = int(os.environ.get("CATALOG_RESULT_CAP_CHARS", "16000"))

# ─── progress + logging ────────────────────────────────────────────────────────
# Two surfaces: a one-line human-readable status to stderr (for `docker compose
# logs -f ai-service` and CLI users), and a structured JSON record at debug
# level for log aggregation. The structured record is only emitted when the
# AI_STRUCTURED_LOGS env is set, so the default surface stays clean.

_quiet = False  # set by the CLI --quiet flag
_emit_json = os.environ.get("AI_STRUCTURED_LOGS", "").lower() in ("1", "true", "yes")

logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(message)s")
_jlog = logging.getLogger("ai_service")


def progress(line: str, **fields: Any) -> None:
    """One-line human-readable status to stderr + optional structured JSON."""
    if not _quiet:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    if _emit_json:
        payload = {"@timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "msg": line, **fields}
        _jlog.warning(json.dumps(payload, default=str))


def _summarize_args(args: dict[str, Any], width: int = 80) -> str:
    if not args:
        return ""
    parts = []
    for k, v in args.items():
        sv = str(v)
        if len(sv) > 40:
            sv = sv[:37] + "..."
        parts.append(f"{k}={sv}")
    out = ", ".join(parts)
    return out if len(out) <= width else out[: width - 3] + "..."


# ─── OpenAI client (pointed at OpenRouter) ─────────────────────────────────────

_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        if not OPENROUTER_API_KEY:
            raise HTTPException(status_code=500, detail="OPENROUTER_API_KEY env var is empty")
        # OpenRouter recommends sending Referer + X-Title so usage attribution works.
        _client = OpenAI(
            api_key=OPENROUTER_API_KEY,
            base_url=OPENROUTER_BASE_URL,
            default_headers={
                "HTTP-Referer": "https://github.com/matan-weisz/sre-assignment",
                "X-Title": "SRE Assignment AI Observability",
            },
            timeout=httpx.Timeout(60.0, connect=10.0),
            max_retries=1,
        )
    return _client


# ─── models ────────────────────────────────────────────────────────────────────


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
    elapsed_ms: int


# ─── agent loop ────────────────────────────────────────────────────────────────


def _truncate(name: str, payload: str) -> tuple[str, bool]:
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
    started = time.monotonic()

    progress(f"[ai] ▶ investigating: {question[:80]}{'...' if len(question) > 80 else ''}")

    for iteration in range(MAX_AGENT_ITERATIONS):
        try:
            resp = client.chat.completions.create(
                model=OPENROUTER_MODEL,
                messages=messages,  # type: ignore[arg-type]
                tools=TOOLS,  # type: ignore[arg-type]
                tool_choice="auto",
                temperature=0.2,
            )
        except Exception as e:  # noqa: BLE001 — surface every LLM failure mode
            progress(f"[ai] ✗ LLM call failed: {e}")
            return InvestigateResponse(
                question=question,
                model=OPENROUTER_MODEL,
                insight=f"LLM call failed: {e}",
                trace=trace,
                iterations=iteration,
                finish_reason="error",
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

        msg = resp.choices[0].message
        assistant_dump: dict[str, Any] = {"role": "assistant"}
        if msg.content:
            assistant_dump["content"] = msg.content
        if msg.tool_calls:
            assistant_dump["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ]
        messages.append(assistant_dump)

        if not msg.tool_calls:
            elapsed = int((time.monotonic() - started) * 1000)
            progress(f"[ai] ✔ done — {iteration + 1} iters, {len(trace)} tool calls, {elapsed} ms")
            return InvestigateResponse(
                question=question,
                model=OPENROUTER_MODEL,
                insight=msg.content or "(empty response)",
                trace=trace,
                iterations=iteration + 1,
                finish_reason="answer",
                elapsed_ms=elapsed,
            )

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
            payload, truncated = _truncate(name, payload_raw)

            progress(
                f"[ai] iter {iteration}  → {name}({_summarize_args(args)})  ({duration_ms} ms)",
                iteration=iteration, tool=name, args=args, duration_ms=duration_ms, truncated=truncated,
            )

            trace.append(
                ToolCallTrace(
                    iter=iteration, tool=name, args=args,
                    duration_ms=duration_ms,
                    result_preview=payload_raw[:300],
                    truncated=truncated,
                )
            )
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": payload})

    elapsed = int((time.monotonic() - started) * 1000)
    progress(f"[ai] ⚠ hit {MAX_AGENT_ITERATIONS}-iteration cap, returning partial findings ({elapsed} ms)")
    return InvestigateResponse(
        question=question,
        model=OPENROUTER_MODEL,
        insight=f"Investigation hit the {MAX_AGENT_ITERATIONS}-iteration cap without converging. Trace below shows what was attempted.",
        trace=trace,
        iterations=MAX_AGENT_ITERATIONS,
        finish_reason="iteration_cap",
        elapsed_ms=elapsed,
    )


# ─── FastAPI ───────────────────────────────────────────────────────────────────

app = FastAPI(
    title="SRE AI Observability",
    description="Natural-language → multi-turn tool-calling agent over Prometheus + Elasticsearch.",
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
    return investigate(req.question)


# ─── CLI ───────────────────────────────────────────────────────────────────────


def _cli() -> int:
    global _quiet
    args = sys.argv[1:]
    if "--quiet" in args:
        _quiet = True
        args = [a for a in args if a != "--quiet"]
    if not args:
        print("usage: python -m ai_service.app [--quiet] '<question>'", file=sys.stderr)
        return 2

    question = " ".join(args)
    result = investigate(question)
    print(json.dumps(result.model_dump(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
