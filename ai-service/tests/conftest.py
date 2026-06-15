"""Shared fixtures: reset cached state between tests so env overrides apply."""

from __future__ import annotations

import pytest

from ai_service import tools
from ai_service.config import get_settings


@pytest.fixture(autouse=True)
def _reset_cached_state() -> None:
    """The settings singleton and the catalog string are process-cached for
    speed in production; clear both around every test so monkeypatched env and
    temp catalog files take effect."""
    get_settings.cache_clear()
    tools._catalog_cache = None
    yield
    get_settings.cache_clear()
    tools._catalog_cache = None
