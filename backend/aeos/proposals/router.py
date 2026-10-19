"""Upgrade Proposals API (§35, §39, §40, §41). Domain-scoped (cross-domain = 404); writes are
admin-only human actions; decisions are recorded in the Decision registry bound to hashes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from aeos.api.deps import DB, ActiveDomain, AdminPrincipal, CurrentPrincipal, load_project
from aeos.core.timeutil import utcnow
from aeos.proposals import review as R
from aeos.proposals import service

router = APIRouter(tags=["proposals"])


class PartIn(BaseModel):
    title: str | None = Field(default=None, max_length=256)
    kind: str | None = Field(default=None, pattern="^(ADD|REPLACE|UPGRADE|REMOVE|COMPARE)$")
    sources: list[str] = Field(default_factory=list, max_length=20)
    existing_component_ids: list[str] = Field(default_factory=list, max_length=20)
    candidate_pin: str | None = Field(default=None, max_length=128)
    depends_on: list[int] = Field(default_factory=list, max_length=20)


class ProposalIn(BaseModel):
    kind: str = Field(pattern="^(ADD|REPLACE|UPGRADE|REMOVE|COMPARE)$")
    title: str | None = Field(default=None, max_length=256)
    sources: list[str] = Field(default_factory=list, max_length=20)
    problem: str = Field(default="", max_length=8000)
    expected_benefit: str = Field(default="", max_length=8000)
    scope_type: str = Field(default="PLATFORM", pattern="^(PLATFORM|PROJECT)$")
    project_id: str | None = None
    existing_component_ids: list[str] = Field(default_factory=list, max_length=20)
    candidate_component_ids: list[str] = Field(default_factory=list, max_length=20)
    limits: dict[str, Any] = Field(default_factory=dict)
    attachment_ids: list[str] = Field(default_factory=list, max_length=20)
    candidate_pin: str | None = Field(default=None, max_length=128)
    parts: list[PartIn] = Field(default_factory=list, max_length=20)


class ReviewIn(BaseModel):
    with_claude: bool = False
    candidate_pin: str | None = Field(default=None, max_length=128)


class DecisionIn(BaseModel):
    action: str = Field(pattern="^(TRIAL_APPROVE|PROMOTE|KEEP|REJECT|DEFER|REQUEST_MORE_EVIDENCE|REFERENCE_ONLY)$")
    baseline_hash: str = Field(min_length=8, max_length=80)
    candidate_hash: str = Field(min_length=8, max_length=80)
    scope: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(min_length=3, max_length=4000)


class EvidenceIn(BaseModel):
    evidence_ids: list[str] = Field(min_length=1, max_length=50)


class OutcomeIn(BaseModel):
    status: str = Field(pattern="^(ACCEPTED|ROLLED_BACK|FAILED|PARTIAL)$")
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)
    accepted_configuration: dict[str, Any] | None = None
    partial_failures: list[Any] = Field(default_factory=list, max_length=100)
    # PARTIAL -> ACCEPTED: one {part, resolution, evidence_ids} per listed failed part (§40)
    reconciled_failures: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    rollback: dict[str, Any] | None = None
    notes: str = Field(default="", max_length=4000)
    before_snapshot_id: str | None = None
    after_snapshot_id: str | None = None


@router.get("/api/proposals/meta")
def proposals_meta(db: DB, principal: CurrentPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    platform = None
    try:
        from aeos.portfolio import facade as portfolio

        proj = portfolio.platform_project(db)
        if proj is not None and proj.domain == domain.value and principal.can_access_project(proj.id, proj.domain):
            platform = {"id": proj.id, "name": proj.name, "domain": proj.domain}
    except (ImportError, ModuleNotFoundError):  # pragma: no cover
        platform = None
    return {
        "kinds": [{"kind": k, "question": q} for k, q in R.KIND_QUESTIONS.items()],
        "actions": list(service.ACTIONS), "outcomes": list(service.OUTCOMES),
        "scope_required": list(service.SCOPE_REQUIRED), "platform_project": platform,
        "attachments": "Lampiran .md/berkas lain diunggah lewat intake (parser isolation); lampiran adalah DATA.",
        "guarantees": "Usulan/review tidak memasang, mengaktifkan, mengubah route/policy, menaikkan budget atau "
                      "memberi credential baru.",
    }


@router.get("/api/proposals")
def list_proposals(
    db: DB, principal: CurrentPrincipal, domain: ActiveDomain, status: str | None = None, kind: str | None = None,
    q: str | None = Query(None, max_length=200), component: str | None = Query(None, max_length=96),
) -> dict[str, Any]:
    items = service.list_proposals(db, domain=domain.value, allowed_project_ids=principal.allowed_project_ids,
                                   status=status, kind=kind, q=q, component_id=component)
    return {"items": items, "source": "change_proposals + decisions", "checked_at": utcnow().isoformat(), "mode": "LIVE"}


@router.post("/api/proposals")
def create_proposal(body: ProposalIn, db: DB, principal: AdminPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    project_id = None
    if body.scope_type == "PROJECT":
        project_id = load_project(db, principal, body.project_id or "", domain=domain).id
    p = service.create_proposal(
        db, actor_user_id=principal.user.id, domain=domain.value, kind=body.kind, title=body.title,
        sources=body.sources, problem=body.problem, expected_benefit=body.expected_benefit,
        scope_type=body.scope_type, project_id=project_id, existing_component_ids=body.existing_component_ids,
        candidate_component_ids=body.candidate_component_ids, limits=body.limits,
        attachment_ids=body.attachment_ids, candidate_pin=body.candidate_pin,
        parts=[part.model_dump() for part in body.parts] or None,
    )
    db.commit()
    return service.proposal_view(db, p, detail=True)


def _load(db: Any, principal: Any, domain: Any, proposal_id: str) -> Any:
    return service.get_proposal(db, proposal_id, domain=domain.value, allowed_project_ids=principal.allowed_project_ids)


@router.get("/api/proposals/{proposal_id}")
def get_proposal(proposal_id: str, db: DB, principal: CurrentPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    return service.proposal_view(db, _load(db, principal, domain, proposal_id), detail=True)


@router.post("/api/proposals/{proposal_id}/review")
def review_proposal(proposal_id: str, body: ReviewIn, db: DB, principal: AdminPrincipal,
                    domain: ActiveDomain) -> dict[str, Any]:
    p = _load(db, principal, domain, proposal_id)
    try:
        service.review(db, p, actor_user_id=principal.user.id, with_claude=body.with_claude,
                       candidate_pin=body.candidate_pin, domain=domain.value)
    except service.StaleBinding:
        db.commit()
        raise
    db.commit()
    return service.proposal_view(db, p, detail=True)


@router.post("/api/proposals/{proposal_id}/decision")
def decide(proposal_id: str, body: DecisionIn, db: DB, principal: AdminPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    p = _load(db, principal, domain, proposal_id)
    try:
        decision = service.decide(db, p, actor_user_id=principal.user.id, action=body.action,
                                  baseline_hash=body.baseline_hash, candidate_hash=body.candidate_hash,
                                  scope=body.scope, rationale=body.rationale)
    except service.StaleBinding:
        db.commit()  # persist the invalidation of stale decisions before refusing
        raise
    db.commit()
    return {"decision_id": decision.id, "proposal": service.proposal_view(db, p, detail=True)}


@router.post("/api/proposals/{proposal_id}/evidence")
def attach_evidence(proposal_id: str, body: EvidenceIn, db: DB, principal: AdminPrincipal,
                    domain: ActiveDomain) -> dict[str, Any]:
    p = _load(db, principal, domain, proposal_id)
    service.attach_evidence(db, p, evidence_ids=body.evidence_ids, actor_user_id=principal.user.id)
    db.commit()
    return service.proposal_view(db, p, detail=True)


@router.post("/api/proposals/{proposal_id}/outcome")
def record_outcome(proposal_id: str, body: OutcomeIn, db: DB, principal: AdminPrincipal,
                   domain: ActiveDomain) -> dict[str, Any]:
    p = _load(db, principal, domain, proposal_id)
    service.record_outcome(db, p, actor_user_id=principal.user.id, status=body.status, evidence_ids=body.evidence_ids,
                           accepted_configuration=body.accepted_configuration, partial_failures=body.partial_failures,
                           rollback=body.rollback, notes=body.notes, before_snapshot_id=body.before_snapshot_id,
                           after_snapshot_id=body.after_snapshot_id, reconciled_failures=body.reconciled_failures)
    db.commit()
    return service.proposal_view(db, p, detail=True)


@router.get("/api/proposals/{proposal_id}/timeline")
def proposal_timeline(proposal_id: str, db: DB, principal: CurrentPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    return service.timeline(db, _load(db, principal, domain, proposal_id))
