"""Live read of the PUBLIC OpenRouter catalog (no key, no cost). Opt-in: DLY_AEOS_NETWORK_TESTS=1."""

from __future__ import annotations

import os

import httpx
import pytest

from aeos.routing import catalog

pytestmark = pytest.mark.network


@pytest.mark.skipif(os.environ.get("DLY_AEOS_NETWORK_TESTS") != "1", reason="set DLY_AEOS_NETWORK_TESTS=1")
def test_live_public_catalog_shape():
    try:
        with httpx.Client() as c:
            models = catalog.fetch_openrouter_models(c)
    except httpx.HTTPError as exc:
        pytest.skip(f"network unavailable: {exc}")
    assert models and all("id" in m for m in models)
    assert any(m["id"].endswith(":free") for m in models)
