# MCP server — reuse the investigation tools from any agent

The four observability tools (`get_metric_catalog`, `query_prometheus`,
`search_logs`, `get_recent_errors`) are also exposed over the
[Model Context Protocol](https://modelcontextprotocol.io). Any MCP-capable agent
— Claude Desktop, Cursor, or your own project — can point at this stack and run
the same investigations the built-in agent does. The MCP tools **are** the
functions in `ai_service/tools.py` (no duplication), so the config-driven field
map and catalog behave identically across both surfaces.

## Local agents (stdio) — the common case

Claude Desktop / Cursor spawn the server as a subprocess over stdio. Install the
package, then point your client at the `obs-mcp` entry point.

```bash
cd ai-service && pip install -e .
```

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "obs-investigator": {
      "command": "obs-mcp",
      "env": {
        "PROMETHEUS_URL": "http://localhost:9090",
        "ELASTICSEARCH_URL": "http://localhost:9200",
        "METRIC_CATALOG_PATH": "/absolute/path/to/repo/metric-catalog.md"
      }
    }
  }
}
```

The URLs point at the host-exposed ports from `docker compose up` (Prometheus
`:9090`, Elasticsearch `:9200`). To investigate a *different* system, change the
URLs, `ES_LOG_INDEX`, and the field-map env vars (see `ai_service/config.py`) —
no code changes.

**Cursor** (`.cursor/mcp.json`) uses the same shape under an `mcpServers` key.

If `obs-mcp` isn't on your client's PATH, use the module form instead:
`"command": "python", "args": ["-m", "ai_service.mcp_server"]`.

## Networked (streamable-HTTP) — optional

For a long-running networked server (e.g. shared by several agents), start the
profile-gated compose service:

```bash
docker compose --profile mcp up -d ai-mcp   # serves http://localhost:8001/mcp
```

It reuses the same image and the same catalog mount as the FastAPI agent.

## Verifying

The MCP surface is covered by `ai-service/tests/test_mcp.py`: it asserts all four
tools register with the right schemas and that calling a tool runs the real
underlying function (backends mocked). Run `pytest` from `ai-service/`.
