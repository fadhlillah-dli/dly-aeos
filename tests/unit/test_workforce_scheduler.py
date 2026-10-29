"""Scheduler behaviour mapped to master §14/§19/§21/§22 and the §50 acceptance rows."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from datetime import timedelta

import psutil
import pytest
from sqlalchemy import func, select, update

from aeos.core.db import session_factory
from aeos.core.errors import Blocked, Conflict, ValidationFailed
from aeos.core.timeutil import utcnow
from aeos.models.audit import AuditEvent
from aeos.models.cost import BudgetReservation, UsageLedgerEntry
from aeos.models.decisions import Decision
from aeos.models.evidence import AcceptanceRecord
from aeos.models.publishing import OperationalWorkProjection
from aeos.models.routing import CapabilityGrant
from aeos.models.workforce import AgentDefinition, Lease, RoleSkillPack, TaskExecution, WorkerRun
from aeos.routing.seed import _go_pricing  # imported before FakeModules replaces aeos.routing.service
from aeos.workforce import demo, scheduler, seed
from aeos.workforce.catalog import ALL_ROLES, EXECUTOR_ROLES, PLANNING_ROLES, SERVICE_ROLES
from tests.unit.test_workforce_support import (
    SLEEP_TREE,
    FakeModules,
    make_owner,
    make_plan,
    make_project,
    make_route,
    process_sandbox_exception,
    seed_and_enable_implementer,
)


@pytest.fixture(name="fakes")
def _fakes_fixture(monkeypatch: pytest.MonkeyPatch) -> FakeModules:
    return FakeModules().install(monkeypatch)


def _ready_env(db, fakes: FakeModules, tasks, *, project=None):
    """Approved plan + route + enabled agent + process-sandbox exception: everything admission needs."""
    project = project or make_project(db)
    plan, pv, specs = make_plan(db, project, tasks)
    fakes.approved[plan.id] = pv.id
    make_route(db)
    fakes.route_key = "worker.test-free"
    seed_and_enable_implementer(db)
    process_sandbox_exception(db)
    scheduler.ensure_executions(db, pv.id, actor="test", requested_by="owner")
    scheduler.refresh_readiness(db, pv.id)
    return plan, pv, specs


# ----------------------------------------------------------------------------- seed (§15-§17, §50)
def test_seed_catalog_complete_and_creates_no_runs(db):
    """§50 'Role catalog berisi banyak jabatan, belum ada task' -> no LLM run/cost."""
    res = seed.run(db)
    assert res["roles_created"] == len(ALL_ROLES) == 7 + 26 + 7
    assert len(PLANNING_ROLES) == 7 and len(EXECUTOR_ROLES) == 26 and len(SERVICE_ROLES) == 7
    assert res["agents_created"] == 3 + 7  # implementer, reviewer, verifier + 7 category agents (ADR-0126)
    roles = list(db.scalars(select(RoleSkillPack)))
    assert {r.mode for r in roles} == {"DESIGN"} and {r.eval_status for r in roles} == {"NOT_EVALUATED"}
    assert all(r.revision_hash and r.version == "1" for r in roles)
    assert sum(r.deterministic_service for r in roles) == 7
    human = db.scalar(select(RoleSkillPack).where(RoleSkillPack.role_key == "human-owner"))
    assert human.kind == "HUMAN"
    assert {r.group for r in roles} == {"product-ux", "architecture-delivery", "fe-api-data", "security-privacy",
                                         "platform-sre", "qa-verification", "docs-release", "research-eval-cost"}
    agents = {a.agent_key: a for a in db.scalars(select(AgentDefinition))}
    assert agents["opencode-implementer"].harness == "opencode" and agents["opencode-implementer"].enabled is False
    assert agents["claude-checkpoint-reviewer"].harness == "claude-native"
    assert agents["independent-verifier"].deterministic is True
    for model in (WorkerRun, Lease, BudgetReservation, UsageLedgerEntry, CapabilityGrant):
        assert db.scalar(select(func.count()).select_from(model)) == 0
    again = seed.run(db)
    assert again["roles_created"] == 0 and again["roles_unchanged"] == len(ALL_ROLES)


def test_seed_never_reenables_or_overwrites_enablement(db):
    seed.run(db)
    a = db.scalar(select(AgentDefinition).where(AgentDefinition.agent_key == "opencode-implementer"))
    a.enabled = True
    a.revision_hash = "changed"
    db.flush()
    seed.run(db)
    assert a.enabled is True and a.revision == "2"


# ----------------------------------------------------------------------------- readiness (§14)
def test_readiness_requires_active_approval(db, fakes):
    p = make_project(db)
    _plan, pv, specs = make_plan(db, p, [{"task_key": "a"}])
    scheduler.ensure_executions(db, pv.id, actor="t")
    scheduler.refresh_readiness(db, pv.id)
    te = scheduler.get_te(db, specs[0].id)
    assert te.state == "BLOCKED" and te.reason_code == "PLAN_NOT_APPROVED"


def test_readiness_fails_closed_without_planning_module(db, absent_modules):
    absent_modules("aeos.planning.service")
    p = make_project(db)
    _plan, pv, specs = make_plan(db, p, [{"task_key": "a"}])
    scheduler.ensure_executions(db, pv.id, actor="t")
    scheduler.refresh_readiness(db, pv.id)
    te = scheduler.get_te(db, specs[0].id)
    assert te.state == "BLOCKED" and "planning module unavailable" in te.reason


def test_dependencies_need_done_and_valid_acceptance(db, fakes):
    """§14: dependency ready only when its artifact is accepted and still valid."""
    _plan, pv, specs = _ready_env(db, fakes, [{"task_key": "a"}, {"task_key": "b", "depends_on": ["a"]}])
    a, b = specs
    assert scheduler.get_te(db, a.id).state == "READY"
    tb = scheduler.get_te(db, b.id)
    assert tb.state == "BLOCKED" and tb.reason_code == "DEPENDENCY_NOT_ACCEPTED"
    # DONE but acceptance no longer valid -> still blocked
    db.execute(update(TaskExecution).where(TaskExecution.task_id == a.id).values(state="DONE"))
    scheduler.refresh_readiness(db, pv.id)
    assert scheduler.get_te(db, b.id).state == "BLOCKED"
    fakes.accepted.add(a.id)
    scheduler.refresh_readiness(db, pv.id)
    assert scheduler.get_te(db, b.id).state == "READY"


def test_a_dependent_waits_until_the_accepted_dependency_is_in_its_base(db, fakes):
    """L32: a dependency is DONE at acceptance, before its pull request is merged, and the dependent starts from the
    source checkout's HEAD; it waits (DEPENDENCY_NOT_IN_BASE) until the accepted change is there."""
    _plan, pv, specs = _ready_env(db, fakes, [{"task_key": "a"}, {"task_key": "b", "depends_on": ["a"]}])
    a, b = specs
    db.execute(update(TaskExecution).where(TaskExecution.task_id == a.id).values(state="DONE"))
    fakes.accepted.add(a.id)
    fakes.in_base = False
    scheduler.refresh_readiness(db, pv.id)
    tb = scheduler.get_te(db, b.id)
    assert tb.state == "BLOCKED" and tb.reason_code == "DEPENDENCY_NOT_IN_BASE"
    assert "dependency a: fake: not merged" in tb.reason
    fakes.in_base = True  # near miss: merged and pulled
    scheduler.refresh_readiness(db, pv.id)
    assert scheduler.get_te(db, b.id).state == "READY"


