"""The MCP server exposes the same four tools, backed by the same functions."""

from __future__ import annotations

import asyncio
import json

import httpx
import respx

from ai_service import mcp_server


def _run(coro):
    return asyncio.run(coro)


def test_all_four_tools_registered() -> None:
    tools = _run(mcp_server.mcp.list_tools())
    names = {t.name for t in tools}
    assert names == {"get_metric_catalog", "query_prometheus", "search_logs", "get_recent_errors"}

    qp = next(t for t in tools if t.name == "query_prometheus")
    assert set(qp.inputSchema["properties"]) == {"promql", "time_range"}
    # description is reused from tools.TOOLS (single source of truth).
    assert "PromQL" in qp.description


@respx.mock
def test_call_tool_executes_underlying_function() -> None:
    payload = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [{"metric": {"route": "/api/payment"}, "values": [[1, "0.40"], [2, "0.42"]]}],
        },
    }
    respx.get(url__regex=r".*/api/v1/query_range").mock(return_value=httpx.Response(200, json=payload))

    result = _run(
        mcp_server.mcp.call_tool("query_prometheus", {"promql": "sum(rate(x[1m]))", "time_range": "15m"})
    )
    content = result[0] if isinstance(result, tuple) else result
    parsed = json.loads(content[0].text)
    assert parsed["series_count"] == 1
    assert parsed["series"][0]["labels"]["route"] == "/api/payment"
