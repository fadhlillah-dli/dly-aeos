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
    ok = _eval_run(db)
    with pytest.raises(ValidationFailed) as exc:
        scheduler.set_concurrency(db, max_active=2, actor="user:o", actor_user_id=owner, rationale="uji dua worker",
                                  eval_run_id=ok.id, decision_record={"goal": "x"})
    assert exc.value.code == "DECISION_RECORD_INCOMPLETE"
    out = scheduler.set_concurrency(db, max_active=2, actor="user:o", actor_user_id=owner, rationale="uji dua worker",
                                    eval_run_id=ok.id, decision_record=RECORD,
                                    scope={"risk_classes": ["R1"], "bogus": ["x"]})
    assert seen["kind"] == "POLICY" and (seen["target_type"], seen["target_id"]) == ("workforce", "concurrency")
    assert seen["details"]["rollback"]["previous_max_active"] == 1 and seen["details"]["eval_run_id"] == ok.id
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


def test_concurrency_endpoint_requires_super_admin_and_evidence(personal_client, db):
    r = personal_client.post("/api/workforce/concurrency", json={"max_active": 2, "rationale": "uji dua worker",
                                                                  "decision_record": RECORD})
    assert r.status_code in (403, 409) and r.json()["error"]["code"] == "CONCURRENCY_EVIDENCE_MISSING"
    ok = _eval_run(db)
    db.commit()
    r = personal_client.post("/api/workforce/concurrency", json={
        "max_active": 2, "rationale": "uji dua worker", "eval_run_id": ok.id, "decision_record": RECORD})
    assert r.status_code == 200, r.text
    assert r.json()["effective_cap"] == 2
    d = db.get(Decision, r.json()["decision_id"])
    assert d.kind == "POLICY" and d.actor_kind == "HUMAN" and d.target_id == "concurrency"


# ----------------------------------------------------------------------------- §19 admission answers
def test_host_without_free_memory_waits_and_compute_is_reserved(db, fakes, monkeypatch):
    import psutil

    _p, _pv, specs = _env(db, fakes, [{"task_key": "a", "limits": {"memory_mb": 2048}}])
    monkeypatch.setattr(psutil, "virtual_memory", lambda: types.SimpleNamespace(available=1024 * 1024 * 1024))
    adm = scheduler.admission_check(db, specs[0].id)
    assert "RESOURCE_INSUFFICIENT" in _codes(adm) and adm.answers["compute"]["status"] == "INSUFFICIENT"
    monkeypatch.setattr(psutil, "virtual_memory", lambda: types.SimpleNamespace(available=64 * 1024 ** 3))
    run = scheduler.acquire_lease(db, specs[0].id, actor="t")
    assert run.caps["compute"] == {"memory_mb": 2048, "status": "OK", "enforced": False}
    assert scheduler._reserved_compute_mb(db, exclude_task=None) == 2048


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
    _eval_run(db, role_key="feature-implementer", configuration={"role_revision_hash": rev})
    assert roles.refresh_role_eval_status(db, "feature-implementer") == 1
    row = db.scalar(select(RoleSkillPack).where(RoleSkillPack.role_key == "feature-implementer"))
    assert row.eval_status == "PASS"
    assert scheduler.admission_check(db, specs[0].id).admitted


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


# ----------------------------------------------------------------------------- §17/§46 RoleSkillPack contract
def test_every_executor_role_has_a_full_competence_definition(db):
    seed.run(db)
    for spec in catalog.EXECUTOR_ROLES:
        row = db.scalar(select(RoleSkillPack).where(RoleSkillPack.role_key == spec["role_key"]))
        assert row.inputs and row.outputs and row.example_outputs and row.failure_modes, spec["role_key"]
        assert row.allowed_scope["write"] == "TaskSpec.allowed_paths only", spec["role_key"]
        assert len(row.practice_sources) >= 3 and "reference only" in row.practice_sources[-1]
    impl = db.scalar(select(RoleSkillPack).where(RoleSkillPack.role_key == "feature-implementer"))
    assert {c["suite_key"] for c in impl.evaluation_cases} >= {"eval.small-bugfix", "eval.feature-fe-api"}
    assert all(c["case_ids"] for c in impl.evaluation_cases)
    analyst = db.scalar(select(RoleSkillPack).where(RoleSkillPack.role_key == "product-analyst"))
    assert analyst.evaluation_cases == [] and analyst.eval_status == "NOT_EVALUATED"  # honest: no suite yet


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
    db.add(ReviewRecord(target_type="worker_run", target_id="r", project_id=p.id, domain="PERSONAL",
                        reviewer_kind="SECURITY", verdict="NOT-PROVEN", status="WAITING_QUOTA", mode="LIVE"))
    db.flush()
    rows = {r["role_key"]: r for g in views.roles_view(db, domain="PERSONAL")["groups"] for r in g["roles"]}
    assert rows["planning-challenger"]["availability"] == "ACTIVE"
    assert rows["security-reviewer"]["availability"] == "ACTIVE"
    assert rows["primary-planner"]["availability"] == "AVAILABLE_INACTIVE"
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
