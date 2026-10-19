"""Cross-module contract for inventory observations (§37, §38). Owned by ``aeos.inventory``.

Observations never grant permission and never rewrite desired state.

Additive (2026-10-19, C4): runtime producers may record usage/relation observations explicitly, and
decision flows may cache an admission/lifecycle change bound to an ACTIVE human Decision. The
inventory itself also derives usage and decision-side dimensions from the owning records (run,
lease, usage ledger, publication outbox, Decision registry), so callers are never required to.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from aeos.models.inventory import ComponentObservation, ComponentReference, ComponentRelation


def record_observation(
    session: Session, *, component_id: str, dimension: str, status: str, observer: str,
    observed_version: str | None = None, observed_identity: str | None = None,
    detail: dict[str, Any] | None = None, ttl_seconds: int | None = 3600, evidence_id: str | None = None,
    scope: dict[str, Any] | None = None,
) -> ComponentObservation:
    kwargs = dict(locals())  # captured before the lazy import adds 'impl'
    from aeos.inventory.observations import record_observation as impl

    return impl(**kwargs)


def get_component(session: Session, component_id: str) -> ComponentReference | None:
    return session.get(ComponentReference, component_id)


def record_usage_observation(
    session: Session, *, component_id: str, status: str, observer: str, run_id: str | None = None,
    operation_ref: str | None = None, ttl_seconds: int | None = 900, domain: str | None = None,
    detail: dict[str, Any] | None = None,
) -> ComponentObservation:
    """Usage/liveness (§38): IN_USE needs a run/process/operation reference and a time window (ttl);
    a finished request is LAST_USED."""
    kwargs = dict(locals())
    from aeos.inventory.observations import record_usage_observation as impl

    return impl(**kwargs)


def record_relation_observation(
    session: Session, *, source_id: str, target_id: str, relation: str, observer: str,
    operation: str = "CALL", run_id: str | None = None, domain: str | None = None,
    data_description: str = "",
) -> ComponentRelation:
    """Upsert an OBSERVED-lens edge (actual traffic/event) with ``last_observed_at`` = now (§36)."""
    kwargs = dict(locals())
    from aeos.inventory.observations import record_relation_observation as impl

    return impl(**kwargs)


def set_admission(session: Session, *, component_id: str, status: str, decision_id: str, actor: str
                  ) -> ComponentReference:
    """Cache an admission state bound to an ACTIVE human Decision (audited; refused otherwise)."""
    from aeos.inventory.decisions import set_admission as impl

    return impl(session, component_id=component_id, status=status, decision_id=decision_id, actor=actor)


def set_lifecycle(session: Session, *, component_id: str, status: str, decision_id: str, actor: str
                  ) -> ComponentReference:
    """Cache a final-lifecycle state (DEPRECATED/QUARANTINED/RETIRING/RETIRED/ACTIVE) bound to an ACTIVE
    human Decision. Never deletes rows or history."""
    from aeos.inventory.decisions import set_lifecycle as impl

    return impl(session, component_id=component_id, status=status, decision_id=decision_id, actor=actor)


def effective_enablement(session: Session, component_id: str, *, domain: str | None = None) -> dict[str, Any]:
    """§38 Enablement as the inventory shows it: ENABLED only with an explicit approved-config basis."""
    from aeos.inventory.views import enablement_for

    return enablement_for(session, component_id, domain=domain)
