"""Minimal unauthenticated health endpoint (no sensitive data) for the bootstrap CLI and UI.

``controller`` says which code this process runs (§09 zones): ``release`` = an extracted, verified release
under the data dir; ``checkout`` = the editable source tree. Release id and commit are not sensitive.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from fastapi import APIRouter

from aeos import SCHEMA_VERSION, __version__
from aeos.core.config import Settings, get_settings

router = APIRouter(tags=["system"])


@lru_cache(maxsize=4)
def _identity(data_dir: str) -> dict[str, Any] | None:
    # The code a process runs does not change while it runs: computed once per process.
    try:
        from aeos.operations.release import code_identity

        return code_identity(Settings(data_dir=data_dir))
    except Exception:
        return None


def controller_status(settings: Settings) -> dict[str, Any]:
    identity = _identity(str(settings.data_dir))
    if identity is None:  # operations module absent/broken: say so, never claim a release
        return {"mode": "UNKNOWN", "status": "UNKNOWN"}
    from aeos.operations.release import running_controller

    info = running_controller(settings, identity=identity)  # ACTIVE pointer re-read on every call
    return {k: info.get(k) for k in ("mode", "release_id", "commit", "dirty", "active_release_id",
                                     "matches_active", "status")}


@router.get("/api/health")
def health() -> dict[str, Any]:
    s = get_settings()
    return {"status": "ok", "version": __version__, "schema_version": SCHEMA_VERSION, "environment": s.environment,
            "controller": controller_status(s)}
