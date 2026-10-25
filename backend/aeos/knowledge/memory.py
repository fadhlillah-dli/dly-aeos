"""Long-term memory (§24 "Long-term memory", §13 MemoryRecord, §50 memory rows).

* proposals come only from accepted facts (an ACTIVE Decision, an approved PlanVersion, an ACCEPTED
  ADR/AcceptanceRecord, or an explicit human statement) — never from attachments, worker/model
  output or research, which stay DATA;
* content with secrets or instruction-injection markers is refused;
* acceptance and rejection are human decisions recorded through ``aeos.policy.facade.record_decision``;
  acceptance fails closed when the decision registry is unavailable;
* partitioned by domain; DOMAIN-scope (cross-project) memory only for PUBLIC/INTERNAL data;
  CONFIDENTIAL/RESTRICTED never leaves its project;
* expiry and supersession keep history (no deletes).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.errors import Blocked, Conflict, NotFound, PolicyDenied, ValidationFailed
from aeos.core.hashing import content_hash
from aeos.core.redaction import contains_secret
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.events import service as events
from aeos.identity.service import Principal
from aeos.intake.scan import UNTRUSTED_INSTRUCTION, scan_text
from aeos.models.decisions import ArchitectureDecisionRecord, Decision
from aeos.models.evidence import AcceptanceRecord
from aeos.models.planning import Plan, PlanVersion
from aeos.models.portfolio import Project
from aeos.models.research import MemoryRecord
from aeos.portfolio import classification as cls
from aeos.portfolio.service import deny, require_writable

ACCEPTED_SOURCES = ("decision", "plan_version", "adr", "acceptance", "human_statement")
UNTRUSTED_SOURCES = frozenset({"attachment", "worker_output", "model_output", "research", "external", "tool_output",
                               "review_finding", "memory_suggestion"})
SCOPES = ("PROJECT", "DOMAIN")
STATUSES = ("PROPOSED", "ACCEPTED", "REJECTED", "EXPIRED", "SUPERSEDED")
MAX_CONTENT = 4000


def _hash(rec: MemoryRecord) -> str:
    return content_hash({"content": rec.content, "scope": rec.scope, "classification": rec.classification,
                         "project_id": rec.project_id, "domain": rec.domain})


def _validate_source(session: Session, project: Project, source_type: str, source_id: str | None) -> dict[str, Any]:
    """Return provenance for an accepted source or raise PolicyDenied/NotFound."""
    if source_type == "human_statement":
        return {"source_type": source_type, "source_id": None}
    if not source_id:
        raise ValidationFailed("source_id is required for this source type", code="SOURCE_REQUIRED")
    if source_type == "decision":
        d = session.get(Decision, source_id)
        if d is None or (d.project_id not in (None, project.id)) or (d.domain not in (None, project.domain)):
            raise NotFound("decision not found")
        if d.status != "ACTIVE":
            raise PolicyDenied(f"decision is {d.status}; only ACTIVE decisions can become memory",
                               code="MEMORY_SOURCE_NOT_ACCEPTED")
        return {"source_type": source_type, "source_id": d.id, "source_hash": d.target_hash, "decision_kind": d.kind}
    if source_type == "plan_version":
        pv = session.get(PlanVersion, source_id)
        if pv is None or pv.project_id != project.id or pv.domain != project.domain:
            raise NotFound("plan version not found")
        plan = session.get(Plan, pv.plan_id)
        if plan is None or plan.approved_version_id != pv.id:
            raise PolicyDenied("plan version is not the approved version", code="MEMORY_SOURCE_NOT_ACCEPTED")
        return {"source_type": source_type, "source_id": pv.id, "source_hash": pv.manifest_hash}
    if source_type == "adr":
        adr = session.scalar(select(ArchitectureDecisionRecord).where(
            or_(ArchitectureDecisionRecord.id == source_id, ArchitectureDecisionRecord.adr_key == source_id)))
        if adr is None:
            raise NotFound("ADR not found")
        if not project.is_platform:
            raise PolicyDenied("platform ADRs become memory only for the platform project", code="MEMORY_SCOPE_DENIED")
        if adr.status != "ACCEPTED":
            raise PolicyDenied(f"ADR is {adr.status}; only ACCEPTED ADRs can become memory",
                               code="MEMORY_SOURCE_NOT_ACCEPTED")
        return {"source_type": source_type, "source_id": adr.adr_key, "source_hash": adr.content_hash}
    if source_type == "acceptance":
        acc = session.get(AcceptanceRecord, source_id)
        if acc is None or acc.project_id != project.id:
            raise NotFound("acceptance record not found")
        if acc.acceptance_status != "ACCEPTED":
            raise PolicyDenied(f"acceptance is {acc.acceptance_status}", code="MEMORY_SOURCE_NOT_ACCEPTED")
        return {"source_type": source_type, "source_id": acc.id, "source_hash": acc.artifact_hash}
    raise ValidationFailed(f"unknown source_type {source_type!r}", code="INVALID_SOURCE_TYPE",
                           details={"allowed": list(ACCEPTED_SOURCES)})


def propose(
    session: Session, principal: Principal, project: Project, *, content: str, source_type: str,
    source_id: str | None = None, scope: str = "PROJECT", classification: str | None = None,
    expires_at: datetime | None = None, supersedes_id: str | None = None,
) -> MemoryRecord:
    action = "knowledge.memory.propose"
    require_writable(project)
    text = (content or "").strip()
    if not 3 <= len(text) <= MAX_CONTENT:
        raise ValidationFailed(f"content must be 3..{MAX_CONTENT} characters", code="INVALID_CONTENT")
    st = (source_type or "").strip().lower()
    if st in UNTRUSTED_SOURCES:
        deny(session, principal, action=action, code="MEMORY_SOURCE_UNTRUSTED", project=project,
             reason=f"{st} is untrusted DATA; only accepted facts/decisions can become memory (§24)")
    if contains_secret(text):
        deny(session, principal, action=action, code="MEMORY_SECRET", project=project,
             reason="memory must never contain secrets (§24, U-13)")
    if any(f["kind"] == UNTRUSTED_INSTRUCTION for f in scan_text(text, source="memory").findings):
        deny(session, principal, action=action, code="MEMORY_UNTRUSTED_INSTRUCTION", project=project,
             reason="instruction-like text cannot become memory or policy (§24, §25)")
    scope_v = (scope or "PROJECT").strip().upper()
    if scope_v not in SCOPES:
        raise ValidationFailed("scope must be PROJECT or DOMAIN", code="INVALID_SCOPE")
    level = cls.at_least(classification, project.data_classification)
    if scope_v == "DOMAIN" and level in cls.SENSITIVE:
        deny(session, principal, action=action, code="MEMORY_CROSS_PROJECT_SENSITIVE", project=project,
             reason=f"{level} memory cannot be shared across projects (§24)")
    if expires_at is not None and ensure_aware(expires_at) <= utcnow():  # type: ignore[operator]
        raise ValidationFailed("expires_at must be in the future", code="INVALID_EXPIRY")
    provenance = _validate_source(session, project, st, source_id)
    if supersedes_id:
        old = session.get(MemoryRecord, supersedes_id)
        if old is None or old.project_id != project.id or old.domain != project.domain:
            raise NotFound("memory record to supersede not found")
        if old.status != "ACCEPTED":
            raise Conflict("only ACCEPTED memory can be superseded", code="INVALID_SUPERSEDE")
        provenance["supersedes"] = old.id
    provenance.update({"proposed_by": principal.user.id, "proposed_at": utcnow().isoformat()})
    rec = MemoryRecord(project_id=project.id, domain=project.domain, scope=scope_v, content=text,
                       provenance=provenance, classification=level, status="PROPOSED", expires_at=expires_at)
    session.add(rec)
    session.flush()
    audit.record(session, actor_type="user", actor_id=principal.user.id, action=action, target_type="memory_record",
                 target_id=rec.id, project_id=project.id, domain=project.domain,
                 details={"source_type": st, "source_id": provenance.get("source_id"), "scope": scope_v,
                          "classification": level, "supersedes": provenance.get("supersedes")})
    events.emit(session, source="knowledge", kind="knowledge.memory.proposed", entity_type="memory_record",
                entity_id=rec.id, project_id=project.id, domain=project.domain, payload={"scope": scope_v})
    return rec


def _record_decision(session: Session, principal: Principal, rec: MemoryRecord, *, kind: str, operation: str,
                     rationale: str, extra: dict[str, Any] | None = None) -> Decision:
    from aeos.policy import facade as policy

    return policy.record_decision(
        session, kind=kind, actor_user_id=principal.user.id, target_type="memory_record", target_id=rec.id,
        target_hash=_hash(rec), allowed_operations=[operation], auth_scope={"project_id": rec.project_id},
        project_id=rec.project_id, domain=rec.domain, rationale=rationale,
        details={"scope": rec.scope, "classification": rec.classification, **(extra or {})},
    )


def system_change(session: Session, rec: MemoryRecord, eval_run_ids: list[str] | None) -> dict[str, Any]:
    """§41/§43 "prompt/memory ... tetap system change dengan eval/decision/rollback": accepted memory reaches model
    prompts (through context packs), so its acceptance records the rollback target - expiring this record, and the
    previously accepted record it supersedes - and the system-change evaluation: EVALUATED with completed,
    all-gates-PASS evaluation runs bound to exactly this record (the evaluated configuration's ``context_params`` name
    the record id or its content hash; any other run is SYSTEM_CHANGE_EVIDENCE_UNRELATED), otherwise NOT_EVALUATED
    (stated, never implied)."""
    prov = rec.provenance or {}
    previous = prov.get("supersedes")
    rollback_target = f"memory_record:{previous}" if previous else f"expire memory_record:{rec.id} (no previous record)"
    rollback = {"how": f"POST /api/memory/{rec.id}/expire", "previous_memory_id": previous,
                "rollback_target": rollback_target}
    if eval_run_ids:
        from aeos.policy import facade as policy

        out = policy.system_change_evidence(session, domain=rec.domain, eval_run_ids=list(eval_run_ids),
                                            rollback_target=rollback_target,
                                            change={"target_type": "memory_record", "target_id": rec.id,
                                                    "target_version": None, "target_hash": _hash(rec)})
        return {**out, "class": "MEMORY", "rollback": rollback}
    return {"class": "MEMORY", "evaluation": "NOT_EVALUATED", "eval_runs": [], "rollback_target": rollback_target,
            "rollback": rollback,
            "note": "Diterima tanpa evaluasi system-change; memori masuk context pack sebagai DATA SUBORDINATE "
                    "dan dapat di-rollback (expire)."}


def accept(session: Session, principal: Principal, project: Project, rec: MemoryRecord, *, rationale: str = "",
           eval_run_ids: list[str] | None = None) -> MemoryRecord:
    if rec.status != "PROPOSED":
        raise Conflict(f"memory is {rec.status}; only PROPOSED memory can be accepted", code="INVALID_TRANSITION")
    require_writable(project)  # reject/expire stay possible: they never grant anything
    if contains_secret(rec.content):  # registry may know new secret values since the proposal
        deny(session, principal, action="knowledge.memory.accept", code="MEMORY_SECRET", project=project,
             target_type="memory_record", target_id=rec.id, reason="memory content now matches a secret")
    prov = rec.provenance or {}
    _validate_source(session, project, str(prov.get("source_type")), prov.get("source_id"))  # still accepted?
    if rec.expires_at is not None and ensure_aware(rec.expires_at) <= utcnow():  # type: ignore[operator]
        raise Conflict("memory proposal already expired", code="MEMORY_EXPIRED")
    change = system_change(session, rec, eval_run_ids)
    try:
        decision = _record_decision(session, principal, rec, kind="APPROVAL", operation="memory.accept",
                                    rationale=rationale or "accept memory record", extra={"system_change": change})
    except (ImportError, ModuleNotFoundError) as exc:
        raise Blocked("decision registry (aeos.policy) unavailable: memory cannot be accepted without a recorded "
                      "human decision", code="DECISION_REGISTRY_UNAVAILABLE") from exc
    rec.status = "ACCEPTED"
    rec.auth_decision_id = decision.id
    rec.provenance = {**prov, "system_change": change}
    superseded = None
    if prov.get("supersedes"):
        old = session.get(MemoryRecord, prov["supersedes"])
        if old is not None and old.status == "ACCEPTED":
            old.status = "SUPERSEDED"
            old.superseded_by = rec.id
            superseded = old.id
    session.flush()
    audit.record(session, actor_type="user", actor_id=principal.user.id, action="knowledge.memory.accept",
                 target_type="memory_record", target_id=rec.id, project_id=rec.project_id, domain=rec.domain,
                 details={"decision_id": decision.id, "superseded": superseded})
    events.emit(session, source="knowledge", kind="knowledge.memory.accepted", entity_type="memory_record",
                entity_id=rec.id, project_id=rec.project_id, domain=rec.domain, payload={"superseded": superseded})
    return rec


def reject(session: Session, principal: Principal, project: Project, rec: MemoryRecord, *, rationale: str = "") -> MemoryRecord:
    """Rejection never grants anything, so it proceeds (audited) even if the registry is absent."""
    if rec.status != "PROPOSED":
        raise Conflict(f"memory is {rec.status}; only PROPOSED memory can be rejected", code="INVALID_TRANSITION")
    decision_id = None
    registry = "RECORDED"
    try:
        decision_id = _record_decision(session, principal, rec, kind="REJECTION", operation="memory.reject",
                                       rationale=rationale or "reject memory record").id
    except (ImportError, ModuleNotFoundError):
        registry = "UNAVAILABLE"
    rec.status = "REJECTED"
    rec.auth_decision_id = decision_id
    session.flush()
    audit.record(session, actor_type="user", actor_id=principal.user.id, action="knowledge.memory.reject",
                 target_type="memory_record", target_id=rec.id, project_id=rec.project_id, domain=rec.domain,
                 reason=rationale, details={"decision_id": decision_id, "decision_registry": registry})
    events.emit(session, source="knowledge", kind="knowledge.memory.rejected", entity_type="memory_record",
                entity_id=rec.id, project_id=rec.project_id, domain=rec.domain)
    return rec


def expire(session: Session, principal: Principal, project: Project, rec: MemoryRecord, *, reason: str = "") -> MemoryRecord:
    if rec.status not in {"PROPOSED", "ACCEPTED"}:
        raise Conflict(f"memory is {rec.status}; nothing to expire", code="INVALID_TRANSITION")
    rec.status = "EXPIRED"
    rec.expires_at = utcnow()
    session.flush()
    audit.record(session, actor_type="user", actor_id=principal.user.id, action="knowledge.memory.expire",
                 target_type="memory_record", target_id=rec.id, project_id=rec.project_id, domain=rec.domain,
                 reason=reason)
    events.emit(session, source="knowledge", kind="knowledge.memory.expired", entity_type="memory_record",
                entity_id=rec.id, project_id=rec.project_id, domain=rec.domain)
    return rec


def get_scoped(session: Session, project: Project, memory_id: str) -> MemoryRecord:
    rec = session.get(MemoryRecord, memory_id)
    if rec is None or rec.project_id != project.id or rec.domain != project.domain:
        raise NotFound("memory record not found")
    return rec


def list_for_project(session: Session, project: Project, *, status: str | None = None) -> list[MemoryRecord]:
    q = select(MemoryRecord).where(MemoryRecord.project_id == project.id, MemoryRecord.domain == project.domain)
    if status:
        q = q.where(MemoryRecord.status == status.upper())
    return list(session.scalars(q.order_by(MemoryRecord.created_at.desc())))


def usable_for_context(session: Session, project: Project, *,
                       allowed_project_ids: list[str] | None = None) -> list[MemoryRecord]:
    """ACCEPTED, unexpired memory visible to *project*: its own records plus non-sensitive DOMAIN-scope
    records of the same domain. Never another domain; never another project's sensitive data.

    *allowed_project_ids* is the requesting principal's project scope (``None`` = all projects of the
    domain): DOMAIN-scope records owned by a project outside that scope are not returned, so a
    project-scoped user cannot read another project's memory through a context pack."""
    now = utcnow()
    shared = (MemoryRecord.scope == "DOMAIN") & MemoryRecord.classification.not_in(tuple(cls.SENSITIVE))
    if allowed_project_ids is not None:
        shared = shared & MemoryRecord.project_id.in_(allowed_project_ids)
    q = select(MemoryRecord).where(
        MemoryRecord.domain == project.domain,
        MemoryRecord.status == "ACCEPTED",
        or_(MemoryRecord.expires_at.is_(None), MemoryRecord.expires_at > now),
        or_(MemoryRecord.project_id == project.id, shared),
    ).order_by(MemoryRecord.created_at.desc())
    return list(session.scalars(q))


def memory_view(rec: MemoryRecord) -> dict[str, Any]:
    expired = rec.expires_at is not None and ensure_aware(rec.expires_at) <= utcnow()  # type: ignore[operator]
    return {
        "id": rec.id,
        "project_id": rec.project_id,
        "domain": rec.domain,
        "scope": rec.scope,
        "content": rec.content,
        "provenance": rec.provenance or {},
        "classification": rec.classification,
        "status": "EXPIRED" if expired and rec.status in {"PROPOSED", "ACCEPTED"} else rec.status,
        "auth_decision_id": rec.auth_decision_id,
        "expires_at": rec.expires_at.isoformat() if rec.expires_at else None,
        "superseded_by": rec.superseded_by,
        # §41/§43 system change: evaluation status + rollback target of the acceptance (None while PROPOSED)
        "system_change": (rec.provenance or {}).get("system_change"),
        "created_at": rec.created_at.isoformat(),
        "updated_at": rec.updated_at.isoformat(),
        "trust": "MEMORY_DATA (never policy)",
    }
