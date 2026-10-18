"""Minimal unauthenticated health endpoint (no sensitive data) for the bootstrap CLI and UI."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from aeos import SCHEMA_VERSION, __version__
from aeos.core.config import get_settings

router = APIRouter(tags=["system"])


@router.get("/api/health")
def health() -> dict[str, Any]:
    s = get_settings()
    return {"status": "ok", "version": __version__, "schema_version": SCHEMA_VERSION, "environment": s.environment}
