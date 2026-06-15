"""AI observability service — `POST /investigate` and a CLI."""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

from .config import get_settings
from .llm import LLMClient, build_client
from .prompts import build_system_prompt
from .report import IncidentReport, incident_report_schema
from .tools import TOOL_REGISTRY, TOOLS

# ─── progress + logging ────────────────────────────────────────────────────────
# Two surfaces: a one-line human-readable status to stderr (for `docker compose
# logs -f ai-service` and CLI users), and a structured JSON record at debug
# level for log aggregation. The structured record is only emitted when the
# AI_STRUCTURED_LOGS env is set, so the default surface stays clean.

_quiet = False  # set by the CLI --quiet flag

logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(message)s")
_jlog = logging.getLogger("ai_service")


def progress(line: str, **fields: Any) -> None:
    """One-line human-readable status to stderr + optional structured JSON."""
    if not _quiet:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    if get_settings().ai_structured_logs:
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


# ─── LLM client ────────────────────────────────────────────────────────────────
# The provider (OpenRouter or native Anthropic) is chosen by config; the agent
# loop below talks only to the neutral LLMClient seam in llm.py. Indirected
# through a module function so tests can inject a FakeLLMClient.


def make_client() -> LLMClient:
    return build_client(get_settings())


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
    insight: str  # the prose incident note (human-readable)
    report: IncidentReport | None = None  # structured form (machine-readable)
    trace: list[ToolCallTrace]
    iterations: int
    finish_reason: str  # "answer" | "iteration_cap" | "error"
    elapsed_ms: int


# ─── structured-output pass ──────────────────────────────────────────────────


def build_report(client: LLMClient, prose: str) -> IncidentReport | None:
    """Ask the client for a structured report and validate it. None on failure."""
    raw = client.structure(prose, incident_report_schema())
    if not raw:
        return None
    try:
        return IncidentReport.model_validate(raw)
    except Exception:  # noqa: BLE001 — a malformed object just means no structured report
        return None


# ─── agent loop ────────────────────────────────────────────────────────────────


def _truncate(name: str, payload: str) -> tuple[str, bool]:
    s = get_settings()
    cap = s.catalog_result_cap_chars if name == "get_metric_catalog" else s.tool_result_cap_chars
    if len(payload) <= cap:
        return payload, False
    return payload[:cap] + f"\n\n[TRUNCATED at {cap} chars — call with narrower args]", True


def investigate(question: str) -> InvestigateResponse:
    s = get_settings()
    max_iterations = s.max_agent_iterations
    client = make_client()
    client.start(build_system_prompt(s), question)
    model = client.model
    trace: list[ToolCallTrace] = []
    started = time.monotonic()

    progress(f"[ai] ▶ investigating: {question[:80]}{'...' if len(question) > 80 else ''}")

    for iteration in range(max_iterations):
        try:
            turn = client.step(TOOLS)
        except Exception as e:  # noqa: BLE001 — surface every LLM failure mode
            progress(f"[ai] ✗ LLM call failed: {e}")
            return InvestigateResponse(
                question=question,
                model=model,
                insight=f"LLM call failed: {e}",
                trace=trace,
                iterations=iteration,
                finish_reason="error",
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

        if not turn.tool_calls:
            prose = turn.text or "(empty response)"
            report = build_report(client, prose) if s.structured_output else None
            elapsed = int((time.monotonic() - started) * 1000)
            progress(
                f"[ai] ✔ done — {iteration + 1} iters, {len(trace)} tool calls, "
                f"{'report' if report else 'no report'}, {elapsed} ms"
            )
            return InvestigateResponse(
                question=question,
                model=model,
                insight=prose,
                report=report,
                trace=trace,
                iterations=iteration + 1,
                finish_reason="answer",
                elapsed_ms=elapsed,
            )

        for tc in turn.tool_calls:
            name, args = tc.name, tc.arguments
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
            client.add_tool_result(tc.id, payload)

    elapsed = int((time.monotonic() - started) * 1000)
    progress(f"[ai] ⚠ hit {max_iterations}-iteration cap, returning partial findings ({elapsed} ms)")
    return InvestigateResponse(
        question=question,
        model=model,
        insight=f"Investigation hit the {max_iterations}-iteration cap without converging. Trace below shows what was attempted.",
        trace=trace,
        iterations=max_iterations,
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
    s = get_settings()
    if s.llm_provider == "anthropic":
        model, key_present = s.anthropic_model, bool(s.anthropic_api_key)
    else:
        model, key_present = s.openrouter_model, bool(s.openrouter_api_key)
    return {
        "ok": True,
        "provider": s.llm_provider,
        "model": model,
        "api_key_present": key_present,
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
