"""Tools the LLM can call.

Four tools — small enough that the LLM picks the right one easily, and each
returns *pre-aggregated* results so the model can reason about top-N series
and quantiles instead of drowning in raw samples.

Design notes:
  - Every tool returns a dict that's JSON-serializable. On error we return
    {"error": str, "hint": str} instead of raising, so the LLM can self-correct
    on the next turn.
  - Prometheus range-query results are summarized: top-10 series by sum,
    with p50/p95 baked in when the series is a histogram bucket query.
  - Elasticsearch results are stripped to ECS essentials — full _source per
    hit would overflow the model's context within a couple of turns.
  - Time ranges are enums (not free-form strings) so the LLM can't accidentally
    say "1.5 hours" and break the query.
"""

from __future__ import annotations

import json
import logging
import math
import os
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

# ─── env wiring ─────────────────────────────────────────────────────────────────

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
ELASTICSEARCH_URL = os.environ.get("ELASTICSEARCH_URL", "http://elasticsearch:9200")
METRIC_CATALOG_PATH = os.environ.get("METRIC_CATALOG_PATH", "/app/metric-catalog.md")
ES_LOG_INDEX = "logs-app.ecom-dev*"

# Loose ceilings so a runaway tool doesn't blow context.
MAX_PROMQL_SERIES = 10
MAX_PROMQL_POINTS_PER_SERIES = 10
MAX_LOG_HITS = 50

TIME_RANGE_SECONDS = {
    "5m": 5 * 60,
    "15m": 15 * 60,
    "1h": 60 * 60,
    "24h": 24 * 60 * 60,
}


# ─── catalog ────────────────────────────────────────────────────────────────────

_catalog_cache: str | None = None


def _load_catalog() -> str:
    """Read metric-catalog.md from disk. Cached after first read."""
    global _catalog_cache
    if _catalog_cache is None:
        path = Path(METRIC_CATALOG_PATH)
        if not path.exists():
            return (
                "ERROR: metric-catalog.md not found at "
                f"{METRIC_CATALOG_PATH}. The catalog is mounted by docker-compose; "
                "the AI agent is degraded without it."
            )
        _catalog_cache = path.read_text(encoding="utf-8")
    return _catalog_cache


def get_metric_catalog() -> dict[str, Any]:
    """Tool: return the full metric catalog as a string the LLM can read."""
    catalog = _load_catalog()
    # The catalog is ~14 KB. We return it whole — that's well under our 8 KB
    # tool-result cap once we let the runner truncate (it'll prefer this over
    # truncating a Prometheus query's series labels). Actually we DO want the
    # full catalog; the runner truncates to 16 KB for the catalog tool only.
    return {"catalog": catalog, "characters": len(catalog)}


# ─── Prometheus ─────────────────────────────────────────────────────────────────


