"""MCP server exposing the four observability tools.

Any MCP-capable agent (Claude Desktop, Cursor, another project) can spawn this
server and investigate the same Prometheus + Elasticsearch backends the FastAPI
agent uses. The registered tools ARE the functions from ``tools.py`` — no
duplication — so the catalog, the config-driven field map, and the behavior are
identical across both surfaces.

Run:
    obs-mcp                                   # stdio (what an MCP client spawns)
    MCP_TRANSPORT=streamable-http obs-mcp     # networked HTTP transport
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from . import tools

# host/port only matter for the networked transports; ignored for stdio.
mcp = FastMCP(
    "obs-investigator",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8001")),
)

# Reuse the exact descriptions the FastAPI agent advertises, so both surfaces
# present the tools identically (single source of truth in tools.TOOLS).
_DESCRIPTIONS = {t["function"]["name"]: t["function"]["description"] for t in tools.TOOLS}

for _fn in (tools.get_metric_catalog, tools.query_prometheus, tools.search_logs, tools.get_recent_errors):
    mcp.add_tool(_fn, description=_DESCRIPTIONS[_fn.__name__])


def main() -> None:
    """Console entry point. Transport defaults to stdio (MCP clients spawn it)."""
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)  # "stdio" | "sse" | "streamable-http"


if __name__ == "__main__":
    main()
