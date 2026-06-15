"""End-to-end agent loop + report validation, driven by a neutral fake client."""

from __future__ import annotations

import json

import httpx
import respx
from _fakes import FakeLLMClient

from ai_service import app
from ai_service.config import get_settings
from ai_service.llm import AssistantTurn, ToolCall

VALID_REPORT = {
    "anomaly": "Payment failure rate elevated",
    "window": "last 15 minutes",
    "supporting_evidence": ["failure ratio 0.40 vs 0.08 baseline"],
    "negative_evidence": ["db payment_record p95 flat at 24ms"],
    "root_cause": "PAYMENT_FAILURE_RATE was bumped",
    "next_action": "check the env var on the backend container",
    "confidence": "high",
}


# ─── build_report ────────────────────────────────────────────────────────────


def test_build_report_validates_structured_dict() -> None:
    rep = app.build_report(FakeLLMClient([], structured=VALID_REPORT), "prose")
    assert rep is not None and rep.confidence == "high"


def test_build_report_none_when_structure_none() -> None:
    assert app.build_report(FakeLLMClient([], structured=None), "prose") is None


def test_build_report_none_on_invalid_dict() -> None:
    assert app.build_report(FakeLLMClient([], structured={"bogus": "x"}), "prose") is None


# ─── investigate (full loop over the seam) ───────────────────────────────────


@respx.mock
def test_investigate_runs_loop_and_structures(monkeypatch) -> None:
    prom_payload = {
        "status": "success",
        "data": {"resultType": "matrix", "result": [{"metric": {}, "values": [[1, "0.40"], [2, "0.42"]]}]},
    }
    respx.get(url__regex=r".*/api/v1/query_range").mock(return_value=httpx.Response(200, json=prom_payload))

    turns = [
        AssistantTurn(tool_calls=[ToolCall("c1", "query_prometheus", {"promql": "sum(rate(x[1m]))", "time_range": "15m"})]),
        AssistantTurn(text="Payment failure rate is ~40% vs the 8% baseline over the last 15 minutes."),
    ]
    fake = FakeLLMClient(turns, structured=VALID_REPORT)
    monkeypatch.setattr(app, "make_client", lambda: fake)

    resp = app.investigate("anything wrong with payments?")
    assert resp.finish_reason == "answer"
    assert resp.iterations == 2
    assert len(resp.trace) == 1
    assert resp.trace[0].tool == "query_prometheus"
    assert "40%" in resp.insight
    assert resp.report is not None and resp.report.confidence == "high"
    assert fake.tool_results and fake.tool_results[0][0] == "c1"
    assert fake.started is not None  # start() was called with system + question


def test_investigate_without_structured_output(monkeypatch) -> None:
    monkeypatch.setenv("STRUCTURED_OUTPUT", "false")
    get_settings.cache_clear()

    fake = FakeLLMClient([AssistantTurn(text="All healthy in the last 15 minutes; nothing anomalous.")],
                         structured=VALID_REPORT)
    monkeypatch.setattr(app, "make_client", lambda: fake)

    resp = app.investigate("any issues?")
    assert resp.report is None  # structuring skipped despite a structured result available
    assert "healthy" in resp.insight


def test_investigate_surfaces_llm_error(monkeypatch) -> None:
    class _Boom(FakeLLMClient):
        def step(self, tools):  # noqa: ANN001
            raise RuntimeError("provider exploded")

    monkeypatch.setattr(app, "make_client", lambda: _Boom([]))
    resp = app.investigate("x")
    assert resp.finish_reason == "error"
    assert "provider exploded" in resp.insight