def test_external_tracker_done_never_unlocks(db, fakes):
    """§50 'Jira/GitHub issue Done tanpa evidence runtime' -> tidak membuka dependency."""
    _plan, pv, specs = _ready_env(db, fakes, [{"task_key": "a"}, {"task_key": "b", "depends_on": ["a"]}])
    db.add(OperationalWorkProjection(project_id=specs[0].project_id, task_id=specs[0].id, provider="github",
                                     issue_id="1", external_status="Done", sync_status="OK"))
    db.flush()
    scheduler.refresh_readiness(db, pv.id)
    assert scheduler.get_te(db, specs[1].id).state == "BLOCKED"


@pytest.mark.parametrize("tasks,bad", [
    ([{"task_key": "a", "depends_on": ["a"]}], "a"),
    ([{"task_key": "a", "depends_on": ["ghost"]}], "a"),
    ([{"task_key": "a", "depends_on": ["b"]}, {"task_key": "b", "depends_on": ["a"]}], "a"),
])
def test_invalid_dag_blocked(db, fakes, tasks, bad):
    _plan, pv, specs = _ready_env(db, fakes, tasks)
    te = scheduler.get_te(db, next(s for s in specs if s.task_key == bad).id)
    assert te.state == "BLOCKED"
    assert "dependency" in te.reason


def test_nested_fan_out_rejected(db, fakes):
    _plan, _pv, specs = _ready_env(db, fakes, [{"task_key": "a", "tools_allowed": ["task"]}])
    te = scheduler.get_te(db, specs[0].id)
    assert te.state == "BLOCKED" and te.reason_code == "FAN_OUT_FORBIDDEN"


# ----------------------------------------------------------------------------- admission (§19)
def test_admission_fails_closed_without_other_modules(db):
    """Missing doctor/routing/cost modules -> waits with explicit reasons, never pretends success."""
    p = make_project(db)
    _plan, pv, specs = make_plan(db, p, [{"task_key": "a"}])
    scheduler.ensure_executions(db, pv.id, actor="t")
    db.execute(update(TaskExecution).where(TaskExecution.task_id == specs[0].id).values(state="READY"))
    adm = scheduler.admission_check(db, specs[0].id)
    assert not adm.admitted
    codes = {c for c, _ in adm.waits}
    assert {"PLAN_NOT_APPROVED", "CAPABILITY_NOT_READY", "SANDBOX_NOT_READY", "ROUTE_INELIGIBLE", "AGENT_DISABLED"} <= codes


