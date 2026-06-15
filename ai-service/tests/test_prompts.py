"""The system prompt is domain-agnostic; the domain hint is appended when set."""

from __future__ import annotations

from ai_service.config import get_settings
from ai_service.prompts import SRE_SYSTEM_PROMPT, build_system_prompt


def test_system_prompt_is_domain_agnostic() -> None:
    low = SRE_SYSTEM_PROMPT.lower()
    for term in ("ecommerce", "e-commerce", "mock-stripe", "ecom_payments", "ecom.error_code"):
        assert term not in low, f"leaked eCommerce-specific term: {term}"
    for step in ("HYPOTHESIZE", "CONFIRM", "NARROW", "NEGATIVE EVIDENCE", "CONCLUDE"):
        assert step in SRE_SYSTEM_PROMPT


def test_build_system_prompt_default_equals_base() -> None:
    assert build_system_prompt() == SRE_SYSTEM_PROMPT


def test_build_system_prompt_appends_domain_hint(monkeypatch) -> None:
    monkeypatch.setenv("SYSTEM_PROMPT_DOMAIN_HINT", "This is a video-encoding pipeline.")
    get_settings.cache_clear()
    out = build_system_prompt()
    assert out.startswith(SRE_SYSTEM_PROMPT)
    assert "# Domain context" in out
    assert "video-encoding pipeline" in out
