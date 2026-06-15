"""Provider client translations: OpenRouter and native Anthropic."""

from __future__ import annotations

import json

from _fakes import FakeAnthropicClient, FakeOpenAIClient, a_response, a_text, a_tool_use, assistant, tool_call

from ai_service.config import Settings
from ai_service.llm import AnthropicClient, OpenRouterClient, ToolCall, build_client
from ai_service.report import incident_report_schema
from ai_service.tools import TOOLS

_REPORT_JSON = json.dumps(
    {
        "anomaly": "a",
        "window": "w",
        "supporting_evidence": [],
        "negative_evidence": [],
        "root_cause": "rc",
        "next_action": "na",
        "confidence": "high",
    }
)


# ─── OpenRouter ──────────────────────────────────────────────────────────────


def test_openrouter_step_parses_tool_call_then_terminal() -> None:
    fake = FakeOpenAIClient(
        [tool_call("c1", "query_prometheus", json.dumps({"promql": "up", "time_range": "5m"})), assistant("done")]
    )
    c = OpenRouterClient(Settings(openrouter_api_key="x"), client=fake)
    c.start("sys", "q")

    turn = c.step(TOOLS)
    assert turn.tool_calls == [ToolCall("c1", "query_prometheus", {"promql": "up", "time_range": "5m"})]
    c.add_tool_result("c1", "{}")

    turn2 = c.step(TOOLS)
    assert turn2.text == "done" and not turn2.tool_calls
    # The tool schema was forwarded as-is (OpenAI function shape).
    assert fake.calls[0]["tools"] == TOOLS


def test_openrouter_structure_valid_and_garbage() -> None:
    ok = OpenRouterClient(Settings(openrouter_api_key="x"), client=FakeOpenAIClient([assistant(_REPORT_JSON)]))
    out = ok.structure("prose", incident_report_schema())
    assert out is not None and out["anomaly"] == "a"

    bad = OpenRouterClient(
        Settings(openrouter_api_key="x"), client=FakeOpenAIClient([assistant("nope"), assistant("still nope")])
    )
    assert bad.structure("prose", incident_report_schema()) is None


# ─── Anthropic ───────────────────────────────────────────────────────────────


def test_anthropic_step_caches_prefix_and_translates_tools() -> None:
    resp = a_response(a_text("thinking"), a_tool_use("t1", "query_prometheus", {"promql": "up", "time_range": "5m"}))
    fake = FakeAnthropicClient([resp])
    c = AnthropicClient(Settings(anthropic_api_key="x"), client=fake)
    c.start("sys", "q")

    # prompt caching: a cache_control breakpoint sits on the system prefix.
    assert c._system[0]["cache_control"] == {"type": "ephemeral"}

    turn = c.step(TOOLS)
    assert turn.text == "thinking"
    assert turn.tool_calls[0] == ToolCall("t1", "query_prometheus", {"promql": "up", "time_range": "5m"})

    kwargs = fake.calls[0]
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    # tools translated OpenAI→Anthropic shape (input_schema, not parameters).
    assert "input_schema" in kwargs["tools"][0] and "parameters" not in kwargs["tools"][0]
    assert kwargs["tools"][0]["name"] == "get_metric_catalog"


def test_anthropic_add_tool_result_shape() -> None:
    c = AnthropicClient(Settings(anthropic_api_key="x"), client=FakeAnthropicClient([]))
    c.start("sys", "q")
    c.add_tool_result("t1", "RESULT")
    last = c._messages[-1]
    assert last["role"] == "user"
    assert last["content"][0] == {"type": "tool_result", "tool_use_id": "t1", "content": "RESULT"}


def test_anthropic_structure_parses_text_block() -> None:
    fake = FakeAnthropicClient([a_response(a_text(_REPORT_JSON))])
    c = AnthropicClient(Settings(anthropic_api_key="x"), client=fake)
    out = c.structure("prose", incident_report_schema())
    assert out is not None and out["confidence"] == "high"


# ─── factory ─────────────────────────────────────────────────────────────────


def test_build_client_selects_provider() -> None:
    assert isinstance(build_client(Settings(llm_provider="openrouter", openrouter_api_key="x")), OpenRouterClient)
    assert isinstance(build_client(Settings(llm_provider="anthropic", anthropic_api_key="x")), AnthropicClient)
