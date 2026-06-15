"""Config defaults reproduce the bundled stack; env vars override everything."""

from __future__ import annotations

from ai_service.config import Settings, get_settings


def test_defaults_reproduce_ecommerce_stack() -> None:
    s = Settings()
    assert s.es_log_index == "logs-app.ecom-dev*"
    assert s.es_error_code_field == "ecom.error_code"
    assert s.es_url_path_field == "url.path"
    assert s.es_level_field == "log.level"
    assert s.log_domain_namespace == "ecom"
    assert s.error_log_levels == ["warn", "error"]
    assert s.openrouter_model == "anthropic/claude-sonnet-4.6"
    assert s.max_agent_iterations == 10
    assert s.prometheus_url == "http://prometheus:9090"


def test_env_overrides_decouple_from_ecommerce(monkeypatch) -> None:
    monkeypatch.setenv("ES_LOG_INDEX", "logs-myapp*")
    monkeypatch.setenv("ES_ERROR_CODE_FIELD", "app.err_code")
    monkeypatch.setenv("LOG_DOMAIN_NAMESPACE", "app")
    monkeypatch.setenv("MAX_AGENT_ITERATIONS", "3")
    monkeypatch.setenv("OPENROUTER_MODEL", "anthropic/claude-opus-4-8")
    get_settings.cache_clear()

    s = get_settings()
    assert s.es_log_index == "logs-myapp*"
    assert s.es_error_code_field == "app.err_code"
    assert s.log_domain_namespace == "app"
    assert s.max_agent_iterations == 3
    assert s.openrouter_model == "anthropic/claude-opus-4-8"


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()
