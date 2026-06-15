"""Unit tests for the four agent tools, with Prometheus/ES HTTP mocked.

These also lock in the decoupling: get_recent_errors builds its ES query from
configured field names, so the same agent can run against a non-eCommerce log
schema by setting env vars alone.
"""

from __future__ import annotations

import json

import httpx
import respx

from ai_service import tools
from ai_service.config import Settings, get_settings

PROM_RANGE = r".*/api/v1/query_range"
ES_SEARCH = r".*/_search"


# ─── query_prometheus ────────────────────────────────────────────────────────


@respx.mock
def test_query_prometheus_summarizes_series() -> None:
    payload = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {"metric": {"route": "/api/payment"}, "values": [[1, "0.40"], [2, "0.42"]]}
            ],
        },
    }
    respx.get(url__regex=PROM_RANGE).mock(return_value=httpx.Response(200, json=payload))

    out = tools.query_prometheus("sum(rate(ecom_payments_total[1m]))", "15m")
    assert out["series_count"] == 1
    series = out["series"][0]
    assert series["labels"]["route"] == "/api/payment"
    assert series["summary"]["last"] == 0.42
    assert series["summary"]["max"] == 0.42
    assert series["summary"]["min"] == 0.40


@respx.mock
def test_query_prometheus_zero_series_returns_hint() -> None:
    payload = {"status": "success", "data": {"resultType": "matrix", "result": []}}
    respx.get(url__regex=PROM_RANGE).mock(return_value=httpx.Response(200, json=payload))

    out = tools.query_prometheus("ecom_auth_attempts_total", "15m")
    assert out["series_count"] == 0
    assert "get_metric_catalog" in out["hint"]


def test_query_prometheus_rejects_bad_time_range() -> None:
    out = tools.query_prometheus("up", "7m")
    assert out["error"].startswith("time_range must be one of")
    assert out["hint"] == "use 5m / 15m / 1h / 24h"


@respx.mock
def test_query_prometheus_surfaces_http_error() -> None:
    respx.get(url__regex=PROM_RANGE).mock(return_value=httpx.Response(400, text="parse error"))
    out = tools.query_prometheus("rate(", "15m")
    assert "prometheus HTTP 400" in out["error"]


# ─── search_logs ─────────────────────────────────────────────────────────────


@respx.mock
def test_search_logs_strips_hits_to_essentials() -> None:
    es_payload = {
        "hits": {
            "total": {"value": 2},
            "hits": [
                {
                    "_source": {
                        "@timestamp": "2026-06-15T10:00:00Z",
                        "log": {"level": "error"},
                        "message": "payment declined",
                        "url": {"path": "/api/payment"},
                        "http": {"request": {"method": "POST"}, "response": {"status_code": 402}},
                        "event": {"duration": 312_000_000, "outcome": "failure"},
                        "ecom": {"error_code": "payment_declined"},
                        "trace": {"id": "abc-123"},
                    }
                }
            ],
        }
    }
    respx.post(url__regex=ES_SEARCH).mock(return_value=httpx.Response(200, json=es_payload))

    out = tools.search_logs("log.level:error", "15m", 10)
    assert out["total_matching"] == 2
    hit = out["hits"][0]
    assert hit["level"] == "error"
    assert hit["url.path"] == "/api/payment"
    assert hit["http.response.status_code"] == 402
    assert hit["event.duration_ms"] == 312.0
    assert hit["ecom"] == {"error_code": "payment_declined"}
    assert hit["trace.id"] == "abc-123"


# ─── get_recent_errors (decoupling proof) ────────────────────────────────────


def _capture_es_body(captured: dict) -> "respx.Route":
    def responder(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "hits": {"total": {"value": 3}, "hits": []},
                "aggregations": {
                    "by_error_code": {"buckets": [{"key": "payment_declined", "doc_count": 3}]},
                    "by_status": {"buckets": [{"key": 402, "doc_count": 3}]},
                },
            },
        )

    return respx.post(url__regex=ES_SEARCH).mock(side_effect=responder)


@respx.mock
def test_get_recent_errors_parses_and_uses_default_fields() -> None:
    captured: dict = {}
    _capture_es_body(captured)

    out = tools.get_recent_errors("/api/payment", "15m")
    assert out["by_error_code"] == {"payment_declined": 3}
    assert out["by_status_code"] == {402: 3}

    body = captured["body"]
    assert body["aggs"]["by_error_code"]["terms"]["field"] == "ecom.error_code"
    assert body["aggs"]["by_status"]["terms"]["field"] == "http.response.status_code"
    filters = body["query"]["bool"]["filter"]
    assert {"term": {"url.path": "/api/payment"}} in filters
    assert {"terms": {"log.level": ["warn", "error"]}} in filters


@respx.mock
def test_get_recent_errors_respects_field_overrides(monkeypatch) -> None:
    monkeypatch.setenv("ES_ERROR_CODE_FIELD", "app.err_code")
    monkeypatch.setenv("ES_URL_PATH_FIELD", "http.target")
    monkeypatch.setenv("ES_LEVEL_FIELD", "severity")
    monkeypatch.setenv("ERROR_LOG_LEVELS", '["warning", "error", "critical"]')
    get_settings.cache_clear()

    captured: dict = {}
    _capture_es_body(captured)

    tools.get_recent_errors("/v2/orders", "15m")
    body = captured["body"]
    assert body["aggs"]["by_error_code"]["terms"]["field"] == "app.err_code"
    filters = body["query"]["bool"]["filter"]
    assert {"term": {"http.target": "/v2/orders"}} in filters
    assert {"terms": {"severity": ["warning", "error", "critical"]}} in filters


# ─── _strip_hit + catalog ────────────────────────────────────────────────────


def test_strip_hit_uses_configured_domain_namespace() -> None:
    s = Settings(log_domain_namespace="app")
    hit = {"_source": {"@timestamp": "t", "app": {"error_code": "x"}, "ecom": {"y": 1}}}
    out = tools._strip_hit(hit, s)
    assert out["app"] == {"error_code": "x"}
    assert "ecom" not in out


def test_get_metric_catalog_reads_file(tmp_path, monkeypatch) -> None:
    catalog = tmp_path / "catalog.md"
    catalog.write_text("# Catalog\nhttp_requests_total — normal range ...")
    monkeypatch.setenv("METRIC_CATALOG_PATH", str(catalog))
    get_settings.cache_clear()
    tools._catalog_cache = None

    out = tools.get_metric_catalog()
    assert "http_requests_total" in out["catalog"]
    assert out["characters"] > 0
