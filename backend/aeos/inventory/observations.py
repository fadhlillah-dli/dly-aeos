"""ComponentObservation writes and reads (§13, §38). Implements ``aeos.inventory.facade.record_observation``.

An observation is dated evidence about ONE dimension of one component, produced by a named
observer (doctor, reconciler, broker, runner, ...). It carries scope, resolved version, a redacted
operation reference and an expiry. Observations never grant permission and never rewrite desired
state; expired observations stay in history and are shown as STALE, never as current truth.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
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
from aeos.models.inventory import ComponentObservation, ComponentReference, ComponentRelation

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


def component_known(session: Session, component_id: str) -> bool:
    """True for a registered component, an ``unregistered:`` finding, or while no manifest is loaded yet
    (bootstrap: the Doctor runs before ``aeos seed``; those observations attach once the catalog loads)."""
    if component_id.startswith(UNREGISTERED_PREFIX):
        return True
    if session.get(ComponentReference, component_id) is not None:
        return True
    return session.scalar(select(ComponentReference.component_id).limit(1)) is None


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
    if not component_known(session, component_id):
        # Doctor and inventory share one set of component references (§08): an observation about an id the
        # loaded manifest does not register would be evidence about nothing. Unregistered findings use the
        # explicit ``unregistered:`` prefix (§38 UNREGISTERED observation).
        raise ValidationFailed(f"component {component_id!r} is not registered in the InstallManifest",
                               code="OBSERVATION_INVALID")
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


def record_usage_observation(
    session: Session, *, component_id: str, status: str, observer: str, run_id: str | None = None,
    operation_ref: str | None = None, ttl_seconds: int | None = 900, domain: str | None = None,
    detail: dict[str, Any] | None = None,
) -> ComponentObservation:
    """Usage/liveness observation (§38). IN_USE without a run/operation reference or a time window is
    refused: "digunakan saat ini" needs a relevant observation reference."""
    if status == "IN_USE" and (not (run_id or operation_ref) or not ttl_seconds):
        raise ValidationFailed("IN_USE needs a run/operation reference and a time window (ttl)",
                               code="OBSERVATION_INVALID")
    scope: dict[str, Any] = {}
    if run_id:
        scope["run_id"] = run_id
    if domain in ("PERSONAL", "OFFICE"):
        scope["domain"] = domain
    data = {**(detail or {}), **({"operation_ref": operation_ref} if operation_ref else {})}
    return record_observation(session, component_id=component_id, dimension="usage", status=status,
                              observer=observer, detail=data, ttl_seconds=ttl_seconds, scope=scope)


def record_relation_observation(
    session: Session, *, source_id: str, target_id: str, relation: str, observer: str, operation: str = "CALL",
    run_id: str | None = None, domain: str | None = None, data_description: str = "",
) -> ComponentRelation:
    """Upsert one OBSERVED edge with ``last_observed_at`` now: traffic/event actually seen, distinct from the
    manifest (DESIGN) and configuration (CONFIG) edges (§36). The edge keeps provenance and scope."""
    from aeos.events import service as events
    from aeos.inventory.manifest import RELATION_TYPES

    if relation not in RELATION_TYPES:
        raise ValidationFailed(f"unknown relation {relation!r}", code="OBSERVATION_INVALID")
    for cid in (source_id, target_id):
        if not cid or not component_known(session, cid):
            raise ValidationFailed(f"component {cid!r} is not registered", code="OBSERVATION_INVALID")
    now = utcnow()
    row = session.scalar(select(ComponentRelation).where(
        ComponentRelation.source_id == source_id, ComponentRelation.target_id == target_id,
        ComponentRelation.relation == relation, ComponentRelation.lens == "OBSERVED"))
    provenance = redact_text(f"{observer}:{run_id or 'operation'}")[:256]
    scope = {"domain": domain} if domain in ("PERSONAL", "OFFICE") else {"domains": ["PLATFORM"]}
    if row is None:
        row = ComponentRelation(source_id=source_id, target_id=target_id, relation=relation, lens="OBSERVED",
                                provenance=provenance, data_description=redact_text(data_description)[:2000],
                                operation=operation[:16], scope={**scope, "source": observer[:64]},
                                last_observed_at=now)
        session.add(row)
    else:
        row.last_observed_at, row.provenance, row.tombstoned_at = now, provenance, None
        row.scope = {**scope, "source": observer[:64]}
    session.flush()
    events.emit(session, source=observer[:64], kind="inventory.relation.observed", entity_type="component",
                entity_id=source_id, domain=domain if domain in ("PERSONAL", "OFFICE") else None,
                payload={"relation_id": row.id, "target": target_id, "relation": relation})
    return row


def is_stale(obs: ComponentObservation, now: datetime | None = None) -> bool:
    exp = ensure_aware(obs.expires_at)
    return exp is not None and exp <= (now or utcnow())


ALL_DOMAINS: Any = object()  # sentinel: no domain filter (platform-internal callers: drift, reconcile, doctor)


def visible_condition(domain: str | None) -> Any:
    """SQL condition: observation scope has no domain tag, or the viewer's domain (§23/§35: an OFFICE-tagged
    observation of a shared component never reaches a PERSONAL view, and vice versa)."""
    tag = func.json_extract(ComponentObservation.scope, "$.domain")
    return tag.is_(None) if domain is None else or_(tag.is_(None), tag == domain)


def latest_observations(
    session: Session, component_ids: list[str] | None = None, dimensions: tuple[str, ...] | None = None,
    *, visible_to: Any = ALL_DOMAINS,
) -> dict[tuple[str, str], ComponentObservation]:
    """Latest observation per (component_id, dimension). With ``visible_to=<domain>`` only observations that
    domain may see count (the latest *visible* one, not the latest overall)."""
    co = ComponentObservation
    sub = select(co.component_id, co.dimension, func.max(co.observed_at).label("m")).group_by(
        co.component_id, co.dimension
    )
    if component_ids is not None:
        sub = sub.where(co.component_id.in_(component_ids))
    if dimensions is not None:
        sub = sub.where(co.dimension.in_(dimensions))
    if visible_to is not ALL_DOMAINS:
        sub = sub.where(visible_condition(visible_to))
    s = sub.subquery()
    q = select(co).join(s, and_(co.component_id == s.c.component_id, co.dimension == s.c.dimension,
                                co.observed_at == s.c.m))
    if visible_to is not ALL_DOMAINS:
        q = q.where(visible_condition(visible_to))
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