def test_admission_and_atomic_lease(db, fakes):
    _plan, _pv, specs = _ready_env(db, fakes, [{"task_key": "a", "allowed_paths": ["src/**"]}])
    adm = scheduler.admission_check(db, specs[0].id)
    assert adm.admitted, adm.reasons
    run = scheduler.acquire_lease(db, specs[0].id, actor="test", admission=adm)
    te = scheduler.get_te(db, specs[0].id)
    assert te.state == "LEASED" and te.active_run_id == run.id and te.attempts_used == 1
    assert run.execution_status == "LEASED" and run.budget_reservation_id
    lease = db.get(Lease, run.lease_id)
    assert lease.state == "ACTIVE" and lease.owner == scheduler.CONTROLLER_ID
    assert fakes.calls["reserve"] == 1


def test_capability_not_ready_blocks_admission(db, fakes):
    fakes.capability = "BLOCKED"
    _plan, _pv, specs = _ready_env(db, fakes, [{"task_key": "a"}])
    adm = scheduler.admission_check(db, specs[0].id)
    assert ("CAPABILITY_NOT_READY" in {c for c, _ in adm.waits}) and not adm.admitted


def test_budget_refusal_keeps_task_waiting(db, fakes):
    fakes.reserve_error = "monthly budget exceeded"
    _plan, _pv, specs = _ready_env(db, fakes, [{"task_key": "a"}])
    assert scheduler.admit_ready_tasks(db) == []
    te = scheduler.get_te(db, specs[0].id)
    assert te.state == "READY" and te.reason_code == "BUDGET_EXCEEDED"


def test_paid_route_without_cost_limit_is_not_admitted(db, fakes):
    """§42: no silent paid run — a non-free route needs an explicit TaskSpec cost limit."""
    p = make_project(db)
    plan, pv, specs = make_plan(db, p, [{"task_key": "a"}])
    fakes.approved[plan.id] = pv.id
    make_route(db, key="worker.paid", is_free="NO")
    fakes.route_key = "worker.paid"
    seed_and_enable_implementer(db)
    process_sandbox_exception(db)
    scheduler.ensure_executions(db, pv.id, actor="t")
    scheduler.refresh_readiness(db, pv.id)
    adm = scheduler.admission_check(db, specs[0].id)
    assert "COST_UNKNOWN" in {c for c, _ in adm.waits}


def test_subscription_route_is_admitted_at_zero_marginal_cost_until_a_window_is_used_up(db, fakes):
    """ADR-0080 amendment 2026-10-26: a flat-fee route needs no TaskSpec cost limit (its plan fee is the activation's
    BUDGET decision; marginal $0 within the plan) and waits - WAITING_QUOTA, never another route - once the allowance
    of a provider window is used up."""
    from aeos.workforce import views

    p = make_project(db)
    plan, pv, specs = make_plan(db, p, [{"task_key": "a"}])
    fakes.approved[plan.id] = pv.id
    route = make_route(db, key="opencode-go/glm-5.3-flash", provider="opencode-go", is_free="NO")
    route.pricing = _go_pricing(60, {"input": 0.15, "output": 0.50})
    route.pricing_checked_at = utcnow()
    fakes.route_key = route.route_key
    seed_and_enable_implementer(db)
    process_sandbox_exception(db)
    scheduler.ensure_executions(db, pv.id, actor="t")
    scheduler.refresh_readiness(db, pv.id)
    adm = scheduler.admission_check(db, specs[0].id)
    assert adm.admitted, adm.reasons
    assert adm.est_usd == 0.0 and adm.answers["subscription"]["state"] == "OK"
    db.add(UsageLedgerEntry(at=utcnow() - timedelta(hours=1), route_key=route.route_key, provider="opencode-go",
                            category="WORKER_API", source="broker", cost_usd=0, charged_usd=0, mode="LIVE",
                            details={"billing": "SUBSCRIPTION", "allowance_usd": "12", "allowance_basis": "REPORTED"}))
    db.flush()
    adm = scheduler.admission_check(db, specs[0].id)
    waits = dict(adm.waits)
    assert not adm.admitted and "the 5h window allowance" in waits["SUBSCRIPTION_ALLOWANCE_EXHAUSTED"]
    assert "$" not in waits["SUBSCRIPTION_ALLOWANCE_EXHAUSTED"]  # no allowance figures in a wait reason
    assert adm.answers["subscription"]["windows"][0]["used_usd"] == 12.0  # the figures are in the answer
    assert scheduler.admit_ready_tasks(db) == [] and fakes.calls.get("reserve") is None
    te = scheduler.get_te(db, specs[0].id)
    assert te.state == "READY" and te.reason_code == "SUBSCRIPTION_ALLOWANCE_EXHAUSTED" and "$" not in te.reason
    assert views.derive_ui_state(None, te) == "WAITING_QUOTA"


def _go_ready(db, fakes):
    p = make_project(db)
    plan, pv, specs = make_plan(db, p, [{"task_key": "a"}])
    fakes.approved[plan.id] = pv.id
    route = make_route(db, key="opencode-go/glm-5.3-flash", provider="opencode-go", is_free="NO")
    route.pricing = _go_pricing(60, {"input": 0.15, "output": 0.50})
    route.pricing_checked_at = utcnow()
    fakes.route_key = route.route_key
    seed_and_enable_implementer(db)
    process_sandbox_exception(db)
    scheduler.ensure_executions(db, pv.id, actor="t")
    scheduler.refresh_readiness(db, pv.id)
    return route, specs


