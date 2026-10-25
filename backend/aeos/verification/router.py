"""Verification & Review API (§34, §35 Evidence & Decisions — evidence part).

Reads are scoped to the session's active domain (plus platform-level evidence without a domain);
cross-domain records are 404 so Office metadata never leaks into a Personal session. Writes are
SUPER_ADMIN only and commit explicitly.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from aeos.api.deps import DB, ActiveDomain, AdminPrincipal, CurrentPrincipal, load_project
from aeos.audit import service as audit
from aeos.core.enums import Domain, Role
from aeos.core.errors import AeosError, NotFound, ValidationFailed
from aeos.core.redaction import redact, redact_text
from aeos.core.timeutil import utcnow
from aeos.identity.facade import Principal
from aeos.models.evidence import AcceptanceRecord, EvidenceArtifact, ReviewRecord
from aeos.models.planning import TaskSpec
from aeos.models.portfolio import Project
from aeos.models.workforce import WorkerRun
from aeos.verification import acceptance as acceptance_mod
from aeos.verification import collector, review, verifier
from aeos.verification.facade import TRUSTED_COLLECTORS

router = APIRouter(tags=["verification"])

EVIDENCE_SOURCE = "evidence_artifacts (append-only local ledger; not a WORM/tamper-proof store — §34)"
MAX_INLINE_CONTENT = 256 * 1024


def _mode_of(modes: Any) -> str:
    """LIVE / MOCK / … for a list response; mixed lists say MIXED so fixtures are never shown as LIVE."""
    distinct = sorted(set(modes))
    return distinct[0] if len(distinct) == 1 else ("MIXED" if distinct else "LIVE")


def visible_project_ids(db: Session, principal: Principal, domain: Domain) -> list[str]:
    ids = list(db.scalars(select(Project.id).where(Project.domain == domain.value)))
    if principal.allowed_project_ids is not None:
        ids = [i for i in ids if i in principal.allowed_project_ids]
    return ids


def _evidence_scope(db: Session, principal: Principal, domain: Domain) -> Any:
    ids = visible_project_ids(db, principal, domain)
    return or_(
        EvidenceArtifact.project_id.in_(ids),
        (EvidenceArtifact.project_id.is_(None)) & ((EvidenceArtifact.domain.is_(None))
                                                   | (EvidenceArtifact.domain == domain.value)),
    )


def evidence_item(ev: EvidenceArtifact) -> dict[str, Any]:
    level_info = (ev.inline or {}).get("level") or {}
    return {
        "id": ev.id, "project_id": ev.project_id, "domain": ev.domain, "subject_type": ev.subject_type,
        "subject_id": ev.subject_id, "run_id": ev.run_id, "task_id": ev.task_id, "claim": ev.claim, "kind": ev.kind,
        "origin": ev.origin, "collector_identity": ev.collector_identity, "trusted": ev.trusted,
        "content_hash": ev.content_hash, "content_type": ev.content_type, "classification": ev.classification,
        "has_content": bool(ev.location), "size_bytes": ev.size_bytes, "evidence_level": ev.evidence_level,
        "level_explanation": collector.LEVEL_EXPLANATIONS.get(ev.evidence_level, ""),
        "level_requested": level_info.get("requested"), "level_reason": level_info.get("reason"),
        "environment": ev.environment or {}, "created_at": ev.created_at.isoformat(), "mode": ev.mode,
        "retention_policy": ev.retention_policy,
    }


def _load_evidence(db: Session, principal: Principal, domain: Domain, evidence_id: str) -> EvidenceArtifact:
    ev = db.scalar(select(EvidenceArtifact).where(EvidenceArtifact.id == evidence_id,
                                                  _evidence_scope(db, principal, domain)))
    if ev is None:
        raise NotFound("evidence not found")
    return ev


@router.get("/api/evidence")
def list_evidence(
    principal: CurrentPrincipal, domain: ActiveDomain, db: DB,
    subject_type: str | None = None, subject_id: str | None = None, run_id: str | None = None,
    task_id: str | None = None, level: str | None = None, trusted: bool | None = None, kind: str | None = None,
    origin: str | None = None, limit: int = Query(200, ge=1, le=500),
) -> dict[str, Any]:
    q = select(EvidenceArtifact).where(_evidence_scope(db, principal, domain))
    if subject_type:
        q = q.where(EvidenceArtifact.subject_type == subject_type)
    if subject_id:
        q = q.where(EvidenceArtifact.subject_id == subject_id)
    if run_id:
        q = q.where(EvidenceArtifact.run_id == run_id)
    if task_id:
        q = q.where(EvidenceArtifact.task_id == task_id)
    if level:
        if level not in collector.LEVELS:
            raise ValidationFailed("level must be E0..E4")
        q = q.where(EvidenceArtifact.evidence_level == level)
    if trusted is not None:
        q = q.where(EvidenceArtifact.trusted.is_(trusted))
    if kind:
        q = q.where(EvidenceArtifact.kind == kind.upper())
    if origin:
        q = q.where(EvidenceArtifact.origin == origin.lower())
    rows = db.scalars(q.order_by(EvidenceArtifact.created_at.desc()).limit(limit)).all()
    from aeos.operations import facade as operations

    retention = operations.RetentionMarkers(db)  # §35: policy/retention.yaml rule per record; nothing is deleted
    return {
        "items": [{**evidence_item(r), "retention": retention.evidence(r)} for r in rows],
        "source": EVIDENCE_SOURCE,
        "checked_at": utcnow().isoformat(),
        "mode": _mode_of(r.mode for r in rows),
        "level_explanations": collector.LEVEL_EXPLANATIONS,
        "trusted_collectors": sorted(TRUSTED_COLLECTORS),
    }


@router.get("/api/evidence/{evidence_id}")
def get_evidence(evidence_id: str, principal: CurrentPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    ev = _load_evidence(db, principal, domain, evidence_id)
    item = evidence_item(ev)
    from aeos.operations import facade as operations

    item["retention"] = operations.RetentionMarkers(db).evidence(ev)
    item["inline"] = ev.inline or {}
    data, integrity = collector.read_content(ev)
    item["integrity"] = integrity
    item["content"] = None
    item["content_note"] = None
    if ev.classification == "RESTRICTED" and principal.role != Role.SUPER_ADMIN:
        item["content_note"] = "RESTRICTED: isi hanya untuk SUPER_ADMIN"
    elif data is None:
        item["content_note"] = "Tidak ada isi tersimpan" if integrity == "NO_CONTENT" else f"Isi {integrity}"
    elif len(data) > MAX_INLINE_CONTENT:
        item["content_note"] = f"Isi terlalu besar untuk ditampilkan ({len(data)} byte)"
    elif ev.content_type.startswith(("text/", "application/json", "application/xml")):
        text = data.decode("utf-8", errors="replace")
        if ev.content_type == "application/json":
            try:
                item["content_json"] = redact(json.loads(text))
            except ValueError:
                pass
        item["content"] = redact_text(text)  # re-redact on read (defence in depth)
    else:
        item["content_note"] = "Isi biner tidak ditampilkan"
    item["source"] = EVIDENCE_SOURCE
    item["checked_at"] = utcnow().isoformat()
    return item


# ---------------------------------------------------------------------------- reviews


def _review_scope(db: Session, principal: Principal, domain: Domain) -> Any:
    ids = visible_project_ids(db, principal, domain)
    return or_(ReviewRecord.project_id.in_(ids),
               (ReviewRecord.project_id.is_(None)) & (ReviewRecord.domain == domain.value))


@router.get("/api/reviews")
def list_reviews(principal: CurrentPrincipal, domain: ActiveDomain, db: DB, run_id: str | None = None,
                 task_id: str | None = None, verdict: str | None = None, status: str | None = None,
                 limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    q = select(ReviewRecord).where(_review_scope(db, principal, domain))
    if run_id:
        q = q.where(ReviewRecord.run_id == run_id)
    if task_id:
        q = q.where(ReviewRecord.task_id == task_id)
    if verdict:
        q = q.where(ReviewRecord.verdict == verdict)
    if status:
        q = q.where(ReviewRecord.status == status)
    rows = db.scalars(q.order_by(ReviewRecord.created_at.desc()).limit(limit)).all()
    return {"items": [review.review_view(r) for r in rows], "source": "review_records",
            "checked_at": utcnow().isoformat(), "mode": _mode_of(r.mode for r in rows),
            "note": "Verdict reviewer saja tidak pernah sama dengan penerimaan (U-14)."}


@router.get("/api/reviews/{review_id}")
def get_review(review_id: str, principal: CurrentPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    rec = db.scalar(select(ReviewRecord).where(ReviewRecord.id == review_id, _review_scope(db, principal, domain)))
    if rec is None:
        raise NotFound("review not found")
    # an immutable review record: its claims date from created_at; generated_at = when it was read
    return {**review.review_view(rec), "source": "review_records", "generated_at": utcnow().isoformat()}


class ManualResultIn(BaseModel):
    structured: dict[str, Any] | None = None


@router.post("/api/reviews/{review_id}/manual-result")
def submit_manual_result(review_id: str, body: ManualResultIn, principal: AdminPrincipal, domain: ActiveDomain,
                         db: DB) -> dict[str, Any]:
    rec = db.scalar(select(ReviewRecord).where(ReviewRecord.id == review_id, _review_scope(db, principal, domain)))
    if rec is None:
        raise NotFound("review not found")
    try:
        outcome = review.collect_manual_review(db, rec.id, actor=principal.user.id, structured=body.structured)
    except AeosError as exc:
        audit_denied(db, principal, "verification.review.manual_result", exc, target_type="review",
                     target_id=rec.id, project_id=rec.project_id, domain=rec.domain)
        raise
    db.commit()
    return {"review": review.review_view(outcome.review), "acceptance": outcome.acceptance, "notes": outcome.notes}


# ---------------------------------------------------------------------------- runs


def _load_run(db: Session, principal: Principal, domain: Domain, run_id: str) -> WorkerRun:
    run = db.get(WorkerRun, run_id)
    if run is None:
        raise NotFound("run not found")
    load_project(db, principal, run.project_id, domain=domain)
    return run


def _run_target(run: WorkerRun) -> dict[str, Any]:
    # read before a rollback expires the instance
    return {"target_type": "worker_run", "target_id": run.id, "project_id": run.project_id, "domain": run.domain}


class VerifyIn(BaseModel):
    candidate_path: str | None = Field(default=None, max_length=1024)
    request_repair: bool = True


@router.post("/api/runs/{run_id}/verify")
def verify(run_id: str, body: VerifyIn, principal: AdminPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    run = _load_run(db, principal, domain, run_id)
    try:
        outcome = verifier.verify_run(db, run.id, actor=principal.user.id, candidate_path=body.candidate_path,
                                      request_repair_on_fail=body.request_repair)
    except AeosError as exc:
        audit_denied(db, principal, "verification.run", exc, **_run_target(run))
        raise
    db.commit()
    return outcome.to_dict()


class ReviewIn(BaseModel):
    reviewer: Literal["FEATURE", "SECURITY", "A11Y", "PRIVACY", "SRE"] = "FEATURE"


class ReviewBatchIn(BaseModel):
    kinds: list[Literal["FEATURE", "SECURITY", "A11Y", "PRIVACY", "SRE"]] | None = Field(default=None, max_length=5)


@router.get("/api/runs/{run_id}/reviews/required")
def required_review_kinds(run_id: str, principal: CurrentPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    run = _load_run(db, principal, domain, run_id)
    out = review.review_required_kinds(db, run.id)
    out.update(checked_at=utcnow().isoformat(), source="task_specs, review_records, evidence_artifacts", mode="LIVE")
    return out


@router.post("/api/runs/{run_id}/reviews/batch")
def request_reviews(run_id: str, principal: AdminPrincipal, domain: ActiveDomain, db: DB,
                    body: ReviewBatchIn | None = None) -> dict[str, Any]:
    """§19: every open independent read-only review kind (or the given ones) for the same verified package."""
    run = _load_run(db, principal, domain, run_id)
    try:
        outcomes = review.review_all(db, run.id, actor=principal.user.id, kinds=list((body or ReviewBatchIn()).kinds
                                                                                     or []) or None)
    except AeosError as exc:
        audit_denied(db, principal, "verification.review", exc, **_run_target(run))
        raise
    db.commit()
    return {"reviews": [review.review_view(o.review) for o in outcomes],
            "acceptance": next((o.acceptance for o in reversed(outcomes) if o.acceptance), None),
            "notes": [n for o in outcomes for n in o.notes]}


@router.post("/api/runs/{run_id}/review")
def request_review(run_id: str, principal: AdminPrincipal, domain: ActiveDomain, db: DB,
                   body: ReviewIn | None = None) -> dict[str, Any]:
    run = _load_run(db, principal, domain, run_id)
    try:
        outcome = review.review_run(db, run.id, actor=principal.user.id, reviewer=(body or ReviewIn()).reviewer)
    except AeosError as exc:
        audit_denied(db, principal, "verification.review", exc, **_run_target(run))
        raise
    db.commit()
    return {"review": review.review_view(outcome.review), "acceptance": outcome.acceptance, "notes": outcome.notes}


@router.post("/api/runs/{run_id}/acceptance/evaluate")
def evaluate(run_id: str, principal: AdminPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    run = _load_run(db, principal, domain, run_id)
    try:
        rec = acceptance_mod.evaluate_acceptance(db, run.id,
                                                 actor=f"acceptance-engine (requested by {principal.user.id})")
    except AeosError as exc:
        audit_denied(db, principal, "verification.acceptance.decide", exc, **_run_target(run))
        raise
    db.commit()
    return acceptance_mod.acceptance_view(db, rec)


# ---------------------------------------------------------------------------- trusted suite (non-platform projects)


class TrustedSuiteIn(BaseModel):
    rationale: str = Field(min_length=5, max_length=2000)
    commit: str | None = Field(default=None, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")


@router.post("/api/projects/{project_id}/trusted-suite", status_code=201)
def register_trusted_suite(project_id: str, body: TrustedSuiteIn, principal: AdminPrincipal, domain: ActiveDomain,
                           db: DB) -> dict[str, Any]:
    """The owner registers the project's grader (``tests/acceptance`` of a reviewed commit of the project's
    own repository) outside every candidate; bound to a POLICY decision on the suite hash (ADR-0060)."""
    from aeos.verification import suite

    project = load_project(db, principal, project_id, domain=domain)
    try:
        out = suite.register_project_suite(db, project, actor_user_id=principal.user.id, rationale=body.rationale,
                                           commit=body.commit)
    except AeosError as exc:
        audit_denied(db, principal, "verification.suite.register", exc, target_type="project", target_id=project.id,
                     project_id=project.id, domain=project.domain)
        raise
    db.commit()
    return out


@router.get("/api/projects/{project_id}/trusted-suite")
def trusted_suite_status(project_id: str, principal: CurrentPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    from aeos.verification import suite

    project = load_project(db, principal, project_id, domain=domain)
    return {**suite.suite_status(db, project), "checked_at": utcnow().isoformat(), "mode": "LIVE",
            "source": "owner POLICY decision (trusted_suite) + controller data dir copy"}


# ---------------------------------------------------------------------------- acceptance


@router.get("/api/acceptance")
def list_acceptance(principal: CurrentPrincipal, domain: ActiveDomain, db: DB, task_id: str | None = None,
                    run_id: str | None = None, limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    ids = visible_project_ids(db, principal, domain)
    q = select(AcceptanceRecord).where(AcceptanceRecord.project_id.in_(ids))
    if task_id:
        q = q.where(AcceptanceRecord.task_id == task_id)
    if run_id:
        q = q.where(AcceptanceRecord.run_id == run_id)
    rows = db.scalars(q.order_by(AcceptanceRecord.created_at.desc()).limit(limit)).all()
    return {"items": [acceptance_mod.acceptance_view(db, r) for r in rows], "source": "acceptance_records",
            "checked_at": utcnow().isoformat(), "mode": _mode_of(r.mode for r in rows),
            "note": "Penerimaan adalah keputusan kode (verifier PASS + review); merge tetap keputusan manusia."}


@router.post("/api/acceptance/{acceptance_id}/recheck")
def recheck_acceptance(acceptance_id: str, principal: AdminPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    rec = db.get(AcceptanceRecord, acceptance_id)
    if rec is None or rec.project_id not in visible_project_ids(db, principal, domain):
        raise NotFound("acceptance record not found")
    acceptance_mod.recheck(db, rec.id, actor=principal.user.id)
    db.commit()
    return acceptance_mod.acceptance_view(db, rec)


# ---------------------------------------------------------------------------- summary (separate fields, §35)


@router.get("/api/verification/summary")
def verification_summary(principal: CurrentPrincipal, domain: ActiveDomain, db: DB,
                         limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    ids = visible_project_ids(db, principal, domain)
    runs = db.scalars(select(WorkerRun).where(WorkerRun.project_id.in_(ids))
                      .order_by(WorkerRun.updated_at.desc()).limit(limit)).all()
    items = []
    for run in runs:
        task = db.get(TaskSpec, run.task_id) if run.task_id else None
        view = verifier.report_view(verifier.latest_report(db, run.id))
        rev = review.latest_review(db, run.id)
        acc = db.scalar(select(AcceptanceRecord).where(AcceptanceRecord.run_id == run.id)
                        .order_by(AcceptanceRecord.created_at.desc()).limit(1))
        acc_view = acceptance_mod.acceptance_view(db, acc) if acc else None
        items.append({
            "run_id": run.id, "task_id": run.task_id, "task_key": task.task_key if task else None,
            "task_title": task.title if task else None, "attempt": run.attempt, "project_id": run.project_id,
            "execution_status": run.execution_status, "head_commit": run.head_commit, "mode": run.mode,
            "worker_reported": {"status": run.worker_reported_status, "exit_code": run.exit_code,
                                "trust": "UNTRUSTED_CLAIM"},
            "verifier": {"status": view["status"] if view else None, "evidence_id": view["evidence_id"] if view else None,
                         "checked_at": view["created_at"] if view else None},
            "reviewer": {"verdict": rev.verdict if rev else None, "status": rev.status if rev else None,
                         "review_id": rev.id if rev else None},
            "accepted": {"status": acc_view["acceptance_status"] if acc_view else None,
                         "valid": acc_view["valid"] if acc_view else False},
            "merged": {"status": run.merged_status},
        })
    return {"items": items, "source": "worker_runs + evidence_artifacts + review_records + acceptance_records",
            "checked_at": utcnow().isoformat(), "mode": _mode_of(r.mode for r in runs)}


def audit_denied(db: Session, principal: Principal, action: str, exc: AeosError, **target: Any) -> None:
    """Persist a DENIED/FAILED audit row for a refused privileged action after rollback."""
    db.rollback()
    audit.record(db, actor_type="user", actor_id=principal.user.id, action=action,
                 result="DENIED" if exc.status_code in (403, 409) else "FAILED", reason=f"{exc.code}: {exc.message}",
                 **target)
    db.commit()
