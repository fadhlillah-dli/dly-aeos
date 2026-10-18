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
from aeos.core.timeutil import utcnow
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


@router.get("/api/broker/requests")
def list_requests(principal: AdminPrincipal, domain: ActiveDomain, db: DB, run_id: str | None = None,
                  limit: int = Query(200, ge=1, le=500)) -> dict[str, Any]:
    q = select(BrokerRequest).where(BrokerRequest.domain == domain.value)
    if run_id:
        q = q.where(BrokerRequest.run_id == run_id)
    rows = db.scalars(q.order_by(BrokerRequest.created_at.desc()).limit(limit))
    items = [{"id": r.id, "run_id": r.run_id, "route_key": r.route_key, "outcome": r.outcome, "status": r.status_code,
              "prompt_tokens": r.prompt_tokens, "completion_tokens": r.completion_tokens,
              "cost_usd": float(r.cost_usd) if r.cost_usd is not None else "UNKNOWN", "model_resolved": r.model_resolved,
              "created_at": r.created_at.isoformat(), "error": r.error_summary} for r in rows]
    return {"items": items, "source": "broker_requests", "checked_at": utcnow().isoformat(), "mode": "LIVE"}


@router.get("/api/broker/side-effects")
def list_side_effects(principal: AdminPrincipal, domain: ActiveDomain, db: DB,
                      run_id: str | None = None) -> dict[str, Any]:
    q = select(BrokerSideEffect).where(BrokerSideEffect.domain == domain.value)
    if run_id:
        q = q.where(BrokerSideEffect.run_id == run_id)
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
    if owned is not None and owned.domain != domain.value:
        from aeos.core.errors import NotFound

        raise NotFound("run not found")
    n = grants.revoke_grants(db, run_id=run_id, reason=body.reason, actor=f"user:{principal.user.id}")
    db.commit()
    return {"run_id": run_id, "revoked": n}
