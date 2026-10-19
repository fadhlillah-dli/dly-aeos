"""Controlled path that makes a worker route OFFICE-eligible (§23 "sampai otorisasi perusahaan").

OFFICE stays default deny. A route becomes OFFICE-eligible only through this operation, and only while:

* an ACTIVE company authorisation exists (``aeos.policy.office.company_authorisation``),
* the route is in the company-approved ``approved_worker_routes`` of the effective OFFICE policy,
* the upstream credential is an ``office/`` reference (OFFICE credentials never come from PERSONAL refs).

The owner's POLICY decision on ``office_route/<route_key>`` is bound to the route identity (provider, model,
endpoint, credential reference) and to the company authorisation it relies on. It is recorded only here (the
generic decision endpoint refuses ``office_route`` targets). ``routing.activate_route`` accepts an
OFFICE-eligible route only while that binding holds, and the scheduler admits an OFFICE task on a route only
while it holds (``OFFICE_ROUTE_NOT_AUTHORISED``): revoking the company authorisation or the decision ends it.
Does not commit.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.enums import Domain
from aeos.core.errors import NotFound, PolicyDenied, ValidationFailed
from aeos.core.hashing import content_hash
from aeos.models.decisions import Decision
from aeos.models.routing import ModelRoute
from aeos.policy import catalog, office
from aeos.policy import decisions as registry

TARGET_OFFICE_ROUTE = catalog.TARGET_OFFICE_ROUTE


def route_identity(route: ModelRoute, credential_ref: str | None = None) -> str:
    ref = credential_ref if credential_ref is not None else (route.mode_params or {}).get("credential_ref")
    return content_hash({"route_key": route.route_key, "provider": route.provider, "model_id": route.model_id,
                         "endpoint": route.endpoint, "credential_ref": ref})


def authorise_office_route(session: Session, *, route_key: str, credential_ref: str, actor_user_id: str,
                           rationale: str) -> Decision:
    auth = office.company_authorisation(session)
    if auth is None:
        raise PolicyDenied("no ACTIVE company authorisation for OFFICE (§23 default deny)",
                           code="OFFICE_AUTHORISATION_REQUIRED")
    route = session.scalar(select(ModelRoute).where(ModelRoute.route_key == route_key))
    if route is None:
        raise NotFound("route not found")
    if route.pool != "WORKER" or route.mode in ("MOCK", "DEMO"):
        raise ValidationFailed("only real WORKER routes can be made OFFICE-eligible")
    if route_key not in office.office_policy(session).get("approved_worker_routes", []):
        raise PolicyDenied("the route is not in the company-approved OFFICE worker routes (approved_worker_routes)",
                           code="ROUTE_NOT_COMPANY_APPROVED")
    ref = (credential_ref or "").strip()
    if not ref.startswith("office/") or ".." in ref or len(ref) > 200:
        raise ValidationFailed("OFFICE routes need an office/<provider> credential reference",
                               code="OFFICE_CREDENTIAL_REF_REQUIRED")
    if not rationale or len(rationale.strip()) < 10:
        raise ValidationFailed("a rationale (min 10 characters) is required")
    identity = route_identity(route, ref)
    d = registry.record_decision(
        session, kind="POLICY", actor_user_id=actor_user_id, target_type=TARGET_OFFICE_ROUTE, target_id=route_key,
        target_hash=identity, allowed_operations=["route.office_eligible"], domain=Domain.OFFICE.value,
        auth_scope={"route_key": route_key, "company_authorisation_id": auth.id}, rationale=rationale,
        details={"company_authorisation_id": auth.id, "credential_ref": ref,
                 "previous_eligible_domains": list(route.eligible_domains or []),
                 "note": "OFFICE eligibility only; activation still needs ROUTE_ACTIVATION/TRIAL bound to the route "
                         "hash. An office/ credential never serves a PERSONAL call (ADR-0041), so the route is "
                         "OFFICE-only from now on."},
    )
    # the office/ credential cannot serve PERSONAL calls (broker + routing credential namespace rule): the route is
    # recorded as OFFICE-only instead of claiming a PERSONAL eligibility it can no longer honour
    route.eligible_domains = [Domain.OFFICE.value]
    route.mode_params = {**(route.mode_params or {}), "credential_ref": ref}
    try:
        from aeos.routing.service import refresh_config_hash

        refresh_config_hash(session, route, reason=f"OFFICE eligibility decision {d.id}")
    except ImportError:  # pragma: no cover - routing ships with the policy module
        pass
    session.flush()
    return d


def office_route_authorised(session: Session, route: ModelRoute) -> Decision | None:
    """The ACTIVE owner decision that makes *route* OFFICE-eligible, while its company authorisation is still the
    ACTIVE one and the route identity is unchanged; None otherwise (fail closed)."""
    auth = office.company_authorisation(session)
    if auth is None:
        return None
    d = registry.find_active_decision(session, kind="POLICY", target_type=TARGET_OFFICE_ROUTE,
                                      target_id=route.route_key, target_hash=route_identity(route))
    if d is None or d.actor_kind != "HUMAN" or (d.details or {}).get("company_authorisation_id") != auth.id:
        return None
    return d


def status(session: Session, route: ModelRoute) -> dict[str, Any]:
    d = office_route_authorised(session, route)
    return {"route_key": route.route_key, "office_eligible": d is not None, "decision_id": d.id if d else None,
            "company_authorisation": office.company_authorisation(session) is not None}
