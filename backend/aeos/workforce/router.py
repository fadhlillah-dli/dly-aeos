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
from aeos.core.enums import Domain
from aeos.core.errors import Blocked, NotFound
from aeos.core.timeutil import utcnow
from aeos.identity.facade import Principal
from aeos.models.planning import Plan
from aeos.models.workforce import TaskExecution, WorkerRun
from aeos.workforce import integrations, scheduler, views
from aeos.workforce.limits import HARD_MAX_ACTIVE

router = APIRouter(tags=["workforce"])
RUN_DETAIL_SOURCE = "worker_runs, task_executions, leases, evidence_artifacts, review_records, acceptance_records"


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
            # §46: from which risk class admission needs a PASS role evaluation (owner setting, pilot default R4)
            "role_eval_threshold": scheduler.role_eval_threshold(db),
            "checked_at": utcnow().isoformat(), "mode": "DESIGN"}


@router.get("/api/workforce/agents")
def workforce_agents(principal: CurrentPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    return {"items": views.agents_view(db, domain=domain.value, project_ids=principal.allowed_project_ids),
            "source": "AgentDefinition records",
            "checked_at": utcnow().isoformat(), "mode": "DESIGN"}


class AgentToggle(BaseModel):
    reason: str = Field(default="", max_length=500)


@router.get("/api/workforce/agents/{agent_key}/prerequisites")
def agent_prerequisites(agent_key: str, principal: CurrentPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    """What still blocks enabling the agent in this domain (read-only; the same rule the enable action applies)."""
    from aeos.workforce import agents

    out = agents.prerequisites(db, agents.get_agent(db, agent_key), domain=domain.value)
    db.rollback()
    return {**out, "checked_at": utcnow().isoformat(), "source": "sandbox probe + routing + Doctor CAN_RUN_WORKER",
            "mode": "LIVE"}


@router.post("/api/workforce/agents/{agent_key}/enable")
def enable_agent(agent_key: str, body: AgentToggle, principal: AdminPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    """Enable an implementation agent only when its prerequisites are really READY (no fake activity)."""
    from aeos.workforce import agents

    try:
        out = agents.enable_agent(db, agent_key, domain=domain.value, actor=_actor(principal), actor_type="user",
                                  reason=body.reason)
    except Blocked:
        db.commit()  # the DENIED audit row is kept
        raise
    db.commit()
    return out


@router.post("/api/workforce/agents/{agent_key}/disable")
def disable_agent(agent_key: str, body: AgentToggle, principal: AdminPrincipal, db: DB) -> dict[str, Any]:
    from aeos.workforce import agents

    out = agents.disable_agent(db, agent_key, actor=_actor(principal), actor_type="user", reason=body.reason)
    db.commit()
    return out


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
    return {**views.run_detail(db, run), "source": RUN_DETAIL_SOURCE, "checked_at": utcnow().isoformat()}


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
    reason: str = Field(default="", max_length=500)


@router.post("/api/workforce/scheduler")
def set_scheduler(body: Enable, principal: AdminPrincipal, db: DB) -> dict[str, Any]:
    scheduler.set_scheduler_enabled(db, body.enabled, actor=_actor(principal), reason=body.reason)
    db.commit()
    return {"enabled": body.enabled}


class ConcurrencyIn(BaseModel):
    max_active: int = Field(ge=1, le=HARD_MAX_ACTIVE)  # one source with the scheduler and the CLI
    rationale: str = Field(default="", max_length=2000)
    eval_run_id: str | None = Field(default=None, max_length=64)  # bounded-parallel run (topology C)
    baseline_eval_run_id: str | None = Field(default=None, max_length=64)  # sequential baseline (topology A)
    # §44 task-class scope: project_ids / role_keys / risk_classes / domains (required for two workers)
    scope: dict[str, list[str]] = Field(default_factory=dict)
    # §44 DecisionRecord: options, goal, evidence_ids, cost_quality_tradeoff, invalidate_if
    decision_record: dict[str, Any] = Field(default_factory=dict)


@router.post("/api/workforce/concurrency")
def set_concurrency(body: ConcurrencyIn, principal: AdminPrincipal, db: DB) -> dict[str, Any]:
    """Owner action: run 1 or 2 implementation workers (§19, §21). Two workers need a human POLICY decision on
    workforce/concurrency that references a passing evaluation run (integration evidence) and its rollback."""
    out = scheduler.set_concurrency(db, max_active=body.max_active, actor=_actor(principal),
                                    actor_user_id=principal.user.id, rationale=body.rationale,
                                    eval_run_id=body.eval_run_id, scope=body.scope,
                                    decision_record=body.decision_record,
                                    baseline_eval_run_id=body.baseline_eval_run_id)
    db.commit()
    detail = scheduler.concurrency_detail(db)
    return {**out, "effective_cap": detail["cap"], "cap_decision_id": detail["decision_id"],
            "cap_reason": detail["reason"], "budget_note": detail["budget_note"]}


@router.get("/api/workforce/concurrency")
def concurrency_state(principal: CurrentPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    """The effective cap, its source (pilot / owner decision / resource budget) and the bounded-parallel experiments
    (RUNNING topology-C EvalRuns of this domain) with their grant and measured two-worker execution."""
    from aeos.models.research import EvalRun
    from aeos.workforce import experiments

    detail = scheduler.concurrency_detail(db)
    runs = [r for r in db.scalars(select(EvalRun).where(EvalRun.topology == experiments.PARALLEL_TOPOLOGY,
                                                        EvalRun.status.in_(["RUNNING", "COMPLETED"])))
            if experiments.run_domain(r) == domain.value]
    return {"items": [experiments.experiment_view(db, r) for r in runs[:50]], "concurrency": detail,
            "source": "SystemSetting workforce.concurrency + Decision rows + doctor.resource_budget + WorkerRun records",
            "checked_at": utcnow().isoformat(), "mode": "LIVE"}


class ExperimentGrantIn(BaseModel):
    eval_run_id: str = Field(min_length=1, max_length=64)
    rationale: str = Field(min_length=10, max_length=2000)
    # like every EXCEPTION: an explicit expiry chosen by the owner (no default)
    days: int = Field(ge=1, le=30)


@router.post("/api/workforce/concurrency/experiment", status_code=201)
def grant_concurrency_experiment(body: ExperimentGrantIn, principal: AdminPrincipal, domain: ActiveDomain,
                                 db: DB) -> dict[str, Any]:
    """Owner action (§19 "Dua worker aktif adalah langkah eksperimen berikutnya", §44 topology C): two workers only for
    the TaskSpecs bound to ONE RUNNING topology-C EvalRun of the active domain, as a human EXCEPTION bound to that
    run (another domain's run is not found)."""
    from aeos.workforce import experiments

    out = experiments.grant_experiment(db, eval_run_id=body.eval_run_id, actor=_actor(principal),
                                       actor_user_id=principal.user.id, rationale=body.rationale, days=body.days,
                                       domain=domain.value)
    db.commit()
    return out


class RoleEvalThresholdIn(BaseModel):
    risk_class: str = Field(pattern=r"^R[0-4]$")
    rationale: str = Field(min_length=5, max_length=2000)


@router.post("/api/workforce/role-eval-threshold")
def set_role_eval_threshold(body: RoleEvalThresholdIn, principal: AdminPrincipal, db: DB) -> dict[str, Any]:
    """Owner action (§46): from which risk class admission requires a PASS role evaluation (or an owner EXCEPTION).
    R4 is the pilot default; a lower class tightens admission."""
    out = scheduler.set_role_eval_threshold(db, risk_class=body.risk_class, actor=_actor(principal),
                                            rationale=body.rationale)
    db.commit()
    return out


class RoleAdoptIn(BaseModel):
    eval_run_id: str = Field(min_length=1, max_length=64)  # the role's current PASS EvalRun
    rationale: str = Field(min_length=5, max_length=2000)
    # §44 DecisionRecord: options, goal, evidence_ids (default: the run), cost_quality_tradeoff, invalidate_if
    decision_record: dict[str, Any] = Field(default_factory=dict)
    # optional scope: project_ids / risk_classes / domains (empty = every task of this role)
    scope: dict[str, list[str]] = Field(default_factory=dict)


@router.post("/api/workforce/roles/{role_key}/adopt")
def adopt_role_result(role_key: str, body: RoleAdoptIn, principal: AdminPrincipal, db: DB) -> dict[str, Any]:
    """Owner action (§44/§46): adopt the role's current PASS evaluation as its admission eligibility, recorded as a
    human DecisionRecord bound to the role revision and the EvalRun."""
    out = scheduler.adopt_role_result(db, role_key=role_key, eval_run_id=body.eval_run_id, actor=_actor(principal),
                                      actor_user_id=principal.user.id, rationale=body.rationale,
                                      decision_record=body.decision_record, scope=body.scope)
    db.commit()
    return out


class RolePackImport(BaseModel):
    pack: dict[str, Any]
    source_url: str = Field(min_length=8, max_length=500)
    commit_pin: str = Field(min_length=40, max_length=40)
    license_spdx: str = Field(min_length=1, max_length=64)
    rationale: str = Field(min_length=10, max_length=2000)


@router.post("/api/workforce/role-packs/import", status_code=201)
def import_role_pack(body: RolePackImport, principal: AdminPrincipal, db: DB) -> dict[str, Any]:
    """§45 selective import after review/license/pin: a CANDIDATE RoleSkillPack version bound to a PROMOTION
    decision; refused when the pack carries hooks/auto-update/policy/settings (SKILL_PACK_FORBIDDEN_FIELD)."""
    from aeos.workforce import skill_import

    row = skill_import.import_pack(db, pack=body.pack, source_url=body.source_url, commit_pin=body.commit_pin,
                                   license_spdx=body.license_spdx, actor_user_id=principal.user.id,
                                   rationale=body.rationale)
    db.commit()
    return {"role_key": row.role_key, "version": row.version, "mode": row.mode, "eval_status": row.eval_status,
            "revision_hash": row.revision_hash}


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
def execute_plan(plan_id: str, principal: AdminPrincipal, domain: ActiveDomain, db: DB,
                 body: Reason | None = None) -> dict[str, Any]:
    """Owner action: queue the approved version's TaskSpecs (Plans/:id "Jalankan rencana", ``aeos work execute``)."""
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise NotFound("plan not found")
    load_project(db, principal, plan.project_id, domain=domain)
    out = scheduler.execute_plan(db, plan_id, actor=_actor(principal), user_id=principal.user.id,
                                 reason=body.reason if body is not None else "")
    db.commit()
    return out


@router.get("/api/tasks/{task_id}/investigation")
def investigation_view(task_id: str, principal: CurrentPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    """§19 read-only investigation: the per-hypothesis reports side by side (untrusted DATA) for the integrator."""
    from aeos.models.planning import TaskSpec
    from aeos.workforce import investigation

    _visible_task(db, principal, domain, task_id)
    task = db.get(TaskSpec, task_id)
    if task is None:
        raise NotFound("task not found")
    return {**investigation.comparison(db, task), "checked_at": utcnow().isoformat(), "mode": "LIVE",
            "source": "WorkerRun INVESTIGATION records (runtime-checked evidence refs; reports are untrusted DATA)"}


class InvestigationChoiceIn(BaseModel):
    hypothesis_key: str = Field(min_length=1, max_length=32)
    rationale: str = Field(min_length=10, max_length=2000)


@router.post("/api/tasks/{task_id}/investigation/choose")
def investigation_choose(task_id: str, body: InvestigationChoiceIn, principal: AdminPrincipal, domain: ActiveDomain,
                         db: DB) -> dict[str, Any]:
    """The integrator (human owner) records the chosen evidenced fix; the plan's repair TaskSpec then carries it."""
    from aeos.workforce import investigation

    _visible_task(db, principal, domain, task_id)
    out = investigation.choose_fix(db, task_id, hypothesis_key=body.hypothesis_key, actor=_actor(principal),
                                   actor_user_id=principal.user.id, rationale=body.rationale)
    db.commit()
    return out


@router.get("/api/tasks/{task_id}/admission")
def admission(task_id: str, principal: CurrentPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    """Why a task waits: the §19 admission answers, computed by code (read-only)."""
    te = _visible_task(db, principal, domain, task_id)
    if te.state != "READY":
        return {"task_id": task_id, "admitted": False, "state": te.state, "reason": te.reason,
                "reason_code": te.reason_code, "source": "task_executions (stored state, admission not evaluated)",
                "checked_at": utcnow().isoformat()}
    adm = scheduler.admission_check(db, task_id)
    db.rollback()
    out = adm.as_dict()
    sub = (out.get("answers") or {}).get("subscription")
    if sub is not None and not _sees_every_domain(principal):
        # ADR-0080 amendment 2026-10-26: a subscription route's allowance counts every domain's calls, so its window
        # figures follow the GLOBAL/ROUTE budget visibility rule (as the route view and the Costs page); the wait
        # reasons carry no figures
        out["answers"] = {**out["answers"], "subscription": integrations.subscription_without_figures(sub)}
    return {**out, "state": te.state, "source": "scheduler admission check (§19, read-only)",
            "checked_at": utcnow().isoformat()}


def _sees_every_domain(principal: Principal) -> bool:
    """A principal without a project scope who may access every domain (the cost module's cross-domain rule)."""
    return principal.allowed_project_ids is None and all(principal.can_access_domain(d) for d in Domain)
