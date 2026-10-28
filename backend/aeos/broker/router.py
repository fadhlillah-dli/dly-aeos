"""Broker routes: admin views under ``/api/broker`` (never tokens or hashes) and the worker model
proxy under ``/broker/v1`` (capability bearer auth only, see ``model_proxy``)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from aeos.api.deps import DB, ActiveDomain, AdminPrincipal
from aeos.broker import grants
from aeos.broker.model_proxy import router as proxy_router
from aeos.core.errors import NotFound
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.models.broker_ext import BrokerRequest, BrokerSideEffect
from aeos.models.routing import CapabilityGrant

router = APIRouter(tags=["broker"])
router.include_router(proxy_router)


@router.get("/api/broker/grants")
def list_grants(principal: AdminPrincipal, domain: ActiveDomain, db: DB, run_id: str | None = None,
                active: bool = Query(False), limit: int = Query(200, ge=1, le=500)) -> dict[str, Any]:
    q = select(CapabilityGrant).where(CapabilityGrant.domain == domain.value)
    if run_id:
        q = q.where(CapabilityGrant.run_id == run_id)
    if active:
        q = q.where(CapabilityGrant.active.is_(True))
    rows = list(db.scalars(q.order_by(CapabilityGrant.created_at.desc()).limit(limit)))
    if principal.allowed_project_ids is not None:
        rows = [g for g in rows if g.project_id in principal.allowed_project_ids]
    return {"items": [grants.grant_view(g) for g in rows], "source": "capability_grants (token hash never exposed)",
            "checked_at": utcnow().isoformat(), "mode": "LIVE"}


def _request_view(r: BrokerRequest) -> dict[str, Any]:
    """One broker request. ``kind`` tells a worker/evaluation call from an owner's feasibility probe (decision C: a
    probe of an OpenRouter :free model is a row too, so it counts against the free-model limit); a probe still PENDING
    from an earlier UTC day was never settled and is shown ABANDONED (it counted on its day)."""
    from aeos.broker.feasibility import PROBE_MARK

    probe = r.grant_id == PROBE_MARK
    outcome = r.outcome
    created = ensure_aware(r.created_at)
    if probe and outcome == "PENDING" and created is not None and created < utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0):
        outcome = "ABANDONED"
    return {"id": r.id, "kind": "FEASIBILITY_PROBE" if probe else "WORKER", "run_id": None if probe else r.run_id,
            "route_key": r.route_key, "outcome": outcome, "status": r.status_code, "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "cost_usd": float(r.cost_usd) if r.cost_usd is not None else "UNKNOWN", "model_resolved": r.model_resolved,
            "created_at": r.created_at.isoformat(), "error": r.error_summary}


@router.get("/api/broker/requests")
def list_requests(principal: AdminPrincipal, domain: ActiveDomain, db: DB, run_id: str | None = None,
                  limit: int = Query(200, ge=1, le=500)) -> dict[str, Any]:
    q = select(BrokerRequest).where(BrokerRequest.domain == domain.value)
    if run_id:
        q = q.where(BrokerRequest.run_id == run_id)
    if principal.allowed_project_ids is not None:
        q = q.where(BrokerRequest.project_id.in_(principal.allowed_project_ids))
    rows = db.scalars(q.order_by(BrokerRequest.created_at.desc()).limit(limit))
    items = [_request_view(r) for r in rows]
    return {"items": items, "source": "broker_requests", "checked_at": utcnow().isoformat(), "mode": "LIVE"}


@router.get("/api/broker/side-effects")
def list_side_effects(principal: AdminPrincipal, domain: ActiveDomain, db: DB,
                      run_id: str | None = None) -> dict[str, Any]:
    q = select(BrokerSideEffect).where(BrokerSideEffect.domain == domain.value)
    if run_id:
        q = q.where(BrokerSideEffect.run_id == run_id)
    if principal.allowed_project_ids is not None:
        q = q.where(BrokerSideEffect.project_id.in_(principal.allowed_project_ids))
    items = [{"id": s.id, "run_id": s.run_id, "kind": s.kind, "status": s.status, "target": s.target,
              "external_url": s.external_url, "commit_sha": s.commit_sha, "attempts": s.attempts,
              "updated_at": s.updated_at.isoformat()} for s in db.scalars(q.order_by(BrokerSideEffect.created_at.desc()))]
    return {"items": items, "source": "broker_side_effects (outbox)", "checked_at": utcnow().isoformat(), "mode": "LIVE"}


class RevokeBody(BaseModel):
    reason: str = Field(default="revoked by owner", max_length=200)


@router.post("/api/broker/runs/{run_id}/revoke")
def revoke_run_grants(run_id: str, body: RevokeBody, principal: AdminPrincipal, domain: ActiveDomain,
                      db: DB) -> dict[str, Any]:
    owned = db.scalar(select(CapabilityGrant).where(CapabilityGrant.run_id == run_id))
    if owned is not None and (owned.domain != domain.value
                              or not principal.can_access_project(owned.project_id, owned.domain)):
        raise NotFound("run not found")
    n = grants.revoke_grants(db, run_id=run_id, reason=body.reason, actor=f"user:{principal.user.id}")
    db.commit()
    return {"run_id": run_id, "revoked": n}


@router.post("/api/broker/routes/{route_key}/feasibility")
def probe_route_feasibility(route_key: str, principal: AdminPrincipal, domain: ActiveDomain,
                            db: DB) -> dict[str, Any]:
    """§23 route feasibility (ADR-0111): one synthetic PUBLIC request to a free, human-activated PERSONAL route of
    an ARENA/COMPARATOR/LOCAL/WORKER pool. Evidence only - the route's status and eligibility never change."""
    from aeos.broker.feasibility import probe_route
    from aeos.core.enums import Domain
    from aeos.core.errors import AeosError, PolicyDenied

    if domain != Domain.PERSONAL:
        raise PolicyDenied("route feasibility probes run in the PERSONAL domain only (OFFICE default deny, §23)",
                           code="OFFICE_DEFAULT_DENY")
    try:
        out = probe_route(db, route_key, actor=f"user:{principal.user.id}")
    except AeosError:
        db.commit()  # keep the DENIED audit record
        raise
    db.commit()
    return out


