"""Acceptance records (§13, §14, §34, U-14).

An AcceptanceRecord is ACCEPTED only when **both**

1. the independent verifier PASSed with trusted, LIVE evidence for the exact head that is accepted, and
2. a COMPLETED, non-MOCK Claude checkpoint review bound to the same artifact hash returned
   PASS-CONTROLLED or better (PASS-STATIC only for docs-only changes whose TaskSpec explicitly allows
   static evidence — risk class R0 or an evidence requirement ``PASS-STATIC``/``STATIC``/``DOCS_ONLY``).

The record binds artifact hash (tree + diff), base/head commits, config hash (grader/lint/verifier
identity), PlanVersion hash and TaskSpec hash. Binding columns are immutable (DB trigger). When any
of these identities changes — a PR gets new commits, its base moves, the grader or plan changes —
the record becomes STALE and re-verification/review is required (§14, §50 "PR baru mengubah head
atau base terkait"). This is a deterministic code decision, not a human approval: merge to main
remains a human action.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.enums import ExecutionState
from aeos.core.errors import Blocked, Conflict, NotFound
from aeos.core.timeutil import utcnow
from aeos.events import service as events
from aeos.models.evidence import AcceptanceRecord, ReviewRecord
from aeos.models.planning import TaskSpec
from aeos.models.portfolio import Project
from aeos.models.workforce import WorkerRun
from aeos.verification.integration import call
from aeos.verification.review import NON_REGRESSION_SCOPE, latest_review, security_review_required
from aeos.verification.verifier import current_config_hash, latest_report, report_refs, report_view

log = logging.getLogger("aeos.verification.acceptance")

ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"
STALE = "STALE"
INVALIDATED = "INVALIDATED"
ACCEPTABLE_VERDICTS = frozenset({"PASS-CONTROLLED", "PASS-STAGING", "PASS-PRODUCTION"})
STATIC_EVIDENCE_MARKERS = frozenset({"PASS-STATIC", "STATIC", "DOCS_ONLY", "E1"})
ENGINE_IDENTITY = "acceptance-engine"


def latest_acceptance(session: Session, task_id: str) -> AcceptanceRecord | None:
    return session.scalar(select(AcceptanceRecord).where(AcceptanceRecord.task_id == task_id)
                          .order_by(AcceptanceRecord.created_at.desc()).limit(1))


def acceptance_is_valid(
    session: Session, task_id: str, *, base_commit: str | None = None, config_hash: str | None = None,
    plan_version_hash: str | None = None,
) -> bool:
    rec = latest_acceptance(session, task_id)
    if rec is None or rec.acceptance_status != ACCEPTED or rec.mode != "LIVE":
        return False
    if base_commit is not None and rec.base_commit != base_commit:
        return False
    if config_hash is not None and rec.config_hash != config_hash:
        return False
    return not (plan_version_hash is not None and rec.plan_version_hash != plan_version_hash)


def _static_allowed(task: TaskSpec) -> bool:
    if task.risk_class == "R0":
        return True
    for req in task.evidence_requirements or []:
        values = [req] if isinstance(req, str) else (list(req.values()) if isinstance(req, dict) else [])
        if any(str(v).strip().upper() in STATIC_EVIDENCE_MARKERS for v in values):
            return True
    return False


def _verdict_problems(rec: ReviewRecord, label: str, *, artifact_hash: str, docs_only: bool,
                      static_allowed: bool) -> list[str]:
    out: list[str] = []
    if rec.target_hash != artifact_hash:
        out.append(f"{label} is bound to a different artifact than the verified one")
    if rec.mode == "MOCK":
        out.append(f"a MOCK {label} can never produce acceptance")
    if rec.verdict in ACCEPTABLE_VERDICTS:
        return out
    if rec.verdict == "PASS-STATIC":
        if not (docs_only and static_allowed):
            out.append(f"{label}: PASS-STATIC is accepted only for docs-only changes whose TaskSpec allows static "
                       "evidence")
        return out
    out.append(f"{label} verdict {rec.verdict} is not a pass")
    return out


def _require_completed(rec: ReviewRecord | None, *, missing_code: str, label: str) -> ReviewRecord:
    if rec is None:
        raise Blocked(f"no {label} for this run", code=missing_code)
    if rec.status != "COMPLETED":
        raise Blocked(f"{label} is {rec.status}: {rec.status_reason}", code=f"{missing_code.rsplit('_', 1)[0]}_NOT_COMPLETED",
                      details={"review_id": rec.id, "status": rec.status})
    return rec


def evaluate_acceptance(session: Session, run_id: str, *, actor: str = ENGINE_IDENTITY) -> AcceptanceRecord:
    """Decide acceptance for a reviewed run (caller commits). Missing inputs raise ``Blocked``."""
    from aeos.workforce import facade as workforce

    run = session.get(WorkerRun, run_id)
    if run is None:
        raise NotFound("run not found")
    if run.execution_status != ExecutionState.REVIEW.value:
        raise Conflict("acceptance is decided for runs in REVIEW only", code="RUN_NOT_IN_REVIEW",
                       details={"execution_status": run.execution_status})
    project = session.get(Project, run.project_id)
    task = session.get(TaskSpec, run.task_id) if run.task_id else None
    if project is None or task is None:
        raise Blocked("run has no project/TaskSpec", code="NO_TASKSPEC")
    report = latest_report(session, run.id)
    view = report_view(report)
    if report is None or view is None:
        raise Blocked("no independent verification report for this run", code="VERIFICATION_REQUIRED")
    if not view.get("artifact_hash"):
        raise Blocked("verification did not establish the artifact identity", code="VERIFICATION_INCOMPLETE")
    review = _require_completed(latest_review(session, run.id), missing_code="REVIEW_REQUIRED",
                                label="checkpoint review")
    security = None
    if security_review_required(task, view):
        security = _require_completed(latest_review(session, run.id, reviewer="SECURITY"),
                                      missing_code="SECURITY_REVIEW_REQUIRED", label="security review")

    reasons: list[str] = []
    if view["status"] != "PASS":
        reasons.append(f"verifier status is {view['status']} (PASS required)")
    if not report.trusted or report.origin != "verifier":
        reasons.append("verification evidence is not from the trusted verifier")
    if report.mode != "LIVE":
        reasons.append(f"verification evidence is {report.mode}, not LIVE")
    if run.head_commit and view.get("head_commit") != run.head_commit:
        reasons.append("verification does not cover the run's current head commit")
    docs_only, static_allowed = bool(view.get("docs_only")), _static_allowed(task)
    reasons += _verdict_problems(review, "review", artifact_hash=view["artifact_hash"], docs_only=docs_only,
                                 static_allowed=static_allowed)
    if security is not None:
        reasons += _verdict_problems(security, "security review", artifact_hash=view["artifact_hash"],
                                     docs_only=docs_only, static_allowed=static_allowed)
    verdict = review.verdict
    if view.get("impact") == "HIGH":
        # §31: high-impact changes need every non-regression dimension argued from evidence.
        for key, _question in NON_REGRESSION_SCOPE:
            status = (review.non_regression or {}).get(key, {}).get("status")
            if status not in ("COVERED", "NOT_APPLICABLE"):
                reasons.append(f"high-impact change: non-regression '{key}' is {status or 'missing'}")
    if not task.plan_version_hash:
        reasons.append("TaskSpec has no PlanVersion hash binding")

    status = REJECTED if reasons else ACCEPTED
    refs = [report.id, *report_refs(report)]
    rec = AcceptanceRecord(
        task_id=task.id, run_id=run.id, project_id=project.id, domain=project.domain,
        artifact_hash=view["artifact_hash"], base_commit=view.get("base_commit"), head_commit=view.get("head_commit"),
        config_hash=view.get("config_hash"), plan_version_hash=task.plan_version_hash, task_spec_hash=task.spec_hash,
        verification_evidence_id=report.id, verifier_evidence_refs=refs[:100], review_record_id=review.id,
        security_review_id=security.id if security else None, review_verdict=verdict, acceptance_status=status,
        reasons=reasons, decided_by=actor[:64],
        mode="LIVE" if report.mode == "LIVE" and review.mode != "MOCK" and (security is None or security.mode != "MOCK")
        else "MOCK",
    )
    if status == ACCEPTED:
        for prev in session.scalars(select(AcceptanceRecord).where(AcceptanceRecord.task_id == task.id,
                                                                   AcceptanceRecord.acceptance_status == ACCEPTED)):
            prev.acceptance_status = INVALIDATED
            prev.invalidated_at = utcnow()
            prev.invalidated_reason = "superseded by a newer acceptance decision"
    session.add(rec)
    session.flush()
    notes: list[str] = []
    upd = call(workforce.update_run, session, run.id, actor=ENGINE_IDENTITY, acceptance_status=status)
    if not upd.ok:
        notes.append(f"run acceptance_status update {upd.label()}")
    if status == ACCEPTED:
        mark = call(workforce.mark_accepted, session, task.id, run.id, acceptance_id=rec.id, actor=ENGINE_IDENTITY)
        if not mark.ok:
            notes.append(f"task DONE transition {mark.label()}")
    audit.record(session, actor_type="system", actor_id=actor, action="verification.acceptance.decide",
                 result="SUCCESS", target_type="task", target_id=task.id, project_id=project.id, domain=project.domain,
                 reason="; ".join(reasons)[:500],
                 details={"acceptance_id": rec.id, "status": status, "run_id": run.id, "verdict": verdict,
                          "artifact_hash": rec.artifact_hash, "head": rec.head_commit, "notes": notes})
    events.emit(session, source="verifier", kind="acceptance.decided", entity_type="acceptance", entity_id=rec.id,
                entity_version=rec.artifact_hash, project_id=project.id, domain=project.domain, run_id=run.id,
                attempt=run.attempt, mode=rec.mode, payload={"status": status, "task_id": task.id, "notes": notes})
    return rec


def staleness(session: Session, rec: AcceptanceRecord) -> list[str]:
    """Reasons why an ACCEPTED record no longer matches the current identities (read-only)."""
    out: list[str] = []
    run = session.get(WorkerRun, rec.run_id)
    if run is not None and run.head_commit and rec.head_commit and run.head_commit != rec.head_commit:
        out.append(f"run head changed {rec.head_commit[:12]} -> {run.head_commit[:12]}")
    task = session.get(TaskSpec, rec.task_id)
    if task is not None:
        if task.plan_version_hash != rec.plan_version_hash:
            out.append("PlanVersion hash differs from the accepted binding")
        current = current_config_hash(task)
        if current is None:
            out.append("current verifier configuration UNKNOWN (trusted suite unreadable)")
        elif rec.config_hash and current != rec.config_hash:
            out.append("verifier configuration changed (grader/lint/verifier/python/TaskSpec policy)")
    try:
        from aeos.models.delivery_ext import PullRequestRecord

        pr = session.scalar(select(PullRequestRecord).where(PullRequestRecord.acceptance_id == rec.id)
                            .order_by(PullRequestRecord.created_at.desc()).limit(1))
    except ImportError:  # pragma: no cover - delivery models are part of this module group
        pr = None
    if pr is not None:
        if pr.observed_head_sha and rec.head_commit and pr.observed_head_sha != rec.head_commit:
            out.append(f"PR head changed {rec.head_commit[:12]} -> {pr.observed_head_sha[:12]}")
        if pr.observed_base_sha and pr.initial_base_sha and pr.observed_base_sha != pr.initial_base_sha:
            out.append(f"PR base moved {pr.initial_base_sha[:12]} -> {pr.observed_base_sha[:12]}")
    return out


def acceptance_view(session: Session, rec: AcceptanceRecord) -> dict[str, Any]:
    stale = staleness(session, rec) if rec.acceptance_status == ACCEPTED else []
    return {
        "id": rec.id, "task_id": rec.task_id, "run_id": rec.run_id, "project_id": rec.project_id,
        "domain": rec.domain, "acceptance_status": rec.acceptance_status, "artifact_hash": rec.artifact_hash,
        "base_commit": rec.base_commit, "head_commit": rec.head_commit, "config_hash": rec.config_hash,
        "plan_version_hash": rec.plan_version_hash, "task_spec_hash": rec.task_spec_hash,
        "verification_evidence_id": rec.verification_evidence_id, "verifier_evidence_refs": rec.verifier_evidence_refs,
        "review_record_id": rec.review_record_id, "security_review_id": rec.security_review_id,
        "review_verdict": rec.review_verdict, "reasons": rec.reasons,
        "invalidated_reason": rec.invalidated_reason,
        "invalidated_at": rec.invalidated_at.isoformat() if rec.invalidated_at else None,
        "decided_by": rec.decided_by, "mode": rec.mode, "created_at": rec.created_at.isoformat(),
        "updated_at": rec.updated_at.isoformat(),
        "valid": rec.acceptance_status == ACCEPTED and rec.mode == "LIVE" and not stale,
        "staleness": stale,
        "is_human_approval": False,  # merge/release remain human decisions (U-09)
    }


def invalidate(
    session: Session, *, task_id: str, reason: str, actor: str, head_commit: str | None = None,
    base_commit: str | None = None, config_hash: str | None = None, plan_version_hash: str | None = None,
    to_status: str = STALE,
) -> list[AcceptanceRecord]:
    """Mark ACCEPTED/PENDING records STALE when a bound identity changed.

    With no identity arguments the invalidation is unconditional (explicit reason). Returns the
    records that changed. Re-verification/review of the new state is required afterwards.
    """
    from aeos.workforce import facade as workforce

    explicit = all(v is None for v in (head_commit, base_commit, config_hash, plan_version_hash))
    changed: list[AcceptanceRecord] = []
    rows = session.scalars(select(AcceptanceRecord).where(
        AcceptanceRecord.task_id == task_id, AcceptanceRecord.acceptance_status.in_([ACCEPTED, "PENDING"])))
    for rec in rows:
        diffs: list[str] = []
        if head_commit and rec.head_commit and head_commit != rec.head_commit:
            diffs.append(f"head {rec.head_commit[:12]} -> {head_commit[:12]}")
        if base_commit and rec.base_commit and base_commit != rec.base_commit:
            diffs.append(f"base {rec.base_commit[:12]} -> {base_commit[:12]}")
        if config_hash and rec.config_hash and config_hash != rec.config_hash:
            diffs.append("config hash changed")
        if plan_version_hash and rec.plan_version_hash and plan_version_hash != rec.plan_version_hash:
            diffs.append("plan version hash changed")
        if not diffs and not explicit:
            continue
        rec.acceptance_status = to_status
        rec.invalidated_at = utcnow()
        rec.invalidated_reason = (reason + (": " + ", ".join(diffs) if diffs else ""))[:2000]
        changed.append(rec)
        upd = call(workforce.update_run, session, rec.run_id, actor=ENGINE_IDENTITY, acceptance_status=to_status)
        audit.record(session, actor_type="system", actor_id=actor, action="verification.acceptance.invalidate",
                     result="SUCCESS", target_type="acceptance", target_id=rec.id, project_id=rec.project_id,
                     domain=rec.domain, reason=rec.invalidated_reason[:500],
                     details={"task_id": task_id, "to": to_status, "run_update": upd.label()})
        events.emit(session, source="verifier", kind="acceptance.stale", entity_type="acceptance", entity_id=rec.id,
                    project_id=rec.project_id, domain=rec.domain, run_id=rec.run_id,
                    payload={"status": to_status, "task_id": task_id, "reason": rec.invalidated_reason,
                             "required": "re-verification and review of the new state"})
    session.flush()
    return changed


def recheck(session: Session, acceptance_id: str, *, actor: str) -> AcceptanceRecord:
    """Persist staleness that ``staleness()`` detects (explicit operator/CLI action)."""
    rec = session.get(AcceptanceRecord, acceptance_id)
    if rec is None:
        raise NotFound("acceptance record not found")
    if rec.acceptance_status != ACCEPTED:
        return rec
    stale = staleness(session, rec)
    if stale:
        invalidate(session, task_id=rec.task_id, reason="recheck: " + "; ".join(stale), actor=actor)
    return rec
