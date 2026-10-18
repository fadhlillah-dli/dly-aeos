"""ComponentObservation writes and reads (§13, §38). Implements ``aeos.inventory.facade.record_observation``.

An observation is dated evidence about ONE dimension of one component, produced by a named
observer (doctor, reconciler, broker, runner, ...). It carries scope, resolved version, a redacted
operation reference and an expiry. Observations never grant permission and never rewrite desired
state; expired observations stay in history and are shown as STALE, never as current truth.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from aeos.core.enums import (
    ConfigurationStatus,
    ConnectivityStatus,
    FunctionalStatus,
    InstallationStatus,
    UsageStatus,
)
from aeos.core.errors import ValidationFailed
from aeos.core.redaction import redact, redact_text
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.models.inventory import ComponentObservation, ComponentReference

DIMENSION_STATUSES: dict[str, frozenset[str]] = {
    "installation": frozenset(s.value for s in InstallationStatus),
    "configuration": frozenset(s.value for s in ConfigurationStatus),
    "connectivity": frozenset(s.value for s in ConnectivityStatus),
    "functional": frozenset(s.value for s in FunctionalStatus),
    "usage": frozenset(s.value for s in UsageStatus),
    # Found in a lockfile/manifest/config in managed scope: a declaration, not a running process (§50).
    "declaration": frozenset({"DECLARED", "NOT_DECLARED"}),
    # Found in managed scope without a registration (§38: UNREGISTERED observation, never adopted).
    "discovery": frozenset({"UNREGISTERED"}),
}
OBSERVED_DIMENSIONS = ("installation", "configuration", "connectivity", "functional", "usage")
UNREGISTERED_PREFIX = "unregistered:"


def component_domain(session: Session, component_id: str, scope: dict[str, Any] | None = None) -> str | None:
    """Domain tag for events: OFFICE/PERSONAL-only components are tagged, platform ones are not."""
    if scope and scope.get("domain") in ("PERSONAL", "OFFICE"):
        return str(scope["domain"])
    comp = session.get(ComponentReference, component_id)
    domains = set(((comp.scope or {}).get("domains") or []) if comp else [])
    if domains == {"OFFICE"}:
        return "OFFICE"
    if domains == {"PERSONAL"}:
        return "PERSONAL"
    return None


def record_observation(
    session: Session, *, component_id: str, dimension: str, status: str, observer: str,
    observed_version: str | None = None, observed_identity: str | None = None,
    detail: dict[str, Any] | None = None, ttl_seconds: int | None = 3600, evidence_id: str | None = None,
    scope: dict[str, Any] | None = None,
) -> ComponentObservation:
    """Persist one observation and emit ``inventory.observation``.

    ``detail`` may carry ``location`` (already logical, e.g. ``<home>/.../claude.exe``) and
    ``operation_ref`` (e.g. ``claude --version``); both are moved to their columns. Everything is
    redacted before persistence. Unknown dimension/status values are rejected (fail closed).
    """
    from aeos.events import service as events

    allowed = DIMENSION_STATUSES.get(dimension)
    if allowed is None:
        raise ValidationFailed(f"unknown observation dimension {dimension!r}", code="OBSERVATION_INVALID")
    if status not in allowed:
        raise ValidationFailed(f"status {status!r} is not valid for dimension {dimension}", code="OBSERVATION_INVALID")
    if not component_id or len(component_id) > 96:
        raise ValidationFailed("component_id is required (max 96 chars)", code="OBSERVATION_INVALID")
    data = dict(detail or {})
    location = data.pop("location", None)
    operation_ref = data.pop("operation_ref", None)
    now = utcnow()
    obs = ComponentObservation(
        component_id=component_id,
        dimension=dimension,
        status=status,
        observer=observer[:64],
        observed_identity=redact_text(observed_identity)[:256] if observed_identity else None,
        observed_version=redact_text(observed_version)[:128] if observed_version else None,
        observed_location=redact_text(str(location))[:512] if location else None,
        scope=redact(dict(scope or {})),
        detail=redact(data),
        operation_ref=redact_text(str(operation_ref))[:256] if operation_ref else None,
        evidence_id=evidence_id,
        observed_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds) if ttl_seconds else None,
    )
    session.add(obs)
    session.flush()
    events.emit(
        session, source=observer[:64], kind="inventory.observation", entity_type="component",
        entity_id=component_id, entity_version=obs.observed_version,
        domain=component_domain(session, component_id, scope),
        payload={
            "observation_id": obs.id, "dimension": dimension, "status": status, "observer": observer,
            "observed_version": obs.observed_version,
            "expires_at": obs.expires_at.isoformat() if obs.expires_at else None,
        },
    )
    return obs


def is_stale(obs: ComponentObservation, now: datetime | None = None) -> bool:
    exp = ensure_aware(obs.expires_at)
    return exp is not None and exp <= (now or utcnow())


def latest_observations(
    session: Session, component_ids: list[str] | None = None, dimensions: tuple[str, ...] | None = None,
) -> dict[tuple[str, str], ComponentObservation]:
    """Latest observation per (component_id, dimension)."""
    co = ComponentObservation
    sub = select(co.component_id, co.dimension, func.max(co.observed_at).label("m")).group_by(
        co.component_id, co.dimension
    )
    if component_ids is not None:
        sub = sub.where(co.component_id.in_(component_ids))
    if dimensions is not None:
        sub = sub.where(co.dimension.in_(dimensions))
    s = sub.subquery()
    q = select(co).join(s, and_(co.component_id == s.c.component_id, co.dimension == s.c.dimension,
                                co.observed_at == s.c.m))
    out: dict[tuple[str, str], ComponentObservation] = {}
    for obs in session.scalars(q):
        key = (obs.component_id, obs.dimension)
        prev = out.get(key)
        if prev is None or (obs.id > prev.id):
            out[key] = obs
    return out


def history(session: Session, component_id: str, *, limit: int = 50) -> list[ComponentObservation]:
    return list(session.scalars(
        select(ComponentObservation).where(ComponentObservation.component_id == component_id)
        .order_by(ComponentObservation.observed_at.desc()).limit(min(limit, 500))
    ))


def expire_component_observations(session: Session, component_id: str, *, reason: str) -> int:
    """Invalidate (not delete) current observations of a component, e.g. after a config change."""
    now = utcnow()
    n = 0
    for obs in session.scalars(select(ComponentObservation).where(ComponentObservation.component_id == component_id)):
        exp = ensure_aware(obs.expires_at)
        if exp is None or exp > now:
            obs.expires_at = now
            obs.detail = {**(obs.detail or {}), "invalidated_reason": redact_text(reason)[:200]}
            n += 1
    session.flush()
    return n
