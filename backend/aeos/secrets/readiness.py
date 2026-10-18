"""Readiness invalidation for secret writes (§08 "token scope ... berubah memicu invalidation", §50).

A stored or deleted credential changes what the Doctor observed about it, so every check that declares
``secret:<ref>`` (or ``secret:*``) is marked STALE and the capabilities that read those checks stop being
READY until a recheck. Values never reach this module — only the reference name.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("aeos.secrets")


def invalidate_secret_checks(ref: str, *, reason: str, session: Any | None = None) -> int:
    """Mark the Doctor checks that depend on ``secret:<ref>`` STALE. Returns how many were marked.

    With ``session`` the change joins the caller's transaction (no commit). Without one a short own
    transaction is used, but only when the local database already exists (never created as a side effect).
    A failure is logged and never undoes the secret write (the store is the source of truth)."""
    try:
        from aeos.doctor import facade as doctor
    except (ImportError, ModuleNotFoundError):
        return 0
    invalidator = f"secret:{ref}"
    # The reference is carried by the invalidator label only, so the Doctor can withhold it from rows that
    # are not scoped to the reference's domain (an office/* ref never appears in a platform-level row).
    why = reason[:200]
    if session is not None:
        return doctor.invalidate(session, invalidator=invalidator, reason=why)
    try:
        from aeos.core.config import get_settings
        from aeos.core.db import session_scope

        if not get_settings().layout.db_path.is_file():
            return 0
        with session_scope() as s:
            return doctor.invalidate(s, invalidator=invalidator, reason=why)
    except Exception as exc:  # pragma: no cover - logged; readiness also expires by TTL
        log.warning("readiness invalidation for %s failed: %s", ref, type(exc).__name__)
        return 0
