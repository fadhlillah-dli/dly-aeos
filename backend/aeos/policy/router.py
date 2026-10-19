"""Policy & Decision Registry (§10, §20, §26): decisions, external-write interlock, Office deny defaults, ADRs, MCI reference."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from aeos.api.deps import DB, ActiveDomain, AdminPrincipal, CurrentPrincipal, load_project
from aeos.core.enums import Domain
from aeos.core.errors import NotFound
from aeos.core.hashing import content_hash
from aeos.core.timeutil import utcnow
from aeos.identity.service import Principal
from aeos.models.decisions import ArchitectureDecisionRecord, Decision
from aeos.models.policy_ext import DecisionRequest
from aeos.policy import adrs as adr_loader
from aeos.policy import catalog, enablement, office, recording, snapshot
from aeos.policy import decisions as registry
from aeos.policy import queue as decision_queue
from aeos.policy import requests as decision_requests

router = APIRouter(tags=["policy"])


def _now() -> str:
    return utcnow().isoformat()


def _visible(principal: Principal, domain: Domain, d: Decision) -> bool:
    if d.domain is not None and d.domain != domain.value:
        return False
    if d.project_id is not None and principal.allowed_project_ids is not None:
        return d.project_id in principal.allowed_project_ids
    return True


def _scoped_refs(principal: Principal) -> Any:
    """Filter for decision references embedded in projections (policy snapshot): only global ones and
    those of the principal's active, accessible domain and project scope (§35, §50 incl. counts)."""
    active = principal.active_domain

    def _filter(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for r in refs:
            dom = r.get("domain")
            if dom is not None and (active is None or dom != active.value or not principal.can_access_domain(dom)):
                continue
            pid = r.get("project_id")
            if pid is not None and principal.allowed_project_ids is not None and pid not in principal.allowed_project_ids:
                continue
            out.append(r)
        return out

    return _filter


def _load_visible_decision(db: DB, principal: Principal, domain: Domain, decision_id: str) -> Decision:
    d = db.get(Decision, decision_id)
    if d is None or not _visible(principal, domain, d):
        raise NotFound("decision not found")  # cross-domain is a 404: no Office metadata leaks (§35)
    return d


# ---------------------------------------------------------------- decisions (read)


@router.get("/api/decisions/queue")
def get_queue(db: DB, principal: CurrentPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    if registry.expire_due_decisions(db):
        db.commit()
    items = decision_queue.queue_items(db, domain, project_ids=principal.allowed_project_ids)
    return {"items": items, "counts": decision_queue.counts(items), "source": "canonical records (plans, "
            "publications, change_proposals, task_executions, worker_runs, decisions, decision_requests)",
            "checked_at": _now(), "mode": "LIVE", "domain": domain.value}


@router.get("/api/decisions")
def list_decisions(
    db: DB, principal: CurrentPrincipal, domain: ActiveDomain,
    kind: str | None = None, status: str | None = None, target_type: str | None = None,
    target_id: str | None = None, project_id: str | None = None, limit: int = Query(200, ge=1, le=500),
) -> dict[str, Any]:
    if registry.expire_due_decisions(db):
        db.commit()
    q = select(Decision).where(or_(Decision.domain == domain.value, Decision.domain.is_(None)))
    if kind:
        q = q.where(Decision.kind == kind)
    if target_type:
        q = q.where(Decision.target_type == target_type)
    if target_id:
        q = q.where(Decision.target_id == target_id)
    if project_id:
        q = q.where(Decision.project_id == project_id)
    rows = [d for d in db.scalars(q.order_by(Decision.created_at.desc()).limit(limit)) if _visible(principal, domain, d)]
    items = [registry.decision_view(db, d) for d in rows]
    if status:
        items = [i for i in items if i["status"] == status]
    by_status: dict[str, int] = {}
    for i in items:
        by_status[i["status"]] = by_status.get(i["status"], 0) + 1
    return {"items": items, "counts": by_status, "source": "decisions", "checked_at": _now(), "mode": "LIVE"}


@router.get("/api/decisions/shapes")
def decision_shapes(_principal: CurrentPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    """The exact target shapes consumers read (UI/CLI forms are generated from this, never guessed)."""
    return {
        "write_enablement": {"providers": list(catalog.providers_for(domain)),
                             "target_type": catalog.TARGET_PROVIDER_WRITE},
        "exceptions": {name: {"target_type": p.target_type, "target_id": p.target_id, "domains": list(p.domains),
                              "details": dict(p.details), "flags": list(p.flags), "consumer": p.consumer,
                              "available": domain.value in p.domains}
                       for name, p in catalog.EXCEPTION_PRESETS.items()},
        "office_authorization": {"target_type": catalog.TARGET_OFFICE_POLICY, "target_id": Domain.OFFICE.value,
                                 "providers": list(catalog.providers_for(Domain.OFFICE)),
                                 "available": domain == Domain.OFFICE},
        "domain": domain.value, "checked_at": _now(),
    }


@router.get("/api/decisions/{decision_id}")
def get_decision(decision_id: str, db: DB, principal: CurrentPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    return registry.decision_view(db, _load_visible_decision(db, principal, domain, decision_id))


# ---------------------------------------------------------------- decisions (write)


class DecisionIn(BaseModel):
    kind: Literal["EXCEPTION", "RISK_ACCEPTANCE", "WRITE_ENABLEMENT", "POLICY", "BUDGET"]
    target_type: str | None = Field(default=None, max_length=64)
    target_id: str | None = Field(default=None, max_length=128)
    target_version: str | None = Field(default=None, max_length=64)
    target_hash: str | None = Field(default=None, max_length=80)
    project_id: str | None = None
    allowed_operations: list[str] = Field(default_factory=list, max_length=50)
    auth_scope: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(min_length=10, max_length=5000)
    expires_at: datetime | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    # WRITE_ENABLEMENT
    provider: str | None = None
    enabled: bool | None = None
    # POLICY (OFFICE company authorisation / overrides)
    office_authorization: bool = False
    company_reference: str | None = Field(default=None, max_length=500)
    allowed_providers: list[str] = Field(default_factory=list)
    office_policy: dict[str, Any] | None = None
    # EXCEPTION presets with the exact target shape their consumer reads (catalog.EXCEPTION_PRESETS)
    exception: Literal["secret_store_interim_keyring", "worker_sandbox_process"] | None = None
    allow_bash: bool | None = None
    request_id: str | None = None


def _request_visible(principal: Principal, domain: Domain, req: DecisionRequest | None) -> bool:
    """Same scope as the queue: the active domain, domain-less (platform) requests only in PERSONAL,
    and the principal's project scope. Anything else is a 404 (no cross-domain metadata)."""
    if req is None:
        return False
    if req.domain is None:
        if domain != Domain.PERSONAL:
            return False
    elif req.domain != domain.value:
        return False
    if req.project_id is not None and principal.allowed_project_ids is not None:
        return req.project_id in principal.allowed_project_ids
    return True


# Detail keys that carry policy meaning and are set only from the dedicated, validated fields above.
_RESERVED_DETAIL_KEYS = recording.RESERVED_DETAIL_KEYS


@router.post("/api/decisions", status_code=201)
def create_decision(body: DecisionIn, db: DB, principal: AdminPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    project_id = None
    if body.project_id:
        project_id = load_project(db, principal, body.project_id, domain=domain).id
    decision = recording.record_generic_decision(
        db, actor_user_id=principal.user.id, domain=domain, kind=body.kind, rationale=body.rationale,
        auth_scope=body.auth_scope, project_id=project_id, target_type=body.target_type, target_id=body.target_id,
        target_version=body.target_version, target_hash=body.target_hash, allowed_operations=body.allowed_operations,
        expires_at=body.expires_at, details=body.details, provider=body.provider, enabled=body.enabled,
        office_authorization=body.office_authorization, company_reference=body.company_reference,
        allowed_providers=body.allowed_providers, office_policy=body.office_policy, exception=body.exception,
        allow_bash=body.allow_bash,
    )
    if body.request_id:
        req = db.get(DecisionRequest, body.request_id)
        if not _request_visible(principal, domain, req):
            raise NotFound("decision request not found")
        decision_requests.resolve_request(db, req.id, decision_id=decision.id, actor_user_id=principal.user.id)
    db.commit()
    return registry.decision_view(db, decision)


class RevokeIn(BaseModel):
    reason: str = Field(min_length=5, max_length=2000)


@router.post("/api/decisions/{decision_id}/revoke")
def revoke(decision_id: str, body: RevokeIn, db: DB, principal: AdminPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    d = _load_visible_decision(db, principal, domain, decision_id)
    registry.revoke_decision(db, d.id, actor_user_id=principal.user.id, reason=body.reason)
    db.commit()
    return registry.decision_view(db, d)


# ---------------------------------------------------------------- decision requests


class DecisionRequestIn(BaseModel):
    kind: str
    title: str = Field(min_length=3, max_length=256)
    reason: str = Field(default="", max_length=5000)
    project_id: str | None = None
    target_type: str = Field(default="", max_length=64)
    target_id: str = Field(default="", max_length=128)
    target_hash: str | None = Field(default=None, max_length=80)
    requested_operations: list[str] = Field(default_factory=list, max_length=50)
    details: dict[str, Any] = Field(default_factory=dict)


@router.get("/api/decision-requests")
def list_requests(db: DB, principal: CurrentPrincipal, domain: ActiveDomain,
                  status: str | None = None) -> dict[str, Any]:
    q = select(DecisionRequest).where(or_(DecisionRequest.domain == domain.value, DecisionRequest.domain.is_(None)))
    if status:
        q = q.where(DecisionRequest.status == status)
    rows = [r for r in db.scalars(q.order_by(DecisionRequest.created_at.desc()).limit(500))
            if _request_visible(principal, domain, r)]
    return {"items": [decision_requests.request_view(r) for r in rows], "source": "decision_requests",
            "checked_at": _now(), "mode": "LIVE"}


@router.post("/api/decision-requests", status_code=201)
def create_request(body: DecisionRequestIn, db: DB, principal: AdminPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    project_id = load_project(db, principal, body.project_id, domain=domain).id if body.project_id else None
    req = decision_requests.create_request(
        db, kind=body.kind, title=body.title, reason=body.reason, requested_by=principal.user.id,
        requested_by_kind="HUMAN", project_id=project_id, domain=domain.value, target_type=body.target_type,
        target_id=body.target_id, target_hash=body.target_hash, requested_operations=body.requested_operations,
        details=body.details,
    )
    db.commit()
    return decision_requests.request_view(req)


class DeclineIn(BaseModel):
    note: str = Field(min_length=5, max_length=2000)


@router.post("/api/decision-requests/{request_id}/decline")
def decline_request(request_id: str, body: DeclineIn, db: DB, principal: AdminPrincipal,
                    domain: ActiveDomain) -> dict[str, Any]:
    req = db.get(DecisionRequest, request_id)
    if not _request_visible(principal, domain, req):
        raise NotFound("decision request not found")
    req = decision_requests.decline_request(db, request_id, actor_user_id=principal.user.id, note=body.note)
    db.commit()
    return decision_requests.request_view(req)


# ---------------------------------------------------------------- policy


@router.get("/api/policy/snapshot")
def get_snapshot(db: DB, principal: CurrentPrincipal) -> dict[str, Any]:
    snap = snapshot.active_snapshot(db)
    current_hash = content_hash(snapshot.compute_content(db))
    return {
        "active": snapshot.snapshot_view(snap, decision_filter=_scoped_refs(principal)),
        "status": "ACTIVE" if snap else "NOT_RECORDED",
        "current_content_hash": current_hash,
        # Drift: policy files/decisions changed since the active snapshot was recorded.
        "drift": bool(snap and snap.content_hash != current_hash),
        "remedy": None if snap else "aeos seed --only aeos.policy.seed",
        "mci_status": snapshot.mci_reference()["status"],
        "checked_at": _now(), "source": "policy/*.yaml + ACTIVE POLICY/EXCEPTION decisions",
    }


@router.post("/api/policy/snapshot/refresh")
def refresh_snapshot(db: DB, principal: AdminPrincipal) -> dict[str, Any]:
    snap = snapshot.ensure_active_snapshot(db, source=f"refresh by {principal.user.username}")
    db.commit()
    return {"active": snapshot.snapshot_view(snap, decision_filter=_scoped_refs(principal)), "status": "ACTIVE",
            "checked_at": _now()}


@router.get("/api/policy/mci")
def get_mci(_principal: CurrentPrincipal) -> dict[str, Any]:
    return {**snapshot.mci_reference(), "risk_classes": snapshot.risk_classes(), "checked_at": _now()}


@router.get("/api/policy/office")
def get_office_policy(db: DB, principal: CurrentPrincipal) -> dict[str, Any]:
    view = {**office.office_policy(db), "checked_at": _now()}
    if principal.active_domain != Domain.OFFICE or not principal.can_access_domain(Domain.OFFICE):
        # The effective (default-deny) policy is visible everywhere; the OFFICE decision references
        # behind it are OFFICE metadata and stay in OFFICE sessions (§35, §50).
        view.update(authorization_decision_id=None, applied_decision_ids=[], ignored_decisions=[],
                    decision_refs_hidden=True)
    return view


class OfficeRouteIn(BaseModel):
    route_key: str = Field(min_length=1, max_length=128)
    credential_ref: str = Field(min_length=8, max_length=200)
    rationale: str = Field(min_length=10, max_length=2000)


@router.post("/api/policy/office/routes", status_code=201)
def authorise_office_route(body: OfficeRouteIn, db: DB, principal: AdminPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    """§23 controlled path: make a company-approved worker route OFFICE-eligible (OFFICE session, SUPER_ADMIN,
    ACTIVE company authorisation). Activation of the route remains a separate, hash-bound owner decision."""
    from aeos.core.errors import PolicyDenied
    from aeos.policy import office_routes

    if domain != Domain.OFFICE:
        raise PolicyDenied("OFFICE route eligibility is decided in an OFFICE session", code="DOMAIN_MISMATCH")
    d = office_routes.authorise_office_route(db, route_key=body.route_key, credential_ref=body.credential_ref,
                                             actor_user_id=principal.user.id, rationale=body.rationale)
    db.commit()
    return {"decision": registry.decision_view(db, d), "checked_at": _now()}


@router.get("/api/policy/write-enablement")
def get_write_enablement(db: DB, _principal: CurrentPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    return {"items": enablement.status(db, domain.value), "domain": domain.value, "source": "system_settings + decisions",
            "checked_at": _now(), "mode": "LIVE"}


# ---------------------------------------------------------------- ADRs


@router.get("/api/adrs")
def list_adrs(db: DB, _principal: CurrentPrincipal) -> dict[str, Any]:
    rows = list(db.scalars(select(ArchitectureDecisionRecord).order_by(ArchitectureDecisionRecord.adr_key)))
    return {"items": [adr_loader.adr_view(r) for r in rows], "source": "docs/decisions (loaded by aeos seed)",
            "checked_at": _now(), "mode": "LIVE",
            "remedy": None if rows else "aeos seed --only aeos.policy.seed"}


@router.get("/api/adrs/{adr_key}")
def get_adr(adr_key: str, db: DB, principal: CurrentPrincipal) -> dict[str, Any]:
    row = db.scalar(select(ArchitectureDecisionRecord).where(ArchitectureDecisionRecord.adr_key == adr_key))
    if row is None:
        raise NotFound("ADR not found")
    view = adr_loader.adr_view(row, full=True)
    linked = list(db.scalars(select(Decision).where(Decision.actor_user_id == f"owner:{adr_key}")))
    active = principal.active_domain
    # ADRs are platform documents (no domain); decisions linked to them stay in their own domain scope.
    visible = [d for d in linked if d.domain is None
               or (active is not None and principal.can_access_domain(active) and _visible(principal, active, d))]
    view["linked_decisions"] = [{"id": d.id, "kind": d.kind, "status": registry.effective_status(d)} for d in visible]
    return view