def _go_use(db, route, *, hours_ago: float, **details) -> UsageLedgerEntry:
    e = UsageLedgerEntry(at=utcnow() - timedelta(hours=hours_ago), route_key=route.route_key, provider="opencode-go",
                         category="WORKER_API", source="broker", cost_usd=0, charged_usd=0, mode="LIVE",
                         details={"billing": "SUBSCRIPTION", **details})
    db.add(e)
    db.flush()
    return e


def test_subscription_advisory_windows_are_shown_but_never_block_admission(db, fakes):
    """The week/month windows are advisory (ADR-0080 amendment 2026-10-26): WARN_EXHAUSTED in the admission answer,
    the task is still admitted - with Extra Usage OFF, exceeding them only yields provider refusals."""
    route, specs = _go_ready(db, fakes)
    _go_use(db, route, hours_ago=24 * 3, allowance_usd="30", allowance_basis="REPORTED")
    adm = scheduler.admission_check(db, specs[0].id)
    assert adm.admitted, adm.reasons
    sub = adm.answers["subscription"]
    assert sub["state"] == "WARN_EXHAUSTED" and sub["windows"][1]["state"] == "WARN_EXHAUSTED"
    assert sub["windows"][1]["blocking"] is False and sub["warnings"]


def test_subscription_uncountable_and_beyond_plan_calls_wait_with_their_own_codes(db, fakes):
    from aeos.workforce import views

    route, specs = _go_ready(db, fakes)
    e = _go_use(db, route, hours_ago=1)  # no allowance, no tokens, no bound: UNKNOWN for at most 5 hours
    adm = scheduler.admission_check(db, specs[0].id)
    assert "at most 5 hours" in dict(adm.waits)["SUBSCRIPTION_ALLOWANCE_UNKNOWN"]
    assert "COST_UNKNOWN" not in dict(adm.waits)
    e.at = utcnow() - timedelta(hours=6)
    db.flush()
    assert scheduler.admission_check(db, specs[0].id).admitted  # out of the 5h window: advisory only
    # recorded now: after the route's activation (make_route records the owner's decision)
    _go_use(db, route, hours_ago=0, allowance_usd="0.01", allowance_basis="REPORTED", billing_source="balance",
            beyond_plan_usd="UNKNOWN")
    adm = scheduler.admission_check(db, specs[0].id)
    reason = dict(adm.waits)["SUBSCRIPTION_BEYOND_PLAN"]
    assert "re-activates the route" in reason and "balance" in reason
    assert scheduler.admit_ready_tasks(db) == []
    te = scheduler.get_te(db, specs[0].id)
    assert te.reason_code == "SUBSCRIPTION_BEYOND_PLAN" and views.derive_ui_state(None, te) == "BLOCKED"


def test_subscription_run_grant_has_a_zero_cost_cap(db, fakes):
    """The run's grant cost cap is the marginal estimate: $0 on a subscription route, so a marginal charge (paid usage
    beyond the plan) stops the run's next model request at the broker."""
    p = make_project(db)
    plan, pv, specs = make_plan(db, p, [{"task_key": "a"}])
    fakes.approved[plan.id] = pv.id
    route = make_route(db, key="opencode-go/glm-5.3-flash", provider="opencode-go", is_free="NO")
    route.pricing = _go_pricing(60, {"input": 0.15, "output": 0.50})
    route.pricing_checked_at = utcnow()
    fakes.route_key = route.route_key
    seed_and_enable_implementer(db)
    process_sandbox_exception(db)
    scheduler.ensure_executions(db, pv.id, actor="t")
    scheduler.refresh_readiness(db, pv.id)
    (run_id,) = scheduler.admit_ready_tasks(db)
    run = db.get(WorkerRun, run_id)
    assert run.caps["max_cost_usd"] == 0.0 and run.model_route_key == route.route_key
    assert fakes.calls["reserve"] == 1


def test_pause_blocks_admission_and_resume(db, fakes):
    _plan, _pv, specs = _ready_env(db, fakes, [{"task_key": "a"}])
    scheduler.set_scheduling_paused(db, True, actor="user:x", reason="maintenance")
    assert scheduler.scheduling_paused(db)
    adm = scheduler.admission_check(db, specs[0].id)
    assert "SCHEDULING_PAUSED" in {c for c, _ in adm.waits}
    scheduler.set_scheduling_paused(db, False, actor="user:x")
    assert scheduler.admission_check(db, specs[0].id).admitted