@router.post("/api/broker/routes/{route_key:path}/tool-probe")
def probe_route_tools(route_key: str, principal: AdminPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    """Tool-call probe (ADR-0125): the feasibility probe's gates and one request with one trivial function definition;
    PASS/FAIL evidence bound to the route's configuration hash. ``supports_tools`` and the route's status never change."""
    from aeos.broker.feasibility import probe_route
    from aeos.core.enums import Domain
    from aeos.core.errors import AeosError, PolicyDenied

    if domain != Domain.PERSONAL:
        raise PolicyDenied("route tool-call probes run in the PERSONAL domain only (OFFICE default deny, §23)",
                           code="OFFICE_DEFAULT_DENY")
    try:
        out = probe_route(db, route_key, actor=f"user:{principal.user.id}", tools=True)
    except AeosError:
        db.commit()  # keep the DENIED audit record
        raise
    db.commit()
    return out


@router.post("/api/broker/side-effects/{side_effect_id}/reconcile")
def reconcile_side_effect(side_effect_id: str, principal: AdminPrincipal, domain: ActiveDomain,
                          db: DB) -> dict[str, Any]:
    """Read the external state of a PENDING/UNKNOWN push/PR and record it (§14 crash recovery).
    Read-only towards the provider; nothing is pushed, created or merged."""
    from aeos.broker.git_broker import reconcile_side_effect as impl

    se = db.get(BrokerSideEffect, side_effect_id)
    if se is None or se.domain != domain.value or (
            se.project_id and not principal.can_access_project(se.project_id, se.domain)):
        raise NotFound("side effect not found")
    out = impl(db, side_effect_id, actor=f"user:{principal.user.id}")
    db.commit()
    return out
