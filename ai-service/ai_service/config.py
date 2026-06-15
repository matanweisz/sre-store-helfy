"""Centralized configuration for the AI observability agent.

Every tunable that used to be a scattered ``os.environ.get(...)`` now lives here
as one validated :class:`Settings` object. The defaults reproduce the original
behavior against the bundled eCommerce stack exactly — but every backend URL,
index pattern, and log field is overridable from the environment, which is what
lets the same agent point at a *different* system without editing source.

Tests construct ``Settings(...)`` directly or call :func:`get_settings` after
``get_settings.cache_clear()`` to pick up monkeypatched env.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, populated from environment variables.

    Field names map case-insensitively to env vars (``openrouter_model`` ←
    ``OPENROUTER_MODEL``), preserving the variable names the stack already uses.
    """

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # ── LLM provider ─────────────────────────────────────────────────────────
    # "openrouter" (default, multi-provider via the OpenAI SDK) or "anthropic"
    # (native SDK — unlocks prompt caching on the system/tools prefix and
    # structured outputs).
    llm_provider: Literal["openrouter", "anthropic"] = "openrouter"

    # ── OpenRouter ───────────────────────────────────────────────────────────
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "anthropic/claude-sonnet-4.6"

    # ── native Anthropic (used when llm_provider="anthropic") ────────────────
    anthropic_api_key: str = ""
    # Mirrors the OpenRouter default (Sonnet 4.6) for parity/cost; override freely.
    anthropic_model: str = "claude-sonnet-4-6"
    anthropic_max_tokens: int = 4096

    # ── agent loop ───────────────────────────────────────────────────────────
    max_agent_iterations: int = 10
    tool_result_cap_chars: int = 8000
    catalog_result_cap_chars: int = 16000
    ai_structured_logs: bool = False
    # When set, one line of domain context is appended to the system prompt — a
    # reuser can orient the agent ("This is a video-encoding pipeline.") without
    # editing prompts.py. The catalog remains the source of signal-level truth.
    system_prompt_domain_hint: str = ""
    # After the agent writes its prose note, run one extra call to also produce a
    # validated IncidentReport. Set false to skip it (prose only).
    structured_output: bool = True

    # ── backends ─────────────────────────────────────────────────────────────
    prometheus_url: str = "http://prometheus:9090"
    elasticsearch_url: str = "http://elasticsearch:9200"
    metric_catalog_path: str = "/app/metric-catalog.md"
    # Index pattern the log-search tools query. The wildcard works whether or not
    # a data stream is created. Point this at your own index to reuse the agent.
    es_log_index: str = "logs-app.ecom-dev*"

    # ── log field map ────────────────────────────────────────────────────────
    # ECS defaults, used by the aggregation/filter tools. Override these to run
    # the agent against logs that use a different schema. ``search_logs`` itself
    # takes a free-form Lucene query, so only the aggregating tools depend on
    # exact field names.
    es_level_field: str = "log.level"
    es_url_path_field: str = "url.path"
    es_status_code_field: str = "http.response.status_code"
    es_error_code_field: str = "ecom.error_code"
    # Domain namespace object lifted verbatim into stripped log hits (the only
    # app-specific part of hit-trimming). Empty string disables it.
    log_domain_namespace: str = "ecom"
    # Levels treated as "errors" by get_recent_errors. JSON list in env, e.g.
    # ERROR_LOG_LEVELS='["warn","error"]'.
    error_log_levels: list[str] = Field(default_factory=lambda: ["warn", "error"])


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton (cached).

    Tests that monkeypatch the environment should call
    ``get_settings.cache_clear()`` first.
    """
    return Settings()