def test_concurrency_cap_default_one_two_only_with_decision(db, fakes):
    _plan, _pv, specs = _ready_env(db, fakes, [{"task_key": "a", "allowed_paths": ["a/**"]},
                                               {"task_key": "b", "allowed_paths": ["b/**"]}])
    leased = scheduler.admit_ready_tasks(db)
    assert len(leased) == 1
    te_b = scheduler.get_te(db, specs[1].id)
    assert te_b.reason_code == "CONCURRENCY_CAP"
    # cap 2 requested without a decision -> still 1
    scheduler._set_setting(db, scheduler.KEY_CONCURRENCY, {"max_active": 2})
    assert scheduler.concurrency_cap(db) == (1, None)
    d = Decision(kind="POLICY", actor_user_id="owner", target_type="workforce", target_id="concurrency",
                 details={"max_active_workers": 2})
    db.add(d)
    db.flush()
    scheduler._set_setting(db, scheduler.KEY_CONCURRENCY, {"max_active": 2, "decision_id": d.id})
    assert scheduler.concurrency_cap(db) == (2, d.id)
    assert len(scheduler.admit_ready_tasks(db)) == 1


def test_write_set_overlap_serialised(db, fakes):
    """§50 'Worker A dan B menulis shared schema yang sama' -> diserialisasi."""
    _plan, _pv, specs = _ready_env(db, fakes, [{"task_key": "a", "allowed_paths": ["backend/aeos/models/**"]},
                                               {"task_key": "b", "allowed_paths": ["backend/aeos/models/x.py"]}])
    d = Decision(kind="POLICY", actor_user_id="owner", target_type="workforce", target_id="concurrency",
                 details={"max_active_workers": 2})
    db.add(d)
    db.flush()
    scheduler._set_setting(db, scheduler.KEY_CONCURRENCY, {"max_active": 2, "decision_id": d.id})
    leased = scheduler.admit_ready_tasks(db)
    assert len(leased) == 1
    te_b = scheduler.get_te(db, specs[1].id)
    assert te_b.state == "READY" and te_b.reason_code == "SHARED_WRITE_CONFLICT"
    assert "shared-write conflict" in te_b.reason


def test_concurrent_lease_exactly_one_winner(settings, fakes):
    """§14 'READY --> LEASED: reservasi dan lease atomik' — N threads race, exactly one wins."""
    sf = session_factory()
    with sf() as db:
        _plan, _pv, specs = _ready_env(db, fakes, [{"task_key": "a"}])
        adm = scheduler.admission_check(db, specs[0].id)
        assert adm.admitted
        db.commit()
        task_id = specs[0].id
    results: list[str] = []
    barrier = threading.Barrier(6)

    def worker():
        with sf() as s:
            a = scheduler.admission_check(s, task_id)
            barrier.wait()
            try:
                scheduler.acquire_lease(s, task_id, actor="race", admission=a)
                s.commit()
                results.append("won")
            except Conflict:
                s.rollback()
                results.append("lost")
            except Exception as exc:  # SQLITE busy etc. would be a real failure
                s.rollback()
                results.append(f"error:{type(exc).__name__}:{exc}")

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("won") == 1, results
    assert all(r in ("won", "lost") for r in results), results
    with sf() as s:
        assert s.scalar(select(func.count()).select_from(WorkerRun)) == 1
        assert s.scalar(select(func.count()).select_from(Lease).where(Lease.state == "ACTIVE")) == 1
        assert s.scalar(select(func.count()).select_from(BudgetReservation).where(
            BudgetReservation.status == "RESERVED")) == 1


def test_attempt_cap_and_repair(db, fakes):
    """§21: max two implementation attempts; reviewer findings -> new attempt; cap -> NEEDS_DECISION."""
    _plan, pv, specs = _ready_env(db, fakes, [{"task_key": "a"}])
    tid = specs[0].id
    for attempt in (1, 2):
        run = scheduler.acquire_lease(db, tid, actor="t")
        assert run.attempt == attempt
        scheduler.transition_task(db, tid, "RUNNING", reason="bound", actor="t")
        scheduler.transition_task(db, tid, "REVIEW", reason="exit 0", actor="t")
        te = scheduler.request_repair(db, tid, findings=["fix the test"], review_record_id=None, actor="reviewer:x")
        if attempt == 1:
            assert te.state == "READY" and te.pending_findings == ["fix the test"]
    assert te.state == "BLOCKED" and te.reason_code == "NEEDS_DECISION"
    with pytest.raises(Blocked):
        scheduler.retry_task(db, tid, actor="user:o")
    owner = make_owner(db)
    te = scheduler.retry_task(db, tid, actor="user:o", extra_attempts=1, rationale="one more try", actor_user_id=owner)
    assert te.state == "READY" and te.extra_attempts == 1
    d = db.get(Decision, te.extra_attempts_decision_id)
    assert d.kind == "EXCEPTION" and d.target_id == tid and d.target_hash == specs[0].spec_hash


def test_reviewer_agent_never_repairs(db, fakes):
    _plan, _pv, specs = _ready_env(db, fakes, [{"task_key": "a"}])
    tid = specs[0].id
    scheduler.acquire_lease(db, tid, actor="t")
    scheduler.transition_task(db, tid, "RUNNING", reason="b", actor="t")
    scheduler.transition_task(db, tid, "REVIEW", reason="x", actor="t")
    db.execute(update(TaskExecution).where(TaskExecution.task_id == tid)
               .values(assigned_agent_key="claude-checkpoint-reviewer"))
    te = scheduler.request_repair(db, tid, findings=["x"], review_record_id=None, actor="reviewer")
    assert te.assigned_agent_key == "opencode-implementer"
    with pytest.raises(ValidationFailed):
        scheduler.reassign_task(db, tid, agent_key="claude-checkpoint-reviewer", actor="user:o")


