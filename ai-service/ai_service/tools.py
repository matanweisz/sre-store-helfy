"""Tools the LLM can call against Prometheus and Elasticsearch.

All tools return JSON-serializable dicts. Errors come back as
`{"error": str, "hint": str}` (never raised) so the agent can self-correct on
the next turn instead of crashing the loop.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .config import Settings, get_settings

MAX_PROMQL_SERIES = 10
MAX_PROMQL_POINTS_PER_SERIES = 10
MAX_LOG_HITS = 50

TIME_RANGE_SECONDS = {
    "5m": 5 * 60,
    "15m": 15 * 60,
    "1h": 60 * 60,
    "24h": 24 * 60 * 60,
}

# ─── catalog ───────────────────────────────────────────────────────────────────

_catalog_cache: str | None = None


def _load_catalog() -> str:
    global _catalog_cache
    if _catalog_cache is None:
        catalog_path = get_settings().metric_catalog_path
        path = Path(catalog_path)
        if not path.exists():
            return (
                f"ERROR: metric-catalog.md not found at {catalog_path}. "
                "The catalog is mounted by docker-compose; the agent is degraded without it."
            )
        _catalog_cache = path.read_text(encoding="utf-8")
    return _catalog_cache


def get_metric_catalog() -> dict[str, Any]:
    catalog = _load_catalog()
    return {"catalog": catalog, "characters": len(catalog)}


# ─── Prometheus ────────────────────────────────────────────────────────────────


def _resolve_window(time_range: str) -> tuple[float, float, int]:
    if time_range not in TIME_RANGE_SECONDS:
        raise ValueError(f"time_range must be one of {list(TIME_RANGE_SECONDS)}; got {time_range!r}")
    window = TIME_RANGE_SECONDS[time_range]
    end = datetime.now(timezone.utc).timestamp()
    start = end - window
    # 15 s floor matches the scrape interval.
    step = max(15, window // MAX_PROMQL_POINTS_PER_SERIES)
    return start, end, int(step)


def _summarize_series(samples: list[list[Any]]) -> dict[str, Any]:
    values = []
    for _ts, v in samples:
        try:
            num = float(v)
            if math.isfinite(num):
                values.append(num)
        except (TypeError, ValueError):
            continue
    if not values:
        return {"points": 0}
    summary: dict[str, Any] = {
        "points": len(values),
        "last": values[-1],
        "min": min(values),
        "max": max(values),
        "mean": round(statistics.fmean(values), 6),
    }
    if len(values) >= 2:
        try:
            summary["p50"] = round(statistics.median(values), 6)
            summary["p95"] = round(
                statistics.quantiles(values, n=20)[-1] if len(values) >= 20 else max(values),
                6,
            )
        except statistics.StatisticsError:
            pass
    return summary


def query_prometheus(promql: str, time_range: str = "15m") -> dict[str, Any]:
    try:
        start, end, step = _resolve_window(time_range)
    except ValueError as e:
        return {"error": str(e), "hint": "use 5m / 15m / 1h / 24h"}

    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.get(
                f"{get_settings().prometheus_url}/api/v1/query_range",
                params={"query": promql, "start": start, "end": end, "step": step},
            )
        if r.status_code != 200:
            return {
                "error": f"prometheus HTTP {r.status_code}",
                "body": r.text[:500],
                "hint": "check PromQL syntax; use get_metric_catalog for metric names",
            }
        payload = r.json()
    except httpx.HTTPError as e:
        return {"error": f"prometheus unreachable: {e}", "hint": "is Prometheus running?"}

    if payload.get("status") != "success":
        return {
            "error": payload.get("error", "unknown"),
            "error_type": payload.get("errorType"),
            "hint": "PromQL parse or eval error — re-check the expression",
        }

    results = payload["data"]["result"]
    series_out: list[dict[str, Any]] = []
    for s in results:
        trimmed = s.get("values", [])[-MAX_PROMQL_POINTS_PER_SERIES:]
        series_out.append({
            "labels": s.get("metric", {}),
            "summary": _summarize_series(trimmed),
            "samples_tail": trimmed,
        })

    series_out.sort(key=lambda x: x["summary"].get("max", 0.0), reverse=True)
    dropped = max(0, len(series_out) - MAX_PROMQL_SERIES)
    series_out = series_out[:MAX_PROMQL_SERIES]

    result = {
        "query": promql,
        "time_range": time_range,
        "step_seconds": step,
        "result_type": payload["data"]["resultType"],
        "series_count": len(results),
        "series_returned": len(series_out),
        "series_dropped": dropped,
        "series": series_out,
    }
    if len(results) == 0:
        # Common failure mode: the LLM guessed a metric name that doesn't exist.
        # Surface this proactively so the next-turn correction is fast.
        result["hint"] = (
            "Zero series matched. Most likely a misspelled metric name "
            "(check get_metric_catalog), nonexistent labels, or no data in this range."
        )
    return result


# ─── Elasticsearch ─────────────────────────────────────────────────────────────


def _es_filter_time(time_range: str) -> dict[str, Any]:
    if time_range not in TIME_RANGE_SECONDS:
        raise ValueError(f"time_range must be one of {list(TIME_RANGE_SECONDS)}; got {time_range!r}")
    return {"range": {"@timestamp": {"gte": f"now-{time_range}", "lte": "now"}}}


def _strip_hit(hit: dict[str, Any], settings: Settings | None = None) -> dict[str, Any]:
    """Trim a hit to ECS essentials. Full _source overflows context fast.

    The ECS field reads below are standard; only the domain namespace (``ecom``
    by default) is app-specific and is sourced from config so the agent can be
    pointed at logs that carry a different business namespace (or none).
    """
    settings = settings or get_settings()
    src = hit.get("_source", {})
    out: dict[str, Any] = {
        "@timestamp": src.get("@timestamp"),
        "level": (src.get("log") or {}).get("level"),
        "message": src.get("message"),
    }
    url = src.get("url") or {}
    if url.get("path"):
        out["url.path"] = url["path"]
    http = src.get("http") or {}
    method = (http.get("request") or {}).get("method")
    status = (http.get("response") or {}).get("status_code")
    if method:
        out["http.request.method"] = method
    if status is not None:
        out["http.response.status_code"] = status
    event = src.get("event") or {}
    if event.get("duration") is not None:
        out["event.duration_ms"] = round(event["duration"] / 1_000_000, 3)
    if event.get("outcome"):
        out["event.outcome"] = event["outcome"]
    namespace = settings.log_domain_namespace
    if namespace and src.get(namespace):
        out[namespace] = src[namespace]
    trace = src.get("trace") or {}
    if trace.get("id"):
        out["trace.id"] = trace["id"]
    return out


def search_logs(query: str = "*", time_range: str = "15m", size: int = 10) -> dict[str, Any]:
    try:
        time_filter = _es_filter_time(time_range)
    except ValueError as e:
        return {"error": str(e), "hint": "use 5m / 15m / 1h / 24h"}

    s = get_settings()
    size = max(1, min(int(size), MAX_LOG_HITS))
    body = {
        "size": size,
        "sort": [{"@timestamp": "desc"}],
        "query": {"bool": {"filter": [time_filter, {"query_string": {"query": query or "*"}}]}},
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.post(
                f"{s.elasticsearch_url}/{s.es_log_index}/_search",
                json=body,
                headers={"content-type": "application/json"},
            )
        if r.status_code != 200:
            return {
                "error": f"elasticsearch HTTP {r.status_code}",
                "body": r.text[:500],
                "hint": "check Lucene syntax; use ECS fields like log.level, url.path, ecom.error_code",
            }
        data = r.json()
    except httpx.HTTPError as e:
        return {"error": f"elasticsearch unreachable: {e}", "hint": "is ES running?"}

    hits_meta = data.get("hits", {})
    total = hits_meta.get("total", {})
    total_value = total.get("value", 0) if isinstance(total, dict) else (total or 0)
    stripped = [_strip_hit(h, s) for h in hits_meta.get("hits", [])]

    return {
        "query": query,
        "time_range": time_range,
        "total_matching": total_value,
        "returned": len(stripped),
        "hits": stripped,
    }


def get_recent_errors(route: str, time_range: str = "15m") -> dict[str, Any]:
    try:
        time_filter = _es_filter_time(time_range)
    except ValueError as e:
        return {"error": str(e), "hint": "use 5m / 15m / 1h / 24h"}

    s = get_settings()
    body = {
        "size": 5,
        "sort": [{"@timestamp": "desc"}],
        "query": {
            "bool": {
                "filter": [
                    time_filter,
                    {"term": {s.es_url_path_field: route}},
                    {"terms": {s.es_level_field: s.error_log_levels}},
                ]
            }
        },
        "aggs": {
            "by_error_code": {"terms": {"field": s.es_error_code_field, "size": 20, "missing": "(none)"}},
            "by_status": {"terms": {"field": s.es_status_code_field, "size": 10}},
        },
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.post(
                f"{s.elasticsearch_url}/{s.es_log_index}/_search",
                json=body,
                headers={"content-type": "application/json"},
            )
        if r.status_code != 200:
            return {
                "error": f"elasticsearch HTTP {r.status_code}",
                "body": r.text[:500],
                "hint": "`route` must be an exact url.path value, e.g. '/api/payment'",
            }
        data = r.json()
    except httpx.HTTPError as e:
        return {"error": f"elasticsearch unreachable: {e}", "hint": "is ES running?"}

    aggs = data.get("aggregations", {})
    by_code = {b["key"]: b["doc_count"] for b in aggs.get("by_error_code", {}).get("buckets", [])}
    by_status = {b["key"]: b["doc_count"] for b in aggs.get("by_status", {}).get("buckets", [])}
    samples = [_strip_hit(h, s) for h in data.get("hits", {}).get("hits", [])]

    total = data.get("hits", {}).get("total", {})
    total_value = total.get("value", 0) if isinstance(total, dict) else (total or 0)

    return {
        "route": route,
        "time_range": time_range,
        "total_error_warn_logs": total_value,
        "by_error_code": by_code,
        "by_status_code": by_status,
        "sample_lines": samples,
    }


# ─── tool schema + registry ────────────────────────────────────────────────────
# These descriptions are what the LLM reads to pick a tool. Keep them precise.

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_metric_catalog",
            "description": (
                "Return the full metric and log-field catalog for this service. "
                "Call FIRST if you are not sure which metric names or log fields exist. "
                "The catalog documents what 'normal' looks like for each signal — use those "
                "baselines to compare against your observations."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_prometheus",
            "description": (
                "Run a PromQL range query and get back a summary of up to 10 top series "
                "(sorted by max value). Each series includes last/min/max/mean and p50/p95 "
                "when enough samples exist. Use for ALL numeric questions: rates, latencies, "
                "histograms, ratios. Common patterns: "
                "`sum by (route)(rate(http_requests_total[1m]))`, "
                "`histogram_quantile(0.95, sum by (route, le)(rate(http_request_duration_seconds_bucket[5m])))`, "
                "`sum(rate(ecom_payments_total{outcome=\"failed\"}[5m])) / sum(rate(ecom_payments_total[5m]))`."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "promql": {
                        "type": "string",
                        "description": "A complete PromQL expression. Wrap counters in rate() and histogram buckets in histogram_quantile().",
                    },
                    "time_range": {
                        "type": "string",
                        "enum": ["5m", "15m", "1h", "24h"],
                        "description": "How far back to query. Default 15m.",
                    },
                },
                "required": ["promql"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_logs",
            "description": (
                "Search the Elasticsearch log stream with a Lucene query. Use to find specific "
                "example events that EXPLAIN a metric anomaly — never to count events (use "
                "Prometheus counters for counts). ECS field paths only. Common queries: "
                "`log.level:error`, "
                "`url.path:\"/api/payment\" AND log.level:warn`, "
                "`ecom.error_code:payment_declined`."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Lucene query string. Use ECS field paths (log.level, url.path, ecom.error_code, message, ...).",
                    },
                    "time_range": {
                        "type": "string",
                        "enum": ["5m", "15m", "1h", "24h"],
                        "description": "Default 15m.",
                    },
                    "size": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "description": "Max log lines to return. Default 10; raise to 50 only when sampling broadly.",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_errors",
            "description": (
                "Convenience wrapper: aggregate recent warn+error logs for ONE specific route, "
                "bucketed by ecom.error_code and http.response.status_code. Use when you've "
                "confirmed a route has elevated errors and want the breakdown by reason. "
                "Returns counts AND a few sample log lines. Cheaper than two separate "
                "search_logs calls."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "route": {
                        "type": "string",
                        "description": "Exact url.path value, e.g. '/api/payment', '/api/checkout', '/api/auth/login'. NOT a template like '/api/products/:id'.",
                    },
                    "time_range": {
                        "type": "string",
                        "enum": ["5m", "15m", "1h", "24h"],
                        "description": "Default 15m.",
                    },
                },
                "required": ["route"],
                "additionalProperties": False,
            },
        },
    },
]


TOOL_REGISTRY: dict[str, Any] = {
    "get_metric_catalog": get_metric_catalog,
    "query_prometheus": query_prometheus,
    "search_logs": search_logs,
    "get_recent_errors": get_recent_errors,
}