def _resolve_window(time_range: str) -> tuple[float, float, int]:
    """Return (start_ts, end_ts, step_seconds) for a query_range call."""
    if time_range not in TIME_RANGE_SECONDS:
        raise ValueError(
            f"time_range must be one of {list(TIME_RANGE_SECONDS)}; got {time_range!r}"
        )
    window = TIME_RANGE_SECONDS[time_range]
    end = datetime.now(timezone.utc).timestamp()
    start = end - window
    # Aim for ~MAX_PROMQL_POINTS_PER_SERIES samples per series, with a
    # 15s floor matching Prometheus scrape interval.
    step = max(15, window // MAX_PROMQL_POINTS_PER_SERIES)
    return start, end, int(step)


def _summarize_series(samples: list[list[Any]]) -> dict[str, Any]:
    """Reduce a Prometheus series to last value + min/max/p50/p95."""
    # samples is [[ts, "value"], ...]
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
    """Tool: run a PromQL range query and return a compact summary.

    Returns up to 10 series sorted by max value, each with last/min/max/mean
    and (if enough points) p50/p95. Raw samples capped at 10 points/series.
    """
    try:
        start, end, step = _resolve_window(time_range)
    except ValueError as e:
        return {"error": str(e), "hint": "use 5m / 15m / 1h / 24h"}

    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.get(
                f"{PROMETHEUS_URL}/api/v1/query_range",
                params={"query": promql, "start": start, "end": end, "step": step},
            )
        if r.status_code != 200:
            return {
                "error": f"prometheus HTTP {r.status_code}",
                "body": r.text[:500],
                "hint": (
                    "Check PromQL syntax. Use get_metric_catalog to confirm metric names. "
                    "Common mistakes: missing histogram_quantile wrapper around _bucket; "
                    "wrong label names; rate() over a gauge."
                ),
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
    result_type = payload["data"]["resultType"]

    series_out: list[dict[str, Any]] = []
    for s in results:
        labels = s.get("metric", {})
        samples = s.get("values", [])
        # Limit samples to MAX_PROMQL_POINTS_PER_SERIES (newest)
        trimmed = samples[-MAX_PROMQL_POINTS_PER_SERIES:]
        summary = _summarize_series(trimmed)
        series_out.append(
            {
                "labels": labels,
                "summary": summary,
                "samples_tail": trimmed,
            }
        )

    # Sort by max value desc; truncate to top N.
    series_out.sort(key=lambda x: x["summary"].get("max", 0.0), reverse=True)
    dropped = max(0, len(series_out) - MAX_PROMQL_SERIES)
    series_out = series_out[:MAX_PROMQL_SERIES]

    result = {
        "query": promql,
        "time_range": time_range,
        "step_seconds": step,
        "result_type": result_type,
        "series_count": len(results),
        "series_returned": len(series_out),
        "series_dropped": dropped,
        "series": series_out,
    }
    if len(results) == 0:
        # Common failure mode: LLM guessed a metric name that doesn't exist
        # (e.g. ecom_auth_attempts_total vs auth_login_attempts_total). Surface
        # this proactively so the next-turn correction is fast.
        result["hint"] = (
            "Zero series matched. Most likely causes: metric name is misspelled "
            "(check get_metric_catalog), labels in {...} don't exist, or there's "
            "genuinely no data in this time range. If unsure about the name, call "
            "get_metric_catalog and grep for the metric."
        )
    return result


# ─── Elasticsearch ──────────────────────────────────────────────────────────────


def _es_filter_time(time_range: str) -> dict[str, Any]:
    if time_range not in TIME_RANGE_SECONDS:
        raise ValueError(
            f"time_range must be one of {list(TIME_RANGE_SECONDS)}; got {time_range!r}"
        )
    return {
        "range": {
            "@timestamp": {
                "gte": f"now-{time_range}",
                "lte": "now",
            }
        }
    }


def _strip_hit(hit: dict[str, Any]) -> dict[str, Any]:
    """Return only the ECS-essential fields. Full _source is too noisy."""
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
    if src.get("ecom"):
        out["ecom"] = src["ecom"]
    trace = src.get("trace") or {}
    if trace.get("id"):
        out["trace.id"] = trace["id"]
    return out


def search_logs(
    query: str = "*",
    time_range: str = "15m",
    size: int = 10,
) -> dict[str, Any]:
    """Tool: search Elasticsearch logs with a Lucene query.

    Returns up to `size` hits stripped to ECS essentials.
    """
    try:
        time_filter = _es_filter_time(time_range)
    except ValueError as e:
        return {"error": str(e), "hint": "use 5m / 15m / 1h / 24h"}

    size = max(1, min(int(size), MAX_LOG_HITS))

    body = {
        "size": size,
        "sort": [{"@timestamp": "desc"}],
        "query": {
            "bool": {
                "filter": [
                    time_filter,
                    {"query_string": {"query": query or "*"}},
                ]
            }
        },
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.post(
                f"{ELASTICSEARCH_URL}/{ES_LOG_INDEX}/_search",
                json=body,
                headers={"content-type": "application/json"},
            )
        if r.status_code != 200:
            return {
                "error": f"elasticsearch HTTP {r.status_code}",
                "body": r.text[:500],
                "hint": (
                    "Check Lucene syntax. Common fields: log.level, url.path, "
                    "ecom.error_code, ecom.payment_status, message, "
                    "http.response.status_code. Use get_metric_catalog for the full list."
                ),
            }
        data = r.json()
    except httpx.HTTPError as e:
        return {"error": f"elasticsearch unreachable: {e}", "hint": "is ES running?"}

    hits_meta = data.get("hits", {})
    total = hits_meta.get("total", {})
    if isinstance(total, dict):
        total_value = total.get("value", 0)
    else:
        total_value = total or 0

    raw_hits = hits_meta.get("hits", [])
    stripped = [_strip_hit(h) for h in raw_hits]

    return {
        "query": query,
        "time_range": time_range,
        "total_matching": total_value,
        "returned": len(stripped),
        "hits": stripped,
    }


def get_recent_errors(route: str, time_range: str = "15m") -> dict[str, Any]:
    """Tool: bucket recent error/warn logs by ecom.error_code for a given route.

    Convenience over search_logs for the common 'what's the error breakdown
    on /api/payment?' question. Returns aggregated counts plus a few sample
    log lines.
    """
    try:
        time_filter = _es_filter_time(time_range)
    except ValueError as e:
        return {"error": str(e), "hint": "use 5m / 15m / 1h / 24h"}

    body = {
        "size": 5,
        "sort": [{"@timestamp": "desc"}],
        "query": {
            "bool": {
                "filter": [
                    time_filter,
                    {"term": {"url.path": route}},
                    {"terms": {"log.level": ["warn", "error"]}},
                ]
            }
        },
        "aggs": {
            "by_error_code": {
                "terms": {
                    "field": "ecom.error_code",
                    "size": 20,
                    "missing": "(none)",
                }
            },
            "by_status": {
                "terms": {
                    "field": "http.response.status_code",
                    "size": 10,
                }
            },
        },
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.post(
                f"{ELASTICSEARCH_URL}/{ES_LOG_INDEX}/_search",
                json=body,
                headers={"content-type": "application/json"},
            )
        if r.status_code != 200:
            return {
                "error": f"elasticsearch HTTP {r.status_code}",
                "body": r.text[:500],
                "hint": "check that `route` matches an exact url.path value, e.g. '/api/payment'",
            }
        data = r.json()
    except httpx.HTTPError as e:
        return {"error": f"elasticsearch unreachable: {e}", "hint": "is ES running?"}

    aggs = data.get("aggregations", {})
    by_code = {
        b["key"]: b["doc_count"] for b in aggs.get("by_error_code", {}).get("buckets", [])
    }
    by_status = {
        b["key"]: b["doc_count"] for b in aggs.get("by_status", {}).get("buckets", [])
    }
    raw_hits = data.get("hits", {}).get("hits", [])
    samples = [_strip_hit(h) for h in raw_hits]

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


# ─── tool schema + registry ─────────────────────────────────────────────────────

# OpenAI-style tool schemas (OpenRouter accepts these directly).
# Descriptions follow the rule: WHEN to use, WHY, and one HINT about the cheapest
# call pattern. The LLM picks by description more than by name.

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