def test_repair_requires_review_state(db, fakes):
    _plan, _pv, specs = _ready_env(db, fakes, [{"task_key": "a"}])
    with pytest.raises(Conflict):
        scheduler.request_repair(db, specs[0].id, findings=[], review_record_id=None, actor="r")


# ----------------------------------------------------------------------------- acceptance (§14, §34)
def _to_review(db, fakes):
    _plan, _pv, specs = _ready_env(db, fakes, [{"task_key": "a"}])
    tid = specs[0].id
    run = scheduler.acquire_lease(db, tid, actor="t")
    scheduler.transition_task(db, tid, "RUNNING", reason="b", actor="t")
    scheduler.set_run_status(db, run, "RUNNING", reason="b", actor="t")
    scheduler.transition_task(db, tid, "REVIEW", reason="exit 0", actor="t")
    scheduler.set_run_status(db, run, "REVIEW", reason="exit 0", actor="t")
    return tid, run


def test_done_impossible_without_valid_acceptance(db, fakes):
    """§50 'Worker exit 0 / menulis all tests pass' -> REVIEW/NOT-PROVEN until verifier evidence."""
    tid, run = _to_review(db, fakes)
    scheduler.update_run(db, run.id, actor="worker", worker_reported_status="ALL_TESTS_PASS")
    scheduler.update_run(db, run.id, actor="verifier", acceptance_status="ACCEPTED")
    assert scheduler.get_te(db, tid).state == "REVIEW"  # observed field never moves state
    with pytest.raises(Conflict):
        scheduler.mark_accepted(db, tid, run.id, acceptance_id="missing", actor="v")
    rec = AcceptanceRecord(task_id=tid, run_id=run.id, project_id=run.project_id, artifact_hash="sha256:x",
                           acceptance_status="PENDING")
    db.add(rec)
    db.flush()
    with pytest.raises(Conflict):
        scheduler.mark_accepted(db, tid, run.id, acceptance_id=rec.id, actor="v")
    rec.acceptance_status = "ACCEPTED"
    db.flush()
    with pytest.raises(Blocked):  # verification module says not valid
        scheduler.mark_accepted(db, tid, run.id, acceptance_id=rec.id, actor="v")
    fakes.accepted.add(tid)
    te = scheduler.mark_accepted(db, tid, run.id, acceptance_id=rec.id, actor="v")
    assert te.state == "DONE" and te.accepted_run_id == run.id
    assert db.get(WorkerRun, run.id).execution_status == "DONE"


def test_mark_accepted_fails_closed_without_verification_module(db, monkeypatch):
    fk = FakeModules().install(monkeypatch)
    tid, run = _to_review(db, fk)
    fk.accepted.add(tid)
    # The verification module disappears after the run reached REVIEW (admission itself already
    # refuses to start a worker without a verifier: see test_admission_waits_without_verifier).
    # A None entry makes every import of it raise ImportError - deleting the entry instead re-imported the real
    # module from disk: alone that failed on the fake collector, after other tests it left a second module object
    # on the package that later monkeypatches (test_doctor_c4) no longer reached.
    monkeypatch.setitem(sys.modules, "aeos.verification.acceptance", None)
    rec = AcceptanceRecord(task_id=tid, run_id=run.id, project_id=run.project_id, artifact_hash="sha256:x",
                           acceptance_status="ACCEPTED")
    db.add(rec)
    db.flush()
    with pytest.raises(Blocked) as exc:
        scheduler.mark_accepted(db, tid, run.id, acceptance_id=rec.id, actor="v")
    assert exc.value.code == "ACCEPTANCE_INVALID" and "verification module unavailable" in exc.value.message


def test_update_run_keeps_status_fields_separate(db, fakes):
    _tid, run = _to_review(db, fakes)
    r = scheduler.update_run(db, run.id, actor="verifier", verifier_status="FAILED", review_verdict="NOT-PROVEN",
                             merged_status="NOT_MERGED", touched_paths=["a.py"])
    assert (r.verifier_status, r.review_verdict, r.merged_status, r.acceptance_status) == \
        ("FAILED", "NOT-PROVEN", "NOT_MERGED", None)
    with pytest.raises(ValidationFailed):
        scheduler.update_run(db, run.id, actor="x", execution_status="DONE")
    with pytest.raises(ValidationFailed):
        scheduler.update_run(db, run.id, actor="x", verifier_status="GREEN")


# ----------------------------------------------------------------------------- heartbeat / stale (§35, §50)
def test_heartbeat_extends_lease_and_stale_events_rejected(db, fakes):
    """§50 'Lease lama mengirim event setelah retry/reassign' -> tidak menimpa attempt baru."""
    _plan, _pv, specs = _ready_env(db, fakes, [{"task_key": "a"}])
    run = scheduler.acquire_lease(db, specs[0].id, actor="t")
    lease = db.get(Lease, run.lease_id)
    before = lease.expires_at
    time.sleep(0.01)
    assert scheduler.heartbeat(db, run.id, lease_id=run.lease_id)
    assert db.get(Lease, run.lease_id).expires_at > before
    assert not scheduler.heartbeat(db, run.id, lease_id="other-lease")
    lease.state = "REVOKED"
    db.flush()
    assert not scheduler.record_action(db, run.id, lease_id=run.lease_id, action={"kind": "edit_file", "label": "x"})
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "workforce.run.action.stale")) is not None
    assert db.get(WorkerRun, run.id).current_action != "x"


# ----------------------------------------------------------------------------- reconciliation (§14, §50)
def _spawn_sleeper():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    return p, psutil.Process(p.pid).create_time()


def test_lease_expiry_with_alive_pid_never_starts_duplicate(db, fakes, monkeypatch):
    """§50 'Controller crash dan worker lama masih aktif' -> no duplicate before reconciliation."""
    _plan, pv, specs = _ready_env(db, fakes, [{"task_key": "a"}])
    tid = specs[0].id
    run = scheduler.acquire_lease(db, tid, actor="t")
    scheduler.transition_task(db, tid, "RUNNING", reason="b", actor="t")
    scheduler.set_run_status(db, run, "RUNNING", reason="b", actor="t")
    proc, ctime = _spawn_sleeper()
    try:
        run.pid, run.pid_create_time = proc.pid, ctime
        db.get(Lease, run.lease_id).expires_at = utcnow() - timedelta(seconds=5)
        db.flush()
        # Simulate an unkillable/unknown old process: kill attempt has no effect.
        monkeypatch.setattr(scheduler, "_kill", lambda run_id: {"killed": [], "remaining": [proc.pid], "methods": []})
        summary = scheduler.reconcile(db)
        assert summary["alive"] == 1
        te = scheduler.get_te(db, tid)
        assert te.state == "BLOCKED" and te.reason_code == "LEASE_LOST_PROCESS_ALIVE"
        assert db.scalar(select(CapabilityGrant).where(CapabilityGrant.run_id == run.id,
                                                       CapabilityGrant.active.is_(True))) is None
        scheduler.refresh_readiness(db, pv.id)
        assert scheduler.get_te(db, tid).state == "BLOCKED"
        assert scheduler.admit_ready_tasks(db) == []
        assert db.scalar(select(func.count()).select_from(WorkerRun)) == 1  # no duplicate
    finally:
        proc.kill()
        proc.wait()
    scheduler.reconcile(db)
    assert db.get(WorkerRun, run.id).execution_status == "FAILED"
    scheduler.refresh_readiness(db, pv.id)
    assert scheduler.get_te(db, tid).state == "READY"


def test_lease_expiry_reconcile_kills_orphan_then_allows_retry(db, fakes):
    _plan, pv, specs = _ready_env(db, fakes, [{"task_key": "a"}])
    tid = specs[0].id
    run = scheduler.acquire_lease(db, tid, actor="t")
    proc, ctime = _spawn_sleeper()
    run.pid, run.pid_create_time = proc.pid, ctime
    db.get(Lease, run.lease_id).expires_at = utcnow() - timedelta(seconds=1)
    db.flush()
    db.commit()  # kill_run_processes reads the pid from the DB (crash path: registry empty)
    scheduler.reconcile(db)
    proc.wait(timeout=10)
    te = scheduler.get_te(db, tid)
    assert te.state == "BLOCKED" and te.reason_code == "LEASE_LOST"  # LEASED -> BLOCKED: lease hilang
    scheduler.refresh_readiness(db, pv.id)
    assert scheduler.get_te(db, tid).state == "READY"
    db.commit()


def test_dead_pid_with_reused_number_not_treated_as_alive(db):
    from aeos.worker_runtime import processes

    proc, ctime = _spawn_sleeper()
    try:
        assert processes.is_alive(proc.pid, ctime)
        assert not processes.is_alive(proc.pid, ctime - 1000)  # different create_time -> different process
    finally:
        proc.kill()
        proc.wait()
    assert not processes.is_alive(proc.pid, ctime)


# ----------------------------------------------------------------------------- cancel (§22, §35, §50)
def test_cancel_kills_child_and_grandchild_revokes_and_releases(db, fakes, tmp_path):
    """§50 'Cancel saat ada child process' -> capability dicabut dan seluruh proses dihentikan."""
    from aeos.broker import facade as broker
    from aeos.worker_runtime import processes

    _plan, _pv, specs = _ready_env(db, fakes, [{"task_key": "a"}])
    tid = specs[0].id
    run = scheduler.acquire_lease(db, tid, actor="t")
    broker.issue_grant(db, run_id=run.id, project_id=run.project_id, domain=run.domain, kind="MODEL_PROXY",
                       scope={"models": {"m": "worker.test-free"}}, ttl_seconds=600)
    dirs = processes.SandboxDirs.create(tmp_path / "sbx")
    mp = processes.spawn(run.id, [sys.executable, "-c", SLEEP_TREE], cwd=tmp_path,
                         env=processes.build_sandbox_env(dirs, run_id=run.id))
    gpid = int(mp.stdout.readline().decode().strip())
    run.pid, run.pid_create_time = mp.pid, mp.create_time
    scheduler.transition_task(db, tid, "RUNNING", reason="bound", actor="t")
    scheduler.set_run_status(db, run, "RUNNING", reason="bound", actor="t")
    out = scheduler.cancel_run(db, run.id, reason="owner stop", actor="user:o")
    assert out.execution_status == "CANCELLED"
    assert not processes.is_alive(mp.pid, mp.create_time)
    assert not (psutil.pid_exists(gpid) and psutil.Process(gpid).status() != psutil.STATUS_ZOMBIE)
    assert scheduler.get_te(db, tid).state == "CANCELLED"
    assert db.scalar(select(CapabilityGrant).where(CapabilityGrant.run_id == run.id)).active is False
    assert db.get(BudgetReservation, run.budget_reservation_id).status == "RELEASED"
    assert db.get(Lease, run.lease_id).state == "REVOKED"
    ev = db.scalar(select(AuditEvent).where(AuditEvent.action == "workforce.run.cancel"))
    assert "external_side_effects_not_undone" in ev.details
    # idempotent
    assert scheduler.cancel_run(db, run.id, reason="again", actor="user:o").execution_status == "CANCELLED"


def test_cancel_from_every_non_terminal_task_state(db, fakes):
    for state in ("QUEUED", "READY", "BLOCKED", "REVIEW"):
        p = make_project(db)
        _plan, pv, specs = make_plan(db, p, [{"task_key": f"t-{state.lower()}"}])
        scheduler.ensure_executions(db, pv.id, actor="t")
        db.execute(update(TaskExecution).where(TaskExecution.task_id == specs[0].id).values(state=state))
        assert scheduler.cancel_task(db, specs[0].id, reason="x", actor="user:o").state == "CANCELLED"


def test_reassign_revokes_old_lease_before_new(db, fakes):
    _plan, _pv, specs = _ready_env(db, fakes, [{"task_key": "a"}])
    tid = specs[0].id
    run = scheduler.acquire_lease(db, tid, actor="t")
    te = scheduler.reassign_task(db, tid, agent_key="opencode-implementer", actor="user:o", reason="swap")
    assert te.state == "READY"
    assert db.get(Lease, run.lease_id).state == "REVOKED"
    assert db.get(WorkerRun, run.id).execution_status == "CANCELLED"
    assert db.get(WorkerRun, run.id).outcome_code == "REASSIGNED"


# ----------------------------------------------------------------------------- execute plan
def test_execute_plan_requires_approval_and_capability(db, monkeypatch):
    fk = FakeModules().install(monkeypatch)
    p = make_project(db)
    plan, pv, specs = make_plan(db, p, [{"task_key": "a"}])
    with pytest.raises(Blocked) as exc:
        scheduler.execute_plan(db, plan.id, actor="user:o", user_id="o")
    assert exc.value.code == "PLAN_NOT_APPROVED"
    fk.approved[plan.id] = pv.id
    fk.capability = "BLOCKED"
    with pytest.raises(Blocked) as exc:
        scheduler.execute_plan(db, plan.id, actor="user:o", user_id="o")
    assert exc.value.code == "CAPABILITY_NOT_READY"
    fk.capability = "READY"
    out = scheduler.execute_plan(db, plan.id, actor="user:o", user_id="o")
    assert out["plan_version_id"] == pv.id and out["ready"] == 1
    assert scheduler.get_te(db, specs[0].id).requested_by == "o"


# ----------------------------------------------------------------------------- DEMO fixtures (§35, §50)
def test_demo_fixtures_labelled_and_excluded(db):
    """§50 'Fixture roster/run ditampilkan saat belum ada runtime' -> MOCK/DEMO, not counted."""
    from aeos.workforce import views

    res = demo.insert(db, domain="PERSONAL")
    assert res["inserted"] == 4
    rows = views.list_runs(db, domain="PERSONAL", project_ids=None)
    assert rows and {r["mode"] for r in rows} == {"DEMO"}
    s = views.summary(db, domain="PERSONAL", project_ids=None, loop_running=False)
    assert s["active_runs"] == 0 and s["demo_runs"] == 4
    assert sum(s["task_counts"].values()) == 0
    assert s["cost"]["entries"] == 0
    roles = views.roles_view(db, domain="PERSONAL")
    assert all(r["active_runs"] == 0 for g in roles["groups"] for r in g["roles"])
    stale = [r for r in rows if r["task"]["key"] == "demo-stale-heartbeat"][0]
    assert stale["heartbeat_stale"] and stale["ui_state"] == "STALE"
    quota = [r for r in rows if r["task"]["key"] == "demo-quota-wait"][0]
    assert quota["ui_state"] == "WAITING_QUOTA"
    review = [r for r in rows if r["task"]["key"] == "demo-api-health"][0]
    assert review["ui_state"] == "WAITING_REVIEW" and review["statuses"]["acceptance"] == "PENDING"
    assert demo.remove(db)["removed"] == 4
    assert views.list_runs(db, domain="PERSONAL", project_ids=None) == []
