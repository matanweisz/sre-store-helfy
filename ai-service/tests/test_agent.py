"""Structuring pass + end-to-end agent loop, driven by a fake LLM client."""

from __future__ import annotations

import json

import httpx
import respx
from _fakes import FakeOpenAIClient, assistant, tool_call

from ai_service import app
from ai_service.config import get_settings
from ai_service.report import IncidentReport

VALID_REPORT_JSON = json.dumps(
    {
        "anomaly": "Payment failure rate elevated",
        "window": "last 15 minutes",
        "supporting_evidence": ["failure ratio 0.40 vs 0.08 baseline"],
        "negative_evidence": ["db payment_record p95 flat at 24ms"],
        "root_cause": "PAYMENT_FAILURE_RATE was bumped",
        "next_action": "check the env var on the backend container",
        "confidence": "high",
    }
)


# ─── structure_report ────────────────────────────────────────────────────────


def test_structure_report_parses_valid_json() -> None:
    fake = FakeOpenAIClient([assistant(VALID_REPORT_JSON)])
    rep = app.structure_report(fake, "m", "payments are failing ~40%")
    assert isinstance(rep, IncidentReport)
    assert rep.confidence == "high"
    assert "env var" in rep.next_action


def test_structure_report_none_on_garbage() -> None:
    # Both attempts return non-JSON → graceful None.
    fake = FakeOpenAIClient([assistant("not json"), assistant("still not json")])
    assert app.structure_report(fake, "m", "something happened") is None


def test_structure_report_skips_empty_prose() -> None:
    fake = FakeOpenAIClient([])  # must never be called
    assert app.structure_report(fake, "m", "   ") is None


# ─── investigate (full loop) ─────────────────────────────────────────────────


@respx.mock
def test_investigate_runs_loop_and_structures(monkeypatch) -> None:
    prom_payload = {
        "status": "success",
        "data": {"resultType": "matrix", "result": [{"metric": {}, "values": [[1, "0.40"], [2, "0.42"]]}]},
    }
    respx.get(url__regex=r".*/api/v1/query_range").mock(return_value=httpx.Response(200, json=prom_payload))

    # Scripted LLM: call a tool, then conclude in prose, then the structuring call.
    fake = FakeOpenAIClient(
        [
            tool_call("c1", "query_prometheus", json.dumps({"promql": "sum(rate(x[1m]))", "time_range": "15m"})),
            assistant("Payment failure rate is ~40% vs the 8% baseline over the last 15 minutes."),
            assistant(VALID_REPORT_JSON),
        ]
    )
    monkeypatch.setattr(app, "get_client", lambda: fake)

    resp = app.investigate("anything wrong with payments?")
    assert resp.finish_reason == "answer"
    assert resp.iterations == 2
    assert len(resp.trace) == 1
    assert resp.trace[0].tool == "query_prometheus"
    assert "40%" in resp.insight
    assert resp.report is not None
    assert resp.report.confidence == "high"


def test_investigate_without_structured_output(monkeypatch) -> None:
    monkeypatch.setenv("STRUCTURED_OUTPUT", "false")
    get_settings.cache_clear()

    fake = FakeOpenAIClient([assistant("All healthy in the last 15 minutes; nothing anomalous.")])
    monkeypatch.setattr(app, "get_client", lambda: fake)

    resp = app.investigate("any issues?")
    assert resp.report is None
    assert "healthy" in resp.insight
    assert len(fake.calls) == 1  # no extra structuring call
