"""Workforce Console API (§35 "Kontrak API yang direncanakan").

Reads are domain-scoped (ActiveDomain) and project-filtered by the principal's scope; Office
runs never appear in a Personal session (cross-domain -> 404). Writes need SUPER_ADMIN + CSRF and
commit explicitly. There is no force-green, bypass-review or auto-merge operation.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from aeos.api.deps import DB, ActiveDomain, AdminPrincipal, CurrentPrincipal, load_project
from aeos.core.enums import Capability
from aeos.core.errors import Blocked, NotFound
from aeos.core.timeutil import utcnow
from aeos.identity.service import Principal
from aeos.models.planning import Plan
from aeos.models.workforce import AgentDefinition, TaskExecution, WorkerRun
from aeos.workforce import scheduler, views

router = APIRouter(tags=["workforce"])


def _actor(p: Principal) -> str:
    return f"user:{p.user.id}"


def _visible_run(db: Any, principal: Principal, domain: Any, run_id: str) -> WorkerRun:
    run = db.get(WorkerRun, run_id)
    if run is None or run.domain != domain.value:
        raise NotFound("run not found")
    if principal.allowed_project_ids is not None and run.project_id not in principal.allowed_project_ids:
        raise NotFound("run not found")
    return run


def _visible_task(db: Any, principal: Principal, domain: Any, task_id: str) -> TaskExecution:
    te = db.scalar(select(TaskExecution).where(TaskExecution.task_id == task_id))
    if te is None or te.domain != domain.value:
        raise NotFound("task not found")
    if not principal.can_access_project(te.project_id, te.domain):
        raise NotFound("task not found")
    return te


@router.get("/api/workforce/summary")
def workforce_summary(principal: CurrentPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    from aeos.workforce import loop

    return views.summary(db, domain=domain.value, project_ids=principal.allowed_project_ids,
                         loop_running=loop.is_running())


@router.get("/api/workforce/roles")
def workforce_roles(principal: CurrentPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    data = views.roles_view(db, domain=domain.value, project_ids=principal.allowed_project_ids)
    return {"items": data["groups"], "note": data["note"], "source": "RoleSkillPack catalog (seed, DESIGN)",
            "checked_at": utcnow().isoformat(), "mode": "DESIGN"}


@router.get("/api/workforce/agents")
def workforce_agents(principal: CurrentPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    return {"items": views.agents_view(db, domain=domain.value, project_ids=principal.allowed_project_ids),
            "source": "AgentDefinition records",
            "checked_at": utcnow().isoformat(), "mode": "DESIGN"}


class AgentToggle(BaseModel):
    reason: str = Field(default="", max_length=500)


@router.post("/api/workforce/agents/{agent_key}/enable")
def enable_agent(agent_key: str, body: AgentToggle, principal: AdminPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    """Enable an implementation agent only when its prerequisites are really READY (no fake activity)."""
    from aeos.audit import service as audit
    from aeos.worker_runtime.opencode import opencode_status
    from aeos.worker_runtime.sandbox import sandbox_readiness
    from aeos.workforce import integrations as ext

    agent = db.scalar(select(AgentDefinition).where(AgentDefinition.agent_key == agent_key))
    if agent is None:
        raise NotFound("agent not found")
    reasons: list[str] = []
    if agent.harness == "opencode":
        st = opencode_status()
        if st["status"] != "INSTALLED":
            reasons.append(f"OpenCode {st['status']}: {st['evidence']}")
        sb = sandbox_readiness(db, domain=domain.value)
        if sb["status"] != "READY":
            reasons += sb["reasons"]
        from aeos.routing import facade as routing

        try:
            rd = routing.eligible_routes(db, domain=domain.value, data_classification="INTERNAL")
            if not rd.eligible:
                reasons.append("no APPROVED worker route is eligible for this domain")
        except ImportError:
            reasons.append("routing module unavailable; failing closed")
        ok, cap_reasons = ext.capability_ready(db, Capability.CAN_RUN_WORKER, domain=domain.value, project_id=None)
        if not ok:
            reasons += cap_reasons
    else:
        reasons.append(f"{agent.harness} agents are enabled by their owning module, not here")
    if reasons:
        raise Blocked("agent cannot be enabled yet", code="AGENT_PREREQUISITES", details={"reasons": reasons})
    agent.enabled = True
    agent.enabled_reason = f"enabled by owner: {body.reason}"[:500]
    audit.record(db, actor_type="user", actor_id=_actor(principal), action="workforce.agent.enable",
                 target_type="agent_definition", target_id=agent_key, domain=domain.value, reason=body.reason)
    db.commit()
    return {"agent_key": agent_key, "enabled": True}


@router.post("/api/workforce/agents/{agent_key}/disable")
def disable_agent(agent_key: str, body: AgentToggle, principal: AdminPrincipal, db: DB) -> dict[str, Any]:
    from aeos.audit import service as audit

    agent = db.scalar(select(AgentDefinition).where(AgentDefinition.agent_key == agent_key))
    if agent is None:
        raise NotFound("agent not found")
    agent.enabled = False
    agent.enabled_reason = f"disabled by owner: {body.reason}"[:500]
    audit.record(db, actor_type="user", actor_id=_actor(principal), action="workforce.agent.disable",
                 target_type="agent_definition", target_id=agent_key, reason=body.reason)
    db.commit()
    return {"agent_key": agent_key, "enabled": False}


@router.get("/api/runs")
def list_runs(principal: CurrentPrincipal, domain: ActiveDomain, db: DB,
              active: bool = Query(False), limit: int = Query(200, ge=1, le=500)) -> dict[str, Any]:
    items = views.list_runs(db, domain=domain.value, project_ids=principal.allowed_project_ids, active_only=active,
                            limit=limit, include_demo=principal.allowed_project_ids is None)
    modes = {i["mode"] for i in items}
    return {"items": items, "source": "WorkerRun/TaskExecution/Lease + broker usage", "checked_at": utcnow().isoformat(),
            "mode": "LIVE" if modes <= {"LIVE"} else "MIXED" if "LIVE" in modes else "DEMO"}


@router.get("/api/runs/{run_id}")
def get_run(run_id: str, principal: CurrentPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    run = _visible_run(db, principal, domain, run_id)
    return {**views.run_detail(db, run), "checked_at": utcnow().isoformat()}


class Reason(BaseModel):
    reason: str = Field(default="", max_length=1000)


@router.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str, body: Reason, principal: AdminPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    _visible_run(db, principal, domain, run_id)
    run = scheduler.cancel_run(db, run_id, reason=body.reason or "cancelled from dashboard", actor=_actor(principal))
    db.commit()
    return views.run_row(db, run) | {"note": "capability dicabut dan proses dihentikan; efek eksternal yang sudah "
                                              "terjadi tidak dibatalkan otomatis"}


class QuarantineRelease(BaseModel):
    rationale: str = Field(min_length=10, max_length=2000)


@router.post("/api/runs/{run_id}/quarantine-release")
def quarantine_release(run_id: str, body: QuarantineRelease, principal: AdminPrincipal, domain: ActiveDomain,
                       db: DB) -> dict[str, Any]:
    """The human owner records the outcome of the special review of a quarantined patch (§21) as an
    EXCEPTION Decision bound to this run and its exact runtime head commit. It never accepts the
    task by itself: verification + review + AcceptanceRecord are still required, and a different
    patch (new head) needs a new decision."""
    _visible_run(db, principal, domain, run_id)
    d = scheduler.release_quarantine(db, run_id, actor=_actor(principal), actor_user_id=principal.user.id,
                                     rationale=body.rationale)
    db.commit()
    return {"run_id": run_id, "decision_id": d.id, "target_hash": d.target_hash,
            "note": "keputusan pemilik tercatat; acceptance tetap memerlukan verifier + review"}


@router.post("/api/workforce/pause")
def pause(body: Reason, principal: AdminPrincipal, db: DB) -> dict[str, Any]:
    scheduler.set_scheduling_paused(db, True, actor=_actor(principal), reason=body.reason)
    db.commit()
    return {"paused": True}


@router.post("/api/workforce/resume")
def resume(body: Reason, principal: AdminPrincipal, db: DB) -> dict[str, Any]:
    scheduler.set_scheduling_paused(db, False, actor=_actor(principal), reason=body.reason)
    db.commit()
    return {"paused": False}


class Enable(BaseModel):
    enabled: bool


@router.post("/api/workforce/scheduler")
def set_scheduler(body: Enable, principal: AdminPrincipal, db: DB) -> dict[str, Any]:
    scheduler.set_scheduler_enabled(db, body.enabled, actor=_actor(principal))
    db.commit()
    return {"enabled": body.enabled}


class Retry(BaseModel):
    extra_attempts: int = Field(default=0, ge=0, le=3)
    rationale: str = Field(default="", max_length=2000)


def _task_row(te: TaskExecution) -> dict[str, Any]:
    return {"task_id": te.task_id, "task_key": te.task_key, "state": te.state, "reason": te.reason,
            "reason_code": te.reason_code, "attempts_used": te.attempts_used, "row_version": te.row_version,
            "assigned_agent_key": te.assigned_agent_key}


@router.post("/api/tasks/{task_id}/retry")
def retry(task_id: str, body: Retry, principal: AdminPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    _visible_task(db, principal, domain, task_id)
    te = scheduler.retry_task(db, task_id, actor=_actor(principal), extra_attempts=body.extra_attempts,
                              rationale=body.rationale, actor_user_id=principal.user.id)
    db.commit()
    return _task_row(te)


class Reassign(BaseModel):
    agent_key: str = Field(min_length=1, max_length=64)
    reason: str = Field(default="", max_length=1000)


@router.post("/api/tasks/{task_id}/reassign")
def reassign(task_id: str, body: Reassign, principal: AdminPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    _visible_task(db, principal, domain, task_id)
    te = scheduler.reassign_task(db, task_id, agent_key=body.agent_key, actor=_actor(principal), reason=body.reason)
    db.commit()
    return _task_row(te)


@router.post("/api/plans/{plan_id}/execute")
def execute_plan(plan_id: str, principal: AdminPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise NotFound("plan not found")
    load_project(db, principal, plan.project_id, domain=domain)
    out = scheduler.execute_plan(db, plan_id, actor=_actor(principal), user_id=principal.user.id)
    db.commit()
    return out


@router.get("/api/tasks/{task_id}/admission")
def admission(task_id: str, principal: CurrentPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    """Why a task waits: the §19 admission answers, computed by code (read-only)."""
    te = _visible_task(db, principal, domain, task_id)
    if te.state != "READY":
        return {"task_id": task_id, "admitted": False, "state": te.state, "reason": te.reason,
                "reason_code": te.reason_code, "checked_at": utcnow().isoformat()}
    adm = scheduler.admission_check(db, task_id)
    db.rollback()
    return {**adm.as_dict(), "state": te.state, "checked_at": utcnow().isoformat()}
