"""Planning & Blueprint (§13-§15): PlanningRun, immutable PlanVersion, Task DAG, 4-stage Opus/Fable pipeline, approvals."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from aeos.api.deps import DB, ActiveDomain, AdminPrincipal, CurrentPrincipal, load_project
from aeos.core.enums import Domain
from aeos.core.errors import Conflict, NotFound
from aeos.core.timeutil import utcnow
from aeos.identity.service import Principal
from aeos.models.planning import Plan, PlanVersion
from aeos.planning import dag, runner, service

router = APIRouter(tags=["planning"])


def _load_plan(db: DB, principal: Principal, domain: Domain, plan_id: str) -> Plan:
    plan = db.get(Plan, plan_id)
    if plan is None or plan.domain != domain.value:
        raise NotFound("plan not found")  # cross-domain is a 404 (§35, §50)
    load_project(db, principal, plan.project_id, domain=domain)
    return plan


def _reconcile_expiry(db: DB) -> None:
    """Persist EXPIRED for approvals past ``expires_at`` before projecting plans, so an expired approval
    never shows as APPROVED (the interlock/get_approved_version already fail closed on expiry)."""
    from aeos.policy import decisions as registry

    if registry.expire_due_decisions(db):
        db.commit()


def _load_version(db: DB, plan: Plan, version_id: str) -> PlanVersion:
    v = db.get(PlanVersion, version_id)
    if v is None or v.plan_id != plan.id:
        raise NotFound("plan version not found")
    return v


class PlanCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    prompt: str = Field(min_length=1, max_length=service.MAX_PROMPT_CHARS)
    attachment_ids: list[str] = Field(default_factory=list, max_length=service.MAX_ATTACHMENTS)


@router.post("/api/projects/{project_id}/plans", status_code=201)
def create_plan(project_id: str, body: PlanCreateIn, db: DB, principal: AdminPrincipal,
                domain: ActiveDomain) -> dict[str, Any]:
    project = load_project(db, principal, project_id, domain=domain)
    plan = service.create_plan(db, project=project, domain=domain, title=body.title, prompt=body.prompt,
                               attachment_ids=body.attachment_ids, actor_user_id=principal.user.id)
    db.commit()
    return service.plan_detail(db, plan)


@router.get("/api/plans")
def list_plans(db: DB, principal: CurrentPrincipal, domain: ActiveDomain, project_id: str | None = None,
               state: str | None = None, limit: int = Query(200, ge=1, le=500)) -> dict[str, Any]:
    _reconcile_expiry(db)
    q = select(Plan).where(Plan.domain == domain.value)
    if project_id:
        load_project(db, principal, project_id, domain=domain)
        q = q.where(Plan.project_id == project_id)
    if principal.allowed_project_ids is not None:
        q = q.where(Plan.project_id.in_(principal.allowed_project_ids))
    if state:
        q = q.where(Plan.state == state)
    plans = list(db.scalars(q.order_by(Plan.updated_at.desc()).limit(limit)))
    items = [service.plan_summary(db, p) for p in plans]
    return {"items": items, "needs_decision": sum(1 for i in items if i["needs_decision"]), "source": "plans",
            "checked_at": utcnow().isoformat(), "mode": "LIVE"}


@router.get("/api/plans/{plan_id}")
def get_plan(plan_id: str, db: DB, principal: CurrentPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    plan = _load_plan(db, principal, domain, plan_id)
    _reconcile_expiry(db)
    return {**service.plan_detail(db, plan), "source": "plans, plan_versions, planning_runs, decisions",
            "checked_at": utcnow().isoformat()}


@router.post("/api/plans/{plan_id}/run", status_code=202)
def run_plan(plan_id: str, db: DB, principal: AdminPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    plan = _load_plan(db, principal, domain, plan_id)
    if runner.is_running(plan.id):
        raise Conflict("the planning pipeline is already running for this plan", code="PLANNING_IN_PROGRESS")
    service.check_runnable(db, plan)
    # OFFICE: capability + company policy gate before any model call (records NEEDS_DECISION, raises Blocked).
    service.office_gate(db, plan, actor=principal.user.id)
    # PERSONAL: CAN_PLAN_PERSONAL BLOCKED stops here too (the run re-evaluates and labels the bridge proof).
    service.personal_gate(db, plan, actor=principal.user.id)
    db.rollback()  # the gate's checkpoint label is re-recorded by the run itself
    started = runner.start(plan.id, actor=principal.user.id, action="run")
    return {"status": "STARTED" if started else "ALREADY_RUNNING", "plan": service.plan_summary(db, plan)}


@router.post("/api/plans/{plan_id}/manual-result", status_code=202)
def import_manual(plan_id: str, db: DB, principal: AdminPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    plan = _load_plan(db, principal, domain, plan_id)
    if runner.is_running(plan.id):
        raise Conflict("the planning pipeline is already running for this plan", code="PLANNING_IN_PROGRESS")
    if plan.state != "AWAITING_MANUAL_CLAUDE":
        raise Conflict(f"plan is {plan.state}, not AWAITING_MANUAL_CLAUDE", code="NOT_AWAITING_MANUAL")
    # Fail fast (synchronously) when the result file does not exist yet.
    try:
        from aeos.claude_bridge import facade as bridge

        if bridge.read_manual_result((plan.checkpoint or {}).get("package_id") or "") is None:
            raise Conflict("no manual result found for the package yet", code="MANUAL_RESULT_MISSING",
                           details={"package_path": (plan.checkpoint or {}).get("package_path")})
    except (ImportError, ModuleNotFoundError) as exc:
        from aeos.core.errors import Blocked

        raise Blocked("manual result reader is not available in this build", code="BRIDGE_NOT_IMPLEMENTED") from exc
    started = runner.start(plan.id, actor=principal.user.id, action="import")
    return {"status": "STARTED" if started else "ALREADY_RUNNING", "plan": service.plan_summary(db, plan)}


@router.get("/api/plans/{plan_id}/versions/{version_id}")
def get_version(plan_id: str, version_id: str, db: DB, principal: CurrentPrincipal,
                domain: ActiveDomain) -> dict[str, Any]:
    plan = _load_plan(db, principal, domain, plan_id)
    v = _load_version(db, plan, version_id)
    return {**service.version_view(v), "is_current": plan.current_version_id == v.id,
            "is_approved": plan.approved_version_id == v.id, "source": "plan_versions (immutable)",
            "generated_at": utcnow().isoformat()}


@router.get("/api/plans/{plan_id}/versions/{version_id}/diff")
def diff_version(plan_id: str, version_id: str, db: DB, principal: CurrentPrincipal, domain: ActiveDomain,
                 against: str | None = None) -> dict[str, Any]:
    plan = _load_plan(db, principal, domain, plan_id)
    return {**service.diff_versions(db, plan.id, version_id, against), "source": "plan_versions (immutable)",
            "generated_at": utcnow().isoformat()}


class VersionIn(BaseModel):
    parent_version_id: str
    content: dict[str, Any]
    rationale: str = Field(min_length=5, max_length=5000)


@router.post("/api/plans/{plan_id}/versions", status_code=201)
def create_version(plan_id: str, body: VersionIn, db: DB, principal: AdminPrincipal,
                   domain: ActiveDomain) -> dict[str, Any]:
    plan = _load_plan(db, principal, domain, plan_id)
    v = service.create_human_version(db, plan.id, content=body.content, parent_version_id=body.parent_version_id,
                                     rationale=body.rationale, actor_user_id=principal.user.id)
    db.commit()
    return service.version_view(v)


class DecisionIn(BaseModel):
    action: Literal["APPROVE_AND_PUBLISH", "APPROVE", "REQUEST_REVISION", "REJECT"]
    version_id: str
    version_hash: str
    targets: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    rationale: str = Field(default="", max_length=5000)
    rerun: bool = False
    expires_at: datetime | None = None
    # §15: approving a version the four-stage review never judged needs this explicit acknowledgement
    acknowledge_unreviewed: bool = False


@router.post("/api/plans/{plan_id}/decision")
def decide(plan_id: str, body: DecisionIn, db: DB, principal: AdminPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    plan = _load_plan(db, principal, domain, plan_id)
    result = service.decide(db, plan.id, action=body.action, version_id=body.version_id,
                            version_hash=body.version_hash, targets=body.targets, rationale=body.rationale,
                            actor_user_id=principal.user.id, expires_at=body.expires_at,
                            acknowledge_unreviewed=body.acknowledge_unreviewed)
    db.commit()
    if body.action == "REQUEST_REVISION" and body.rerun:
        try:
            service.office_gate(db, plan, actor=principal.user.id)
            result["rerun"] = "STARTED" if runner.start(plan.id, actor=principal.user.id, action="run") else "ALREADY_RUNNING"
        except Exception as exc:  # the decision itself is recorded; report why the rerun did not start
            result["rerun"] = f"NOT_STARTED: {getattr(exc, 'code', type(exc).__name__)}"
    result["plan"] = service.plan_summary(db, plan)
    return result


class CompileIn(BaseModel):
    version_id: str | None = None


@router.post("/api/plans/{plan_id}/compile")
def compile_plan(plan_id: str, body: CompileIn, db: DB, principal: AdminPrincipal,
                 domain: ActiveDomain) -> dict[str, Any]:
    plan = _load_plan(db, principal, domain, plan_id)
    approved = service.get_approved_version(db, plan.id)
    if approved is None:
        raise Conflict("only the version bound by an ACTIVE approval can be compiled", code="NOT_APPROVED")
    if body.version_id and body.version_id != approved.id:
        raise Conflict("the requested version is not the approved version", code="NOT_APPROVED",
                       details={"approved_version_id": approved.id})
    result = dag.compile_version(db, approved, actor=principal.user.id)
    db.commit()
    return result


@router.get("/api/plans/{plan_id}/dag")
def get_dag(plan_id: str, db: DB, principal: CurrentPrincipal, domain: ActiveDomain,
            version_id: str | None = None) -> dict[str, Any]:
    plan = _load_plan(db, principal, domain, plan_id)
    vid = version_id or plan.approved_version_id or plan.current_version_id
    if not vid:
        return {"plan_id": plan.id, "nodes": [], "edges": [], "critical_blockers": [], "shared_write_conflicts": [],
                "validation_errors": [], "compiled": False, "mode": "EMPTY", "checked_at": utcnow().isoformat(),
                "remedy": "Jalankan perencanaan untuk menghasilkan versi rencana pertama."}
    view = dag.dag_view(db, plan, _load_version(db, plan, vid))
    return {**view, "checked_at": utcnow().isoformat(), "source": "task_specs, task_executions, leases, acceptance_records"}
