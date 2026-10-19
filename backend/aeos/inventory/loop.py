"""Optional inventory polling (§38 "Polling mengikuti interval/TTL per jenis data, rate limit, resource budget").

Off by default. When the owner enables it (``aeos inventory poll --enable``, audited system setting
``inventory.poll_enabled``), the server lifecycle hook refreshes — read-only, scoped, one component at a
time — only the core components whose latest reconciler observation has expired according to that
component's own probe TTL. Each tick is bounded (``MAX_PER_TICK`` components) and every refresh goes
through the same per-scope concurrency guard as the API. Nothing is installed, enabled or adopted.
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.config import Settings
from aeos.core.timeutil import ensure_aware, utcnow

log = logging.getLogger("aeos.inventory")
FLAG_KEY = "inventory.poll_enabled"
TICK_SECONDS = 60.0
MAX_PER_TICK = 5  # resource bound per tick (probes are sequential and time-bounded)
_stop = threading.Event()
_thread: threading.Thread | None = None


def poll_enabled(session: Session) -> bool:
    from aeos.models.identity import SystemSetting

    row = session.get(SystemSetting, FLAG_KEY)
    return bool(row is not None and isinstance(row.value, dict) and row.value.get("enabled") is True)


def set_poll_enabled(session: Session, enabled: bool, *, actor: str, reason: str) -> dict[str, Any]:
    from aeos.audit import service as audit
    from aeos.events import service as events
    from aeos.models.identity import SystemSetting

    value = {"enabled": bool(enabled), "set_by": actor, "set_at": utcnow().isoformat(), "reason": reason[:300]}
    row = session.get(SystemSetting, FLAG_KEY)
    if row is None:
        session.add(SystemSetting(key=FLAG_KEY, value=value))
    else:
        row.value = value
        row.version = (row.version or 1) + 1
    session.flush()
    audit.record(session, actor_type="cli" if actor in ("cli", "local-operator") else "user", actor_id=actor,
                 action="inventory.poll.set", target_type="system_setting", target_id=FLAG_KEY, reason=reason[:300],
                 details={"enabled": bool(enabled)})
    events.emit(session, source="inventory", kind="inventory.poll.changed", entity_type="system_setting",
                entity_id=FLAG_KEY, payload={"enabled": bool(enabled)})
    return value


def due_components(session: Session, *, now: Any = None, limit: int = MAX_PER_TICK) -> list[str]:
    """Core components whose latest reconciler observation expired (per-component probe TTL), oldest first."""
    from aeos.inventory.observations import latest_observations
    from aeos.inventory.reconcile import OBSERVER, core_targets
    from aeos.models.inventory import ComponentReference

    now = now or utcnow()
    comps = core_targets(session.scalars(select(ComponentReference)))
    latest = latest_observations(session, [c.component_id for c in comps], ("installation",))
    due: list[tuple[Any, str]] = []
    for c in comps:
        obs = latest.get((c.component_id, "installation"))
        if obs is None or obs.observer != OBSERVER:
            due.append((None, c.component_id))
            continue
        ttl = int((c.probe or {}).get("ttl_seconds") or 3600)
        seen = ensure_aware(obs.observed_at)
        if seen is None or seen + timedelta(seconds=ttl) <= now:
            due.append((seen, c.component_id))
    due.sort(key=lambda x: (x[0] is not None, x[0] or now, x[1]))
    return [cid for _at, cid in due[:limit]]


def tick(settings: Settings) -> list[str]:
    """One bounded polling pass; returns the refreshed component ids (empty while polling is disabled)."""
    from aeos.core.db import session_scope
    from aeos.core.errors import AeosError
    from aeos.inventory.reconcile import reconcile

    refreshed: list[str] = []
    with session_scope() as s:
        if not poll_enabled(s):
            return refreshed
        targets = due_components(s)
    for cid in targets:
        try:
            with session_scope() as s:
                reconcile(s, settings, component_id=cid, actor="system", trigger="schedule")
            refreshed.append(cid)
        except AeosError as exc:  # e.g. RECONCILE_IN_PROGRESS from a concurrent API refresh
            log.info("inventory poll skipped %s: %s", cid, exc.code)
    return refreshed


def _run(settings: Settings) -> None:
    while not _stop.wait(TICK_SECONDS):
        try:
            tick(settings)
        except Exception:  # never fatal; the coverage view shows the stale observations
            log.exception("inventory poll tick failed")


def start(settings: Settings) -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_run, args=(settings,), name="aeos-inventory-poll", daemon=True)
    _thread.start()


def stop() -> None:
    global _thread
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=30)
    _thread = None
