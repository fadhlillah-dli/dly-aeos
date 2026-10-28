"""Readiness invalidation for secret writes (§08 "token scope ... berubah memicu invalidation", §50).

A stored or deleted credential changes what the Doctor observed about it, so every check that declares
``secret:<ref>`` (or ``secret:*``) is marked STALE and the capabilities that read those checks stop being
READY until a recheck. A change of the store itself (OpenBao started, stopped, unsealed, initialised) signals
``secret:store`` (``store_state_changed``), the one signal every check that depends on whether the store answers
declares. Values never reach this module — only the reference name.

Each signal is also the single owner of one dashboard event (§35 "every state change the dashboard shows"),
emitted once per action in the same transaction as the invalidation (source ``secrets``):
``secret.presence_changed`` (payload: the reference name only; an ``office/`` name only into the OFFICE domain, a
``projects/`` name never, as the Doctor's own rows) and ``secret.store_changed`` (payload: the transition).
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("aeos.secrets")

EVENT_SOURCE = "secrets"
PRESENCE_EVENT = "secret.presence_changed"
STORE_EVENT = "secret.store_changed"
# The state transitions ``store_state_changed`` reports (the payload of ``secret.store_changed``).
STORE_TRANSITIONS = frozenset({"started", "stopped", "unsealed", "initialised"})


def invalidate_secret_checks(ref: str, *, reason: str, session: Any | None = None) -> int:
    """Mark the Doctor checks that depend on ``secret:<ref>`` STALE and emit ``secret.presence_changed``.
    Returns how many checks were marked.

    With ``session`` the change joins the caller's transaction (no commit). Without one a short own
    transaction is used, but only when the local database already exists (never created as a side effect).
    A failure is logged and never undoes the secret write (the store is the source of truth)."""
    # The reference is carried by the invalidator label only, so the Doctor can withhold it from rows that
    # are not scoped to the reference's domain (an office/* ref never appears in a platform-level row). The event
    # follows the same rule: a private name travels only into its owning domain, otherwise it is withheld.
    private, owner = _ref_scope(ref)
    quote = not private or owner is not None
    event = {"kind": PRESENCE_EVENT, "entity_type": "secret_ref", "entity_id": ref if quote else "",
             "domain": owner if private else None, "payload": {"ref": ref if quote else None}}
    return _signal(f"secret:{ref}", reason=reason, session=session, event=event)


# Declared by every check whose result depends on whether the secret store answers (secret.status, the route checks).
STORE_INVALIDATOR = "secret:store"


def store_state_changed(reason: str, *, transition: str, session: Any | None = None) -> int:
    """The secret store itself changed state (``transition``: one of ``STORE_TRANSITIONS``): mark the checks that
    declare ``secret:store`` STALE, so a result taken while the store was down is not kept until its TTL, and emit
    ``secret.store_changed``. Same transaction and failure rules as ``invalidate_secret_checks``: without ``session``
    it never raises (the vault operation already happened)."""
    event = {"kind": STORE_EVENT, "entity_type": "secret_store", "entity_id": "store", "domain": None,
             "payload": {"transition": transition}}
    return _signal(STORE_INVALIDATOR, reason=reason, session=session, event=event)


def _ref_scope(ref: str) -> tuple[bool, str | None]:
    try:
        from aeos.doctor import facade as doctor
    except (ImportError, ModuleNotFoundError):  # pragma: no cover - fail closed: withhold the name
        return True, None
    return doctor.secret_ref_scope(ref)


def _signal(invalidator: str, *, reason: str, session: Any | None, event: dict[str, Any]) -> int:
    why = reason[:200]
    if session is not None:
        return _apply(session, invalidator, why, event)
    try:
        from aeos.core.config import get_settings
        from aeos.core.db import session_scope

        if not get_settings().layout.db_path.is_file():
            return 0
        with session_scope() as s:
            return _apply(s, invalidator, why, event)
    except Exception as exc:  # pragma: no cover - logged; readiness also expires by TTL
        log.warning("readiness invalidation for %s failed: %s", invalidator.partition(":")[2], type(exc).__name__)
        return 0


def _apply(session: Any, invalidator: str, why: str, event: dict[str, Any]) -> int:
    """Invalidate and emit in one transaction: the dashboard event exists exactly when the STALE marks do."""
    from aeos.events import service as events

    try:
        from aeos.doctor import facade as doctor
    except (ImportError, ModuleNotFoundError):  # pragma: no cover - the event still tells the dashboard
        n = 0
    else:
        n = int(doctor.invalidate(session, invalidator=invalidator, reason=why))
    events.emit(session, source=EVENT_SOURCE, **event)
    return n
