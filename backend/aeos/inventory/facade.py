"""Cross-module contract for inventory observations (§37, §38). Owned by ``aeos.inventory``.

Observations never grant permission and never rewrite desired state.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from aeos.models.inventory import ComponentObservation, ComponentReference


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


def blast_radius(session: Session, component_id: str, *, domain: str | None,
                 direction: str = "dependents") -> dict[str, Any]:
    """Who is affected if *component_id* changes/fails (``dependents``) or what it relies on (read-only)."""
    from aeos.inventory.views import blast_radius as impl

    return impl(session, component_id, domain=domain, direction=direction)
