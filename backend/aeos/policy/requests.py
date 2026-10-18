"""Decision requests: "a human decision is needed" markers that feed the single queue (U-12).

A request is DATA (it may come from a deterministic component or an agent-originated blocker); it
never grants anything. Resolving it requires a separate human Decision; declining it is recorded.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.enums import Domain
from aeos.core.errors import NotFound, ValidationFailed
from aeos.core.redaction import redact_text
from aeos.core.timeutil import utcnow
from aeos.events import service as events
from aeos.models.policy_ext import DecisionRequest
from aeos.models.portfolio import Project
from aeos.policy import catalog
from aeos.policy.textsafe import scrub

REQUESTABLE_KINDS = ("EXCEPTION", "RISK_ACCEPTANCE", "POLICY", "BUDGET", "WRITE_ENABLEMENT", "ROUTE_ACTIVATION",
                     "TRIAL", "PROMOTION")
REQUESTER_KINDS = ("SYSTEM", "HUMAN", "AGENT")


def create_request(
    session: Session, *, kind: str, title: str, reason: str, requested_by: str, requested_by_kind: str = "SYSTEM",
    project_id: str | None = None, domain: str | None = None, target_type: str = "", target_id: str = "",
    target_hash: str | None = None, requested_operations: list[str] | None = None,
    details: dict[str, Any] | None = None,
) -> DecisionRequest:
    if kind not in REQUESTABLE_KINDS:
        raise ValidationFailed(f"unsupported request kind {kind!r}", details={"allowed": list(REQUESTABLE_KINDS)})
    if requested_by_kind not in REQUESTER_KINDS:
        raise ValidationFailed("requested_by_kind must be SYSTEM, HUMAN or AGENT")
    if not title.strip() or not requested_by.strip():
        raise ValidationFailed("title and requested_by are required")
    if domain is not None:
        try:
            domain = Domain(domain).value
        except ValueError as exc:
            raise ValidationFailed(f"invalid domain {domain!r}") from exc
    if project_id is not None:
        project = session.get(Project, project_id)
        if project is None:
            raise NotFound("project not found")
        if domain is not None and domain != project.domain:
            raise ValidationFailed("request domain does not match the project domain", code="DOMAIN_MISMATCH")
        domain = project.domain
    ops = [o for o in (requested_operations or []) if isinstance(o, str)]
    req = DecisionRequest(
        kind=kind, title=redact_text(title)[:256], reason=redact_text(reason or ""),
        requested_by=requested_by[:128], requested_by_kind=requested_by_kind, project_id=project_id, domain=domain,
        target_type=target_type[:64], target_id=str(target_id)[:128], target_hash=target_hash,
        requested_operations=ops, details=scrub(details or {}), status="OPEN",
    )
    session.add(req)
    session.flush()
    audit.record(session, actor_type=requested_by_kind.lower(), actor_id=requested_by, action="decision.request.create",
                 target_type="decision_request", target_id=req.id, project_id=project_id, domain=domain,
                 details={"kind": kind, "human_only_ops_requested": sorted(set(ops) & catalog.HUMAN_ONLY_OPERATIONS)})
    events.emit(session, source="policy", kind="decision.request.created", entity_type="decision_request",
                entity_id=req.id, project_id=project_id, domain=domain, payload={"kind": kind, "title": req.title})
    return req


def resolve_request(session: Session, request_id: str, *, decision_id: str, actor_user_id: str) -> DecisionRequest:
    req = session.get(DecisionRequest, request_id)
    if req is None:
        raise NotFound("decision request not found")
    if req.status != "OPEN":
        raise ValidationFailed(f"decision request is already {req.status}")
    req.status = "RESOLVED"
    req.resolved_decision_id = decision_id
    req.resolved_at = utcnow()
    req.resolved_by = actor_user_id
    session.flush()
    events.emit(session, source="policy", kind="decision.request.resolved", entity_type="decision_request",
                entity_id=req.id, project_id=req.project_id, domain=req.domain, payload={"decision_id": decision_id})
    return req


def decline_request(session: Session, request_id: str, *, actor_user_id: str, note: str) -> DecisionRequest:
    req = session.get(DecisionRequest, request_id)
    if req is None:
        raise NotFound("decision request not found")
    if req.status != "OPEN":
        raise ValidationFailed(f"decision request is already {req.status}")
    if not note or len(note.strip()) < 5:
        raise ValidationFailed("declining a request needs a rationale")
    req.status = "DECLINED"
    req.resolved_at = utcnow()
    req.resolved_by = actor_user_id
    req.resolution_note = redact_text(note.strip())
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="decision.request.decline",
                 target_type="decision_request", target_id=req.id, project_id=req.project_id, domain=req.domain,
                 reason=req.resolution_note)
    events.emit(session, source="policy", kind="decision.request.declined", entity_type="decision_request",
                entity_id=req.id, project_id=req.project_id, domain=req.domain, payload={})
    return req


def open_requests(session: Session, *, domain: str) -> list[DecisionRequest]:
    q = select(DecisionRequest).where(DecisionRequest.status == "OPEN")
    rows = list(session.scalars(q.order_by(DecisionRequest.created_at.asc())))
    # Domain-less (platform) requests belong to the PERSONAL platform project scope (§09).
    return [r for r in rows if r.domain == domain or (r.domain is None and domain == Domain.PERSONAL.value)]


def request_view(r: DecisionRequest) -> dict[str, Any]:
    return {
        "id": r.id, "kind": r.kind, "title": r.title, "reason": r.reason, "requested_by": r.requested_by,
        "requested_by_kind": r.requested_by_kind, "project_id": r.project_id, "domain": r.domain,
        "target_type": r.target_type, "target_id": r.target_id, "target_hash": r.target_hash,
        "requested_operations": r.requested_operations or [], "details": r.details or {}, "status": r.status,
        "resolved_decision_id": r.resolved_decision_id, "resolution_note": r.resolution_note,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }
