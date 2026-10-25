"""Workforce gap closure (cluster C7): admission answers (§19), fairness (§10), contract classes (§19),
the owner's concurrency mode (§19/§21/§44), RoleSkillPack contracts and evaluation-derived eligibility (§17,
§46), project-scoped agents (§35) and Live Work provenance (§35)."""

from __future__ import annotations

import sys
import types
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update

from aeos.core.db import session_factory
from aeos.core.errors import Blocked, ValidationFailed
from aeos.core.timeutil import utcnow
from aeos.models.broker_ext import BrokerSideEffect
from aeos.models.decisions import Decision
from aeos.models.planning import Plan, PlanningRun
from aeos.models.research import EvalRun
from aeos.models.workforce import AgentDefinition, RoleSkillPack, TaskExecution, WorkerRun
from aeos.workforce import catalog, roles, scheduler, seed, views
from tests.unit.test_workforce_support import (
    FakeModules,
    make_plan,
    make_project,
    make_route,
    process_sandbox_exception,
    seed_and_enable_implementer,
)


@pytest.fixture(name="fakes")
def _fakes_fixture(monkeypatch: pytest.MonkeyPatch) -> FakeModules:
    return FakeModules().install(monkeypatch)


def _env(db, fakes, tasks, *, project=None, route=True):
    project = project or make_project(db)
    plan, pv, specs = make_plan(db, project, tasks)
    fakes.approved[plan.id] = pv.id
    if route and not db.scalar(select(scheduler.ModelRoute)):
        make_route(db)
    fakes.route_key = "worker.test-free"
    if not db.scalar(select(AgentDefinition).where(AgentDefinition.enabled.is_(True))):
        seed_and_enable_implementer(db)
        process_sandbox_exception(db)
    scheduler.ensure_executions(db, pv.id, actor="test", requested_by="owner")
    scheduler.refresh_readiness(db, pv.id)
    return plan, pv, specs


def _codes(adm):
    return {c for c, _ in [*adm.blocks, *adm.waits]}


# ----------------------------------------------------------------------------- §21 / §19 TaskSpec contract
def test_empty_write_scope_is_never_admitted(db, fakes):
    _p, _pv, specs = _env(db, fakes, [{"task_key": "a", "allowed_paths": []}])
    adm = scheduler.admission_check(db, specs[0].id)
    assert not adm.admitted and "NO_ALLOWED_PATHS" in _codes(adm)
    assert scheduler.admit_ready_tasks(db) == [] and db.scalar(select(WorkerRun)) is None


def test_task_without_expected_output_is_not_admitted(db, fakes):
    _p, _pv, specs = _env(db, fakes, [{"task_key": "a", "expected_outputs": []}])
    assert "NO_EXPECTED_OUTPUT" in _codes(scheduler.admission_check(db, specs[0].id))


def test_unknown_role_is_blocked_at_readiness_and_admission(db, fakes):
    _p, _pv, specs = _env(db, fakes, [{"task_key": "a", "role_key": "chief-executive-officer"}])
    te = scheduler.get_te(db, specs[0].id)
    assert te.state == "BLOCKED" and te.reason_code == "ROLE_UNKNOWN"
    assert views.derive_ui_state(None, te) == "BLOCKED"


def test_distinct_migration_files_and_auth_files_are_serialised(db, fakes):
    _p, _pv, specs = _env(db, fakes, [
        {"task_key": "m1", "allowed_paths": ["backend/aeos/migrations/versions/0002_a.py"]},
        {"task_key": "m2", "allowed_paths": ["backend/aeos/migrations/versions/0003_b.py"]},
        {"task_key": "free", "allowed_paths": ["frontend/src/x/**"]}])
    d = Decision(kind="POLICY", actor_user_id="owner", target_type="workforce", target_id="concurrency",
                 details={"max_active_workers": 2})
    db.add(d)
    db.flush()
    scheduler._set_setting(db, scheduler.KEY_CONCURRENCY, {"max_active": 2, "decision_id": d.id})
    leased = scheduler.admit_ready_tasks(db)
    assert len(leased) == 2
    keys = {db.get(WorkerRun, r).task_key for r in leased}
    assert keys == {"m1", "free"}
    assert "SHARED_WRITE_CONFLICT" in _codes(scheduler.admission_check(db, specs[1].id))
    conflicts = scheduler.write_set_conflicts(db, specs[1])
    assert conflicts[0]["pairs"] == [] and conflicts[0]["classes"] == ["migrations"]
    assert scheduler.serialisation_classes(["backend/aeos/api/deps.py"]) == {"auth-iam"}
    assert scheduler.serialisation_classes(["src/app/**"]) == set()


# ----------------------------------------------------------------------------- §10 fairness
def test_round_robin_by_project_and_age_bump(db, fakes):
    p1, p2 = make_project(db), make_project(db)
    _plan1, _pv1, a = _env(db, fakes, [{"task_key": f"a{i}"} for i in range(3)], project=p1)
    _plan2, _pv2, b = _env(db, fakes, [{"task_key": f"b{i}"} for i in range(2)], project=p2)
    now = utcnow()
    for i, s in enumerate(a + b):  # every task of project 1 became READY before those of project 2
        db.execute(update(TaskExecution).where(TaskExecution.task_id == s.id)
                   .values(state_changed_at=now - timedelta(minutes=10 - i)))
    ready = list(db.scalars(select(TaskExecution).where(TaskExecution.state == "READY")))
    order = [te.task_key for te in scheduler.fair_order(db, ready)]
    assert order[:4] == ["a0", "b0", "a1", "b1"]  # alternating, not global FIFO (a0, a1, a2, b0, ...)
    # project 1 just got a lease: the next round starts with project 2
    leased = scheduler.admit_ready_tasks(db)
    assert db.get(WorkerRun, leased[0]).task_key == "a0"
    rest = list(db.scalars(select(TaskExecution).where(TaskExecution.state == "READY")))
    assert scheduler.fair_order(db, rest)[0].project_id == p2.id
    # anti-starvation: a task waiting longer than the bound goes first regardless of project order
    db.execute(update(TaskExecution).where(TaskExecution.task_id == a[2].id)
               .values(state_changed_at=now - scheduler.FAIRNESS_MAX_WAIT - timedelta(minutes=1)))
    rest = list(db.scalars(select(TaskExecution).where(TaskExecution.state == "READY")))
    assert scheduler.fair_order(db, rest)[0].task_key == "a2"


# ----------------------------------------------------------------------------- §19/§21/§44 concurrency mode
def test_only_a_policy_decision_on_workforce_concurrency_raises_the_cap(db, fakes):
    _env(db, fakes, [{"task_key": "a"}])
    wrong = Decision(kind="EXCEPTION", actor_user_id="owner", target_type="task", target_id="x",
                     details={"max_active_workers": 2})
    db.add(wrong)
    db.flush()
    scheduler._set_setting(db, scheduler.KEY_CONCURRENCY, {"max_active": 2, "decision_id": wrong.id})
    assert scheduler.concurrency_cap(db) == (1, None)
    other_target = Decision(kind="POLICY", actor_user_id="owner", target_type="office_policy", target_id="OFFICE",
                            details={"max_active_workers": 2})
    db.add(other_target)
    db.flush()
    scheduler._set_setting(db, scheduler.KEY_CONCURRENCY, {"max_active": 2, "decision_id": other_target.id})
    assert scheduler.concurrency_cap(db) == (1, None)


def _eval_run(db, *, gates=None, status="COMPLETED", role_key=None, configuration=None, accepted=1):
    run = EvalRun(suite_key="eval.small-bugfix", configuration=configuration or {"model_id": "m"},
                  configuration_hash="sha256:c", status=status, gate_results=gates or {
                      "security": "PASS", "authority": "PASS", "evidence": "PASS"},
                  role_key=role_key, metrics={"outcome": {"accepted": accepted}}, mode="LIVE")
    db.add(run)
    db.flush()
    return run


RECORD = {"options": ["1 worker", "2 workers"], "goal": "throughput independent tasks",
          "cost_quality_tradeoff": "2x slots, same review", "invalidate_if": "any gate FAIL"}


def _metrics(*, rate=0.8, latency=600.0, queue=120.0, critical=0, security=0, scope=1.0):
    return {"outcome": {"accepted": 4, "final_acceptance_rate": rate, "critical_regressions": critical},
            "contract": {"security_violations": security, "scope_adherence_rate": scope},
            "performance": {"latency_p50_s": latency, "queue_wait_p50_s": queue}}


def _topology_runs(db, *, candidate_metrics=None, baseline_metrics=None, baseline_conditions="sha256:cond"):
    """A sequential baseline (A) and a bounded-parallel run (C) of the same suite/configuration/conditions."""
    base = _eval_run(db)
    base.topology, base.conditions_hash, base.metrics = "A", baseline_conditions, baseline_metrics or _metrics()
    cand = _eval_run(db)
    cand.topology, cand.conditions_hash = "C", "sha256:cond"
    cand.metrics = candidate_metrics or _metrics(latency=360.0, queue=30.0)  # same acceptance, faster
    db.flush()
    return cand, base


def test_owner_sets_two_workers_only_with_passing_evaluation_and_record(db, fakes, monkeypatch):
    from aeos.workforce import integrations as ext

    seen = {}
    monkeypatch.setattr(ext, "record_decision", lambda session, **kw: seen.update(kw) or _decision(db, **kw))
    owner = "owner-1"
    with pytest.raises(Blocked) as exc:
        scheduler.set_concurrency(db, max_active=2, actor="user:o", actor_user_id=owner, rationale="uji dua worker",
                                  eval_run_id=None, decision_record=RECORD)
    assert exc.value.code == "CONCURRENCY_EVIDENCE_MISSING"
    failing = _eval_run(db, gates={"security": "PASS", "authority": "FAIL", "evidence": "PASS"})
    with pytest.raises(Blocked):
        scheduler.set_concurrency(db, max_active=2, actor="user:o", actor_user_id=owner, rationale="uji dua worker",
                                  eval_run_id=failing.id, decision_record=RECORD)
    ok, baseline = _topology_runs(db)
    with pytest.raises(ValidationFailed) as exc:
        scheduler.set_concurrency(db, max_active=2, actor="user:o", actor_user_id=owner, rationale="uji dua worker",
                                  eval_run_id=ok.id, baseline_eval_run_id=baseline.id, decision_record={"goal": "x"},
                                  scope={"risk_classes": ["R1"]})
    assert exc.value.code == "DECISION_RECORD_INCOMPLETE"
    with pytest.raises(ValidationFailed) as exc:  # §44 never "every project/role at once"
        scheduler.set_concurrency(db, max_active=2, actor="user:o", actor_user_id=owner, rationale="uji dua worker",
                                  eval_run_id=ok.id, baseline_eval_run_id=baseline.id, decision_record=RECORD)
    assert exc.value.code == "CONCURRENCY_SCOPE_REQUIRED"
    out = scheduler.set_concurrency(db, max_active=2, actor="user:o", actor_user_id=owner, rationale="uji dua worker",
                                    eval_run_id=ok.id, baseline_eval_run_id=baseline.id, decision_record=RECORD,
                                    scope={"risk_classes": ["R1"], "bogus": ["x"]})
    assert seen["kind"] == "POLICY" and (seen["target_type"], seen["target_id"]) == ("workforce", "concurrency")
    assert seen["details"]["rollback"]["previous_max_active"] == 1 and seen["details"]["eval_run_id"] == ok.id
    assert seen["details"]["baseline_eval_run_id"] == baseline.id
    assert set(seen["details"]["benefit"]["gains"]) == {"latency_p50_s", "queue_wait_p50_s"}
    assert seen["details"]["decision_record"]["evidence_ids"] == [ok.id, baseline.id]
    assert out["scope"] == {"risk_classes": ["R1"]}
    assert scheduler.concurrency_cap(db)[0] == 2
    # §44: the mode is adopted per task class only
    t_r1 = types.SimpleNamespace(project_id="p", role_key="feature-implementer", risk_class="R1", domain="PERSONAL")
    t_r3 = types.SimpleNamespace(project_id="p", role_key="feature-implementer", risk_class="R3", domain="PERSONAL")
    assert scheduler.concurrency_cap(db, t_r1)[0] == 2 and scheduler.concurrency_cap(db, t_r3) == (1, None)
    back = scheduler.set_concurrency(db, max_active=1, actor="user:o", actor_user_id=owner, rationale="kembali")
    assert back["previous"] == 2 and scheduler.concurrency_cap(db) == (1, None)


def _decision(db, **kw):
    d = Decision(**{k: v for k, v in kw.items() if v is not None}, actor_kind="HUMAN", status="ACTIVE")
    db.add(d)
    db.flush()
    return d


@pytest.mark.parametrize("problem, candidate, baseline, conditions", [
    ("no baseline", None, None, "sha256:cond"),
    ("worse acceptance", _metrics(rate=0.6, latency=100.0), None, "sha256:cond"),
    ("more critical regressions", _metrics(critical=1, latency=100.0), None, "sha256:cond"),
    ("no gain", _metrics(), None, "sha256:cond"),
    ("unmeasured", {"performance": {"latency_p50_s": 100.0}}, None, "sha256:cond"),
    ("other conditions", None, None, "sha256:other"),
])
def test_two_workers_need_a_measured_benefit_over_the_sequential_baseline(db, fakes, problem, candidate, baseline,
                                                                         conditions):
    """§44 (re-audit): any single gate-passing EvalRun used to be accepted; the bounded-parallel topology must be
    shown better than the sequential baseline on the same configuration for that task class."""
    cand, base = _topology_runs(db, candidate_metrics=candidate, baseline_metrics=baseline,
                                baseline_conditions=conditions)
    with pytest.raises(Blocked) as exc:
        scheduler.set_concurrency(db, max_active=2, actor="user:o", actor_user_id="owner-1", rationale="uji dua worker",
                                  eval_run_id=cand.id, decision_record=RECORD, scope={"risk_classes": ["R1"]},
                                  baseline_eval_run_id=None if problem == "no baseline" else base.id)
    assert exc.value.code == "CONCURRENCY_BENEFIT_NOT_PROVEN", problem
    assert scheduler.concurrency_cap(db) == (1, None)


def test_concurrency_endpoint_requires_super_admin_and_evidence(personal_client, db):
    r = personal_client.post("/api/workforce/concurrency", json={"max_active": 2, "rationale": "uji dua worker",
                                                                  "decision_record": RECORD})
    assert r.status_code in (403, 409) and r.json()["error"]["code"] == "CONCURRENCY_EVIDENCE_MISSING"
    ok, baseline = _topology_runs(db)
    db.commit()
    r = personal_client.post("/api/workforce/concurrency", json={
        "max_active": 2, "rationale": "uji dua worker", "eval_run_id": ok.id, "decision_record": RECORD,
        "baseline_eval_run_id": baseline.id, "scope": {"risk_classes": ["R1", "R2"]}})
    assert r.status_code == 200, r.text
    assert r.json()["effective_cap"] == 2
    d = db.get(Decision, r.json()["decision_id"])
    assert d.kind == "POLICY" and d.actor_kind == "HUMAN" and d.target_id == "concurrency"


# ----------------------------------------------------------------------------- §19 admission answers
def test_host_without_free_memory_waits_and_compute_is_reserved(db, fakes, monkeypatch):
    import psutil

    _p, _pv, specs = _env(db, fakes, [{"task_key": "a", "limits": {"max_memory_mb": 2048, "max_cpus": 2}}])
    # a 3 GB host: after the 2 GB host reserve, 1 GB is left for workers - not enough for a 2 GB run
    monkeypatch.setattr(psutil, "virtual_memory", lambda: types.SimpleNamespace(total=3 * 1024 ** 3,
                                                                                available=1024 ** 3))
    adm = scheduler.admission_check(db, specs[0].id)
    assert "RESOURCE_INSUFFICIENT" in _codes(adm) and adm.answers["compute"]["status"] == "INSUFFICIENT"
    # the reservation is checked against capacity, not against other processes' momentary memory use: a busy
    # 64 GB host with little free memory still admits (and the observation is recorded as a warning)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: types.SimpleNamespace(total=64 * 1024 ** 3,
                                                                                available=256 * 1024 ** 2))
    monkeypatch.setattr(psutil, "cpu_count", lambda logical=True: 4)
    adm = scheduler.admission_check(db, specs[0].id)
    assert adm.admitted and "below this run's reservation" in adm.answers["compute"]["warning"]
    run = scheduler.acquire_lease(db, specs[0].id, actor="t", admission=adm)
    from aeos.worker_runtime import processes

    # wave 7 (W6-055): the process sandbox caps CPU on Windows (Job Object hard cap); elsewhere a reservation only
    assert run.caps["compute"] == {"memory_mb": 2048, "cpus": 2.0, "status": "OK", "enforced": True,
                                   "memory_enforced": True, "cpu_enforced": processes.cpu_cap_supported(),
                                   "sandbox_kind": "process"}
    assert scheduler._reserved_compute(db, exclude_task=None) == (2048, 2.0)
    # LEASED and RUNNING runs keep their reservation: a host whose remaining budget is too small waits
    monkeypatch.setattr(psutil, "virtual_memory", lambda: types.SimpleNamespace(total=6 * 1024 ** 3,
                                                                                available=4 * 1024 ** 3))
    other = scheduler.compute_check(db, types.SimpleNamespace(id="other", limits={"max_memory_mb": 3072}),
                                    active=1, cap=2)
    assert other["status"] == "INSUFFICIENT" and other["reserved_mb"] == 2048 and other["worker_budget_mb"] == 4096
    # ... and CPUs are reserved too: 2 reserved + 3 requested exceed a 4-CPU host even when memory fits
    monkeypatch.setattr(psutil, "virtual_memory", lambda: types.SimpleNamespace(total=64 * 1024 ** 3,
                                                                                available=32 * 1024 ** 3))
    cpu = scheduler.compute_check(db, types.SimpleNamespace(id="other", limits={"max_memory_mb": 1024, "max_cpus": 3}),
                                  active=1, cap=2)
    assert cpu["status"] == "INSUFFICIENT" and cpu["reserved_cpus"] == 2.0 and "CPU" in cpu["reason"]


def test_reserved_compute_is_exactly_the_cap_the_runtime_enforces(db, fakes):
    """§19/§21/§22 (re-audit): admission used to reserve a nominal 512 MB while the runtime let the run grow to its
    4096 MB cap - two admitted workers could use ~8x the reservation. The reservation is now the enforced cap."""
    from aeos.planning import dag
    from aeos.worker_runtime import container, processes

    for limits in ({}, {"max_memory_mb": 1536}, dict(dag.RESOURCE_LIMITS)):
        task = types.SimpleNamespace(id="t", limits=limits)
        proc = scheduler.run_compute_limits(task, sandbox_kind="process")
        assert proc["memory_mb"] == processes.ResourceLimits.from_task(limits).max_memory_mb  # the watchdog's RSS cap
        box = scheduler.run_compute_limits(task, sandbox_kind="container")
        assert f"{box['memory_mb']}m" == container.limits_for(limits)["memory"]  # docker --memory
        assert box["cpus"] == float(container.limits_for(limits)["cpus"]) and box["cpu_enforced"] is True
    # the compiler writes the runtime cap into every TaskSpec (fixed by the control plane, not by model output)
    assert dag.RESOURCE_LIMITS["max_memory_mb"] == processes.DEFAULT_MAX_MEMORY_MB
    _p, _pv, specs = _env(db, fakes, [{"task_key": "a"}])
    adm = scheduler.admission_check(db, specs[0].id)
    assert adm.admitted, adm.reasons
    assert adm.answers["compute"]["memory_mb"] == processes.DEFAULT_MAX_MEMORY_MB != 512


def test_context_larger_than_the_route_window_is_refused(db, fakes):
    _p, _pv, specs = _env(db, fakes, [{"task_key": "a", "limits": {"max_output_tokens": 16000}}])
    route = db.scalar(select(scheduler.ModelRoute))
    route.context_window = 8000
    db.flush()
    adm = scheduler.admission_check(db, specs[0].id)
    assert "CONTEXT_TOO_LARGE" in _codes(adm) and adm.answers["context"]["context_window"] == 8000
    route.context_window = 200_000
    db.flush()
    adm = scheduler.admission_check(db, specs[0].id)
    assert adm.admitted and adm.answers["context"]["status"] == "FITS"


def test_context_estimate_counts_the_attachments_and_context_pack_the_worker_receives(db, fakes, monkeypatch):
    """§19 "berapa konteks yang harus disalin" (re-audit): the estimate used to leave out the resolved
    input-artifact content and the ContextPack (up to ~300k characters), so admission could report FITS for a
    prompt larger than the route window."""
    from aeos.worker_runtime import executor

    big = [{"kind": "attachment", "ref": f"attachment:a{i}", "name": f"brief{i}.md",
            "content": "x" * executor.MAX_ATTACHMENT_CHARS} for i in range(4)]  # 80k chars, within the runtime caps
    _p, _pv, specs = _env(db, fakes, [{"task_key": "a", "limits": {"max_output_tokens": 1000}}])
    route = db.scalar(select(scheduler.ModelRoute))
    route.context_window = 20_000
    db.flush()
    te = scheduler.get_te(db, specs[0].id)
    base = scheduler.context_estimate(db, specs[0], te, route)
    assert base["status"] == "FITS" and base["attachments"] == 0
    monkeypatch.setattr(executor, "resolve_artifacts", lambda s, t: big)  # what the runtime copies into the prompt
    with_data = scheduler.context_estimate(db, specs[0], te, route)
    assert with_data["status"] == "TOO_LARGE" and with_data["attachments"] == 4
    assert with_data["prompt_tokens"] >= base["prompt_tokens"] + 80_000 // scheduler.CHARS_PER_TOKEN
    monkeypatch.setattr(executor, "resolve_artifacts", lambda s, t: [])
    pack = {"id": "cp1", "hash": "sha256:p", "items": [{"kind": "doc", "ref": "d", "trust": "T", "content": "y" * 90_000}]}
    monkeypatch.setattr(executor, "_context_pack", lambda s, t, b: (pack, {"pack_id": "cp1", "used": True}))
    with_pack = scheduler.context_estimate(db, specs[0], te, route)
    assert with_pack["status"] == "TOO_LARGE" and with_pack["context_pack_used"] is True
    adm = scheduler.admission_check(db, specs[0].id)
    assert "CONTEXT_TOO_LARGE" in _codes(adm)


def test_admission_records_verifier_and_reviewer_path(db, fakes):
    _p, _pv, specs = _env(db, fakes, [{"task_key": "a"}])
    adm = scheduler.admission_check(db, specs[0].id)
    assert adm.admitted
    v = adm.answers["verifier"]
    assert v["suite"]["status"] == "BLOCKED" and v["suite"]["code"] == "NO_TRUSTED_SUITE"  # stated, not hidden
    assert v["reviewer_path"].startswith("native") and "AcceptanceRecord" in v["result_receiver"]
    run = scheduler.acquire_lease(db, specs[0].id, actor="t", admission=adm)
    assert run.caps["verifier_path"]["suite"]["code"] == "NO_TRUSTED_SUITE"


# ----------------------------------------------------------------------------- §46 evaluation-derived eligibility
def test_role_eligibility_from_evaluation_bound_to_the_revision(db, fakes):
    _p, _pv, specs = _env(db, fakes, [{"task_key": "a"}])  # risk class R1 (TaskSpec default)
    scheduler._set_setting(db, scheduler.KEY_ROLE_EVAL_FROM, "R1")
    adm = scheduler.admission_check(db, specs[0].id)
    assert "ROLE_NOT_EVALUATED" in _codes(adm) and adm.answers["role_eligibility"]["status"] == "NOT_EVALUATED"
    rev = roles.role_revision(db, "feature-implementer")
    _eval_run(db, role_key="feature-implementer", configuration={"role_revision_hash": "sha256:old"})
    assert roles.role_evaluation(db, "feature-implementer", rev)["status"] == "STALE"  # other revision only
    run = _eval_run(db, role_key="feature-implementer", configuration={"role_revision_hash": rev})
    assert roles.refresh_role_eval_status(db, "feature-implementer") == 1
    row = db.scalar(select(RoleSkillPack).where(RoleSkillPack.role_key == "feature-implementer"))
    assert row.eval_status == "PASS"
    # §44 (review): the PASS result is eligibility only once the owner adopted it as a DecisionRecord
    adm = scheduler.admission_check(db, specs[0].id)
    assert "ROLE_RESULT_NOT_ADOPTED" in _codes(adm)
    assert adm.answers["role_eligibility"]["eligibility_basis"] == "EVALUATION_RESULT_NOT_ADOPTED"
    scheduler.adopt_role_result(db, role_key="feature-implementer", eval_run_id=run.id, actor="user:owner",
                                actor_user_id="owner", rationale="hasil eval diadopsi", decision_record=RECORD)
    assert scheduler.admission_check(db, specs[0].id).admitted


def test_role_result_adoption_is_an_owner_decision_record(personal_client, db, fakes):
    """§44 "Hasil routing/role/mode menjadi DecisionRecord: opsi, tujuan, evidence, cost/quality tradeoff, owner AUTH,
    scope, versi dan kondisi yang membatalkannya" (review): a PASS role evaluation used to confer admission
    eligibility by itself, with no owner AUTH, scope or invalidation condition."""
    _p, _pv, specs = _env(db, fakes, [{"task_key": "a"}])
    scheduler._set_setting(db, scheduler.KEY_ROLE_EVAL_FROM, "R0")
    rev = roles.role_revision(db, "feature-implementer")
    failed = _eval_run(db, role_key="feature-implementer", configuration={"role_revision_hash": rev}, accepted=0)
    db.commit()
    url = "/api/workforce/roles/feature-implementer/adopt"
    body = {"eval_run_id": failed.id, "rationale": "adopsi hasil eval", "decision_record": RECORD}
    r = personal_client.post(url, json=body)  # not a PASS result
    assert r.status_code == 409 and r.json()["error"]["code"] == "ROLE_RESULT_NOT_ADOPTABLE"
    run = _eval_run(db, role_key="feature-implementer", configuration={"role_revision_hash": rev})
    db.commit()
    r = personal_client.post(url, json={**body, "eval_run_id": run.id, "decision_record": {"goal": "x"}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "DECISION_RECORD_INCOMPLETE"
    # the generic decision endpoint can never record an adoption without these checks
    g = personal_client.post("/api/decisions", json={"kind": "POLICY", "target_type": "role_adoption",
                                                     "target_id": "feature-implementer", "auth_scope": {"x": "y"},
                                                     "rationale": "adopsi lewat jalur umum"})
    assert g.status_code == 422 and g.json()["error"]["code"] == "DEDICATED_ENDPOINT_REQUIRED"
    # scoped to another project: this task stays not eligible
    r = personal_client.post(url, json={**body, "eval_run_id": run.id, "scope": {"project_ids": ["other-project"]}})
    assert r.status_code == 200, r.text
    db.expire_all()
    elig = scheduler.admission_check(db, specs[0].id).answers["role_eligibility"]
    assert elig["eligible"] is False and "outside the adoption scope" in elig["adoption_problem"]
    r = personal_client.post(url, json={**body, "eval_run_id": run.id, "scope": {"project_ids": [specs[0].project_id]}})
    decision_id = r.json()["decision_id"]
    db.expire_all()
    adm = scheduler.admission_check(db, specs[0].id)
    assert adm.admitted and adm.answers["role_eligibility"]["eligibility_basis"] == "EVALUATION_RESULT"
    d = db.get(Decision, decision_id)
    assert d.kind == "POLICY" and d.actor_kind == "HUMAN" and d.target_hash == rev
    assert d.details["decision_record"]["evidence_ids"] == [run.id] and d.details["eval_run_id"] == run.id
    assert d.details["rollback"]["how"].startswith("revoke")
    # a newer result of the same revision is a new result: the old adoption no longer applies
    newer = _eval_run(db, role_key="feature-implementer", configuration={"role_revision_hash": rev})
    newer.updated_at = utcnow() + timedelta(seconds=5)
    db.flush()
    assert "ROLE_RESULT_NOT_ADOPTED" in _codes(scheduler.admission_check(db, specs[0].id))
    # revoking the adoption (rollback: the decision is no longer ACTIVE) returns the role to "not eligible"
    db.rollback()
    assert scheduler.admission_check(db, specs[0].id).admitted
    db.get(Decision, decision_id).status = "INVALIDATED"
    db.flush()
    assert "ROLE_RESULT_NOT_ADOPTED" in _codes(scheduler.admission_check(db, specs[0].id))


def test_role_adoption_through_the_real_registry_is_human_and_revision_bound(db):
    """The scheduler tests stub the policy registry; this one records and reads the adoption through the real
    decision registry: a human actor is required and the decision binds exactly the role revision."""
    from aeos.core.errors import PolicyDenied
    from tests.conftest import create_admin

    seed.run(db)
    owner = create_admin(db)
    rev = roles.role_revision(db, "feature-implementer")
    run = _eval_run(db, role_key="feature-implementer", configuration={"role_revision_hash": rev})
    with pytest.raises(PolicyDenied):  # never an agent/unknown actor
        scheduler.adopt_role_result(db, role_key="feature-implementer", eval_run_id=run.id, actor="worker:x",
                                    actor_user_id="not-a-user", rationale="adopsi otomatis", decision_record=RECORD)
    out = scheduler.adopt_role_result(db, role_key="feature-implementer", eval_run_id=run.id, actor=f"user:{owner.id}",
                                      actor_user_id=owner.id, rationale="hasil eval diadopsi", decision_record=RECORD)
    task = types.SimpleNamespace(role_key="feature-implementer", risk_class="R4", project_id="p", domain="PERSONAL")
    decision, problem = scheduler.role_adoption(db, task, revision=rev, run_id=run.id)
    assert decision is not None and decision.id == out["decision_id"] and problem is None
    assert decision.actor_kind == "HUMAN" and decision.target_hash == rev
    # another role revision (a changed RoleSkillPack) is never covered by this adoption
    assert scheduler.role_adoption(db, task, revision="sha256:" + "0" * 64, run_id=run.id)[0] is None
    elig = scheduler.role_eligibility(db, task)
    assert elig["required"] and elig["eligible"] and elig["eligibility_basis"] == "EVALUATION_RESULT"
    assert elig["adoption_decision_id"] == out["decision_id"]


def test_owner_exception_admits_an_unevaluated_role(db, fakes):
    _p, _pv, specs = _env(db, fakes, [{"task_key": "a"}])
    scheduler._set_setting(db, scheduler.KEY_ROLE_EVAL_FROM, "R0")
    assert "ROLE_NOT_EVALUATED" in _codes(scheduler.admission_check(db, specs[0].id))
    db.add(Decision(kind="EXCEPTION", actor_user_id="owner", actor_kind="HUMAN", target_type="role_evaluation",
                    target_id="feature-implementer", target_hash=roles.role_revision(db, "feature-implementer"),
                    rationale="pilot sebelum eval", status="ACTIVE"))
    db.flush()
    assert scheduler.admission_check(db, specs[0].id).admitted


def test_default_threshold_r4_keeps_the_pilot_flow(db, fakes):
    _p, _pv, specs = _env(db, fakes, [{"task_key": "a"}])
    elig = scheduler.admission_check(db, specs[0].id).answers["role_eligibility"]
    assert elig["required"] is False and elig["threshold"] == "R4" and elig["status"] == "NOT_EVALUATED"
    # §46 (re-audit): below the threshold the admission states that eligibility does NOT come from an evaluation
    assert elig["eligibility_basis"] == "PILOT_THRESHOLD_UNEVALUATED" and "BUKAN dari hasil evaluasi" in elig["note"]


def test_owner_tightens_the_role_evaluation_threshold(personal_client, db, fakes):
    """§46 (re-audit): eligibility from evaluation results applied only from R4 and the owner had no way to require it
    for lower risk classes. The owner sets the threshold; admission then waits for a PASS evaluation."""
    _p, _pv, specs = _env(db, fakes, [{"task_key": "a"}])
    db.commit()
    bad = personal_client.post("/api/workforce/role-eval-threshold", json={"risk_class": "R9", "rationale": "ketat"})
    assert bad.status_code == 422
    r = personal_client.post("/api/workforce/role-eval-threshold",
                             json={"risk_class": "R0", "rationale": "semua role wajib dievaluasi"})
    assert r.status_code == 200, r.text
    assert r.json() == {"previous": "R4", "threshold": "R0"}
    db.expire_all()
    adm = scheduler.admission_check(db, specs[0].id)
    elig = adm.answers["role_eligibility"]
    assert "ROLE_NOT_EVALUATED" in _codes(adm) and elig["eligibility_basis"] == "NOT_ELIGIBLE"
    rev = roles.role_revision(db, "feature-implementer")
    run = _eval_run(db, role_key="feature-implementer", configuration={"role_revision_hash": rev})
    roles.refresh_role_eval_status(db, "feature-implementer")
    scheduler.adopt_role_result(db, role_key="feature-implementer", eval_run_id=run.id, actor="user:owner",
                                actor_user_id="owner", rationale="hasil eval diadopsi", decision_record=RECORD)
    adm = scheduler.admission_check(db, specs[0].id)
    assert adm.admitted and adm.answers["role_eligibility"]["eligibility_basis"] == "EVALUATION_RESULT"
    from aeos.models.audit import AuditEvent

    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "workforce.role_eval_threshold")) is not None


# ----------------------------------------------------------------------------- §17/§46 RoleSkillPack contract
def test_every_executor_role_has_a_full_competence_definition(db):
    seed.run(db)
    for spec in catalog.EXECUTOR_ROLES:
        row = db.scalar(select(RoleSkillPack).where(RoleSkillPack.role_key == spec["role_key"]))
        assert row.inputs and row.outputs and row.example_outputs and row.failure_modes, spec["role_key"]
        assert row.allowed_scope["write"] == "TaskSpec.allowed_paths only", spec["role_key"]
        assert len(row.practice_sources) >= 3 and "reference only" in row.practice_sources[-1]
        # §46 (re-audit): named, verifiable practice sources (publication + canonical URL), not generic labels
        assert "https://" in row.practice_sources[-1], spec["role_key"]
        # §46 (re-audit): every executor RoleSkillPack carries protected acceptance/evaluation cases
        assert row.evaluation_cases and all(c["case_ids"] for c in row.evaluation_cases), spec["role_key"]
    impl = db.scalar(select(RoleSkillPack).where(RoleSkillPack.role_key == "feature-implementer"))
    assert {c["suite_key"] for c in impl.evaluation_cases} >= {"eval.small-bugfix", "eval.feature-fe-api"}
    analyst = db.scalar(select(RoleSkillPack).where(RoleSkillPack.role_key == "product-analyst"))
    # cases exist, but no evaluation has run: still NOT_EVALUATED (never PASS by default)
    assert [c["suite_key"] for c in analyst.evaluation_cases] == ["eval.product-requirements"]
    assert analyst.eval_status == "NOT_EVALUATED"


def test_worker_prompt_carries_the_role_contract(db):
    from aeos.models.planning import TaskSpec
    from aeos.worker_runtime import executor

    seed.run(db)
    t = TaskSpec(task_key="k", title="t", goal="g", role_key="product-analyst", allowed_paths=["docs/**"],
                 acceptance_criteria=["AC"], expected_outputs=["PRD"], plan_version_id="v", plan_version_hash="h",
                 project_id="p", domain="PERSONAL", spec_hash="s")
    contract = roles.role_contract(db, "product-analyst")
    text = executor.build_prompt(t, findings=[], protected=(), branch="aeos/k", role_contract=contract)
    assert "## Role contract (Product Analyst" in text and contract["revision_hash"][:19] in text
    assert "Tidak mengarang stakeholder atau keputusan bisnis" in text
    assert "Mengarang stakeholder/keputusan bisnis" in text  # failure modes are part of the contract
    assert roles.role_contract(db, "chief-executive-officer") is None


def test_only_one_agent_and_role_registry_exists():
    """§17 "AgentDefinition memperluas kontrak worker yang ada": one registry (models.workforce), no duplicate."""
    root = Path(__file__).resolve().parents[2] / "backend" / "aeos"
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for name in ("class AgentDefinition(", "class RoleSkillPack(", '__tablename__ = "agent_definitions"',
                     '__tablename__ = "role_skill_packs"'):
            if name in text and path.relative_to(root).as_posix() != "models/workforce.py":
                offenders.append((path.relative_to(root).as_posix(), name))
    assert offenders == []


# ----------------------------------------------------------------------------- §35 project-scoped agents
def test_agent_outside_its_domain_scope_is_hidden_and_refused(db, fakes):
    _p, _pv, specs = _env(db, fakes, [{"task_key": "a"}])
    agent = db.scalar(select(AgentDefinition).where(AgentDefinition.agent_key == "opencode-implementer"))
    agent.allowed_scope = {**agent.allowed_scope, "domains": ["OFFICE"]}
    db.flush()
    assert "opencode-implementer" not in {a["agent_key"] for a in views.agents_view(db, domain="PERSONAL")}
    assert "opencode-implementer" in {a["agent_key"] for a in views.agents_view(db, domain="OFFICE")}
    assert "AGENT_OUT_OF_SCOPE" in _codes(scheduler.admission_check(db, specs[0].id))


def test_agent_definition_tools_and_route_policy_configure_admission(db, fakes):
    """§17 (re-audit): AgentDefinition.tools and route_policy used to be display-only; the runtime tool grant, the
    TaskSpec's bash/webfetch requests and the selected route are now checked against them."""
    from aeos.worker_runtime.opencode import BASE_ALLOW

    _p, _pv, specs = _env(db, fakes, [{"task_key": "a"}, {"task_key": "b", "tools_allowed": ["fs.read", "bash"]}])
    agent = db.scalar(select(AgentDefinition).where(AgentDefinition.agent_key == "opencode-implementer"))
    assert set(BASE_ALLOW) <= set(agent.tools)  # the seeded envelope covers what every worker run receives
    assert scheduler.admission_check(db, specs[0].id).admitted
    assert "AGENT_TOOL_NOT_ALLOWED" not in _codes(scheduler.admission_check(db, specs[1].id))  # deny unless TaskSpec
    agent.tools = [t for t in agent.tools if t != "edit"]
    db.flush()
    adm = scheduler.admission_check(db, specs[0].id)
    assert not adm.admitted and "AGENT_TOOLS_INSUFFICIENT" in _codes(adm)
    agent.tools = list(catalog.DEFAULT_IMPLEMENTER_TOOLS)
    agent.limits = {**agent.limits, "bash": "deny"}
    db.flush()
    assert "AGENT_TOOL_NOT_ALLOWED" in _codes(scheduler.admission_check(db, specs[1].id))
    assert scheduler.admission_check(db, specs[0].id).admitted
    agent.route_policy = {**agent.route_policy, "pool": "LOCAL"}
    db.flush()
    assert "AGENT_ROUTE_POLICY" in _codes(scheduler.admission_check(db, specs[0].id))
    agent.route_policy = {**agent.route_policy, "pool": "WORKER", "allowed_routes": ["worker.other-route"]}
    db.flush()
    assert "AGENT_ROUTE_POLICY" in _codes(scheduler.admission_check(db, specs[0].id))
    agent.route_policy = {**agent.route_policy, "allowed_routes": ["worker.test-free"]}
    db.flush()
    assert scheduler.admission_check(db, specs[0].id).admitted


def test_reviewer_and_verifier_availability_follow_their_real_gates(db, fakes):
    seed.run(db)
    by = {a["agent_key"]: a for a in views.agents_view(db, domain="PERSONAL")}
    assert by["claude-checkpoint-reviewer"]["enabled"] is True  # fake doctor: CAN_REVIEW_WITH_MAX READY
    assert by["claude-checkpoint-reviewer"]["enabled_source"] == "owning module gate"
    assert by["independent-verifier"]["availability"] == "ENABLED_IDLE"
    fakes.capability = "BLOCKED"
    by = {a["agent_key"]: a for a in views.agents_view(db, domain="PERSONAL")}
    assert by["claude-checkpoint-reviewer"]["availability"] == "DISABLED"
    assert "CAN_REVIEW_WITH_MAX" in by["claude-checkpoint-reviewer"]["enabled_reason"]


# ----------------------------------------------------------------------------- §17 org view shows real activity
def test_org_view_counts_running_planning_stages_and_open_reviews(db):
    from aeos.models.evidence import ReviewRecord

    seed.run(db)
    p = make_project(db)
    plan = Plan(project_id=p.id, domain="PERSONAL", title="t", state="CHALLENGE_REVIEW")
    db.add(plan)
    db.flush()
    db.add(PlanningRun(plan_id=plan.id, project_id=p.id, stage="CHALLENGE_REVIEW", role="Planning Challenger",
                       status="RUNNING"))
    in_review = WorkerRun(project_id=p.id, domain="PERSONAL", attempt=1, execution_status="REVIEW", mode="LIVE")
    finished = WorkerRun(project_id=p.id, domain="PERSONAL", attempt=1, execution_status="CANCELLED", mode="LIVE")
    db.add_all([in_review, finished])
    db.flush()
    db.add(ReviewRecord(target_type="worker_run", target_id=in_review.id, run_id=in_review.id, project_id=p.id,
                        domain="PERSONAL", reviewer_kind="SECURITY", verdict="NOT-PROVEN", status="WAITING_QUOTA",
                        mode="LIVE"))
    # an uncollected manual package of a run that left REVIEW is history, not current activity
    db.add(ReviewRecord(target_type="worker_run", target_id=finished.id, run_id=finished.id, project_id=p.id,
                        domain="PERSONAL", reviewer_kind="CLAUDE", verdict="NOT-PROVEN", status="NEEDS_DECISION",
                        package_ref="pkg-old", mode="LIVE"))
    db.flush()
    rows = {r["role_key"]: r for g in views.roles_view(db, domain="PERSONAL")["groups"] for r in g["roles"]}
    assert rows["planning-challenger"]["availability"] == "ACTIVE"
    assert rows["security-reviewer"]["availability"] == "ACTIVE"
    assert rows["primary-planner"]["availability"] == "AVAILABLE_INACTIVE"
    assert rows["feature-architecture-reviewer"]["active_runs"] == 0
    office = {r["role_key"]: r for g in views.roles_view(db, domain="OFFICE")["groups"] for r in g["roles"]}
    assert office["planning-challenger"]["active_runs"] == 0  # domain scoped
    scoped = {r["role_key"]: r for g in views.roles_view(db, domain="PERSONAL", project_ids=[])["groups"]
              for r in g["roles"]}
    assert scoped["planning-challenger"]["active_runs"] == 0  # project scoped


# ----------------------------------------------------------------------------- §35 Live Work provenance
def test_current_action_source_follows_the_writer(db, fakes):
    _p, _pv, specs = _env(db, fakes, [{"task_key": "a"}])
    run = scheduler.acquire_lease(db, specs[0].id, actor="t")
    assert scheduler.record_action(db, run.id, lease_id=run.lease_id,
                                   action={"kind": "edit_file", "label": "Mengubah src/a/x.py", "path": "src/a/x.py",
                                           "status": "completed"})
    row = views.run_row(db, run)
    assert row["current_action"] == "Mengubah src/a/x.py" and row["current_action_writer"] == "RUNNER"
    assert row["current_action_source"] == "observed tool action (runner)"
    # narration never becomes the current action
    scheduler.record_action(db, run.id, lease_id=run.lease_id, action={"kind": "narration", "label": "Saya selesai!"})
    assert views.run_row(db, run)["current_action"] == "Mengubah src/a/x.py"
    # controller status text is labelled as such
    run.current_action = "WAITING_QUOTA: upstream 429 (retry-after 60s)"
    db.flush()
    row = views.run_row(db, run)
    assert row["current_action_writer"] == "CONTROLLER" and "controller status" in row["current_action_source"]


def test_live_work_row_lists_external_resources_touched(db, fakes):
    _p, _pv, specs = _env(db, fakes, [{"task_key": "a"}])
    run = scheduler.acquire_lease(db, specs[0].id, actor="t")
    db.add(BrokerSideEffect(run_id=run.id, project_id=run.project_id, kind="push_branch", target="owner/repo:aeos/a",
                            status="DONE", idempotency_key="k1"))
    db.flush()
    row = views.run_row(db, run)
    assert row["resources"] == [{"kind": "push_branch", "target": "owner/repo:aeos/a", "status": "DONE",
                                 "external_url": None}]


# ----------------------------------------------------------------------------- §42 scheduler never calls an LLM
def test_scheduler_ticks_and_heartbeats_never_invoke_the_claude_bridge(settings, fakes, monkeypatch):
    from aeos.workforce import loop

    def _boom(*_a, **_k):
        raise AssertionError("the scheduler must not call an LLM")

    fake_bridge = types.ModuleType("aeos.claude_bridge.bridge")
    fake_bridge.run_structured = _boom
    monkeypatch.setitem(sys.modules, "aeos.claude_bridge.bridge", fake_bridge)
    from aeos.claude_bridge import facade as bridge_facade

    monkeypatch.setattr(bridge_facade, "run_structured", _boom)
    with session_factory()() as db:
        _p, _pv, specs = _env(db, fakes, [{"task_key": "a"}, {"task_key": "b"}])
        scheduler.set_scheduler_enabled(db, True, actor="t")
        db.commit()
    lp = loop.SchedulerLoop(settings)
    for _ in range(3):
        lp.tick(start_threads=False)
    with session_factory()() as db:
        run = db.scalar(select(WorkerRun))
        for _ in range(3):
            assert scheduler.heartbeat(db, run.id, lease_id=run.lease_id)
        scheduler.reconcile(db)
        db.commit()


# ----------------------------------------------------------------------------- review (C7): §23 OFFICE route binding
def test_office_task_waits_until_the_route_office_decision_holds(db, fakes, monkeypatch):
    """§23 controlled path: an OFFICE task runs on a route only while the owner's office_route decision (company
    authorisation + company-approved route, bound to the route identity) holds; revoking it ends admission."""
    from aeos.policy import office_routes

    project = make_project(db, domain="OFFICE")
    make_route(db, domains=["PERSONAL", "OFFICE"])
    _p, _pv, specs = _env(db, fakes, [{"task_key": "a"}], project=project)
    adm = scheduler.admission_check(db, specs[0].id)
    assert "OFFICE_ROUTE_NOT_AUTHORISED" in _codes(adm) and not adm.admitted
    monkeypatch.setattr(office_routes, "office_route_authorised", lambda s, r: types.SimpleNamespace(id="d-1"))
    assert "OFFICE_ROUTE_NOT_AUTHORISED" not in _codes(scheduler.admission_check(db, specs[0].id))
    assert "OFFICE_ROUTE_NOT_AUTHORISED" in views.BLOCKING_WAIT_CODES


def test_imported_candidate_pack_is_shown_as_candidate_not_available(db):
    seed.run(db)
    db.add(RoleSkillPack(role_key="ext-helper", name="Ext Helper", group="fe-api-data", kind="EXECUTOR",
                         version="1", revision_hash="sha256:x", eval_status="NOT_EVALUATED", mode="CANDIDATE"))
    db.flush()
    rows = {r["role_key"]: r for g in views.roles_view(db, domain="PERSONAL")["groups"] for r in g["roles"]}
    assert rows["ext-helper"]["availability"] == "CANDIDATE"
    assert rows["feature-implementer"]["availability"] == "AVAILABLE_INACTIVE"


def test_skill_pack_text_is_redacted_before_it_is_stored():
    from aeos.workforce import skill_import

    pack = {"role_key": "ext-leaky", "name": "Leaky", "group": "fe-api-data", "inputs": ["x"],
            "outputs": ["password=hunter2-very-secret"], "boundaries": ["b"],
            "forbidden_actions": list(catalog.COMMON_FORBIDDEN), "verification": ["v"], "failure_modes": ["f"]}
    clean = skill_import.validate_pack(pack)
    assert "hunter2-very-secret" not in clean["outputs"][0]


def test_viewer_cannot_change_concurrency_or_import_skill_packs(client, db):
    """§19/§45: the concurrency mode and a skill-pack import are owner (SUPER_ADMIN) actions."""
    from tests.conftest import VIEWER_PW, create_admin, create_viewer

    create_admin(db)
    create_viewer(db)
    client.login("viewer", VIEWER_PW)
    assert client.post("/api/session/context", json={"domain": "PERSONAL"}).status_code == 200
    ok = _eval_run(db)
    db.commit()
    r = client.post("/api/workforce/concurrency", json={"max_active": 2, "rationale": "uji dua worker",
                                                         "eval_run_id": ok.id, "decision_record": RECORD})
    assert r.status_code == 403, r.text
    r = client.post("/api/workforce/role-packs/import", json={
        "pack": {"role_key": "ext-x"}, "source_url": "https://example.org/x", "commit_pin": "a" * 40,
        "license_spdx": "MIT", "rationale": "uji impor oleh viewer"})
    assert r.status_code == 403, r.text
    assert scheduler.concurrency_cap(db) == (1, None)
    assert db.scalar(select(Decision).where(Decision.target_type == "workforce")) is None
