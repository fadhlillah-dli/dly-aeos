"""Wave-7 cluster D (workforce controls): the owner can enable the implementer agent, execute an approved plan,
register a trusted suite, adopt a role result and set the concurrency mode from the Control Center or the CLI
(W6-137/181/164/200/119/123); the owner's resource budget caps the scheduler (W6-145, §51 P2); a RUNNING topology-C
EvalRun gets two workers only through an owner experiment grant bound to it (W6-122/W6-131, §19/§44) and a slot can
hold an ordered task set aggregated as one sample (W6-131)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from aeos.core.errors import Blocked, ValidationFailed
from aeos.core.ids import new_id
from aeos.core.timeutil import utcnow
from aeos.models.audit import AuditEvent
from aeos.models.decisions import Decision
from aeos.models.research import EvalRun
from aeos.models.workforce import AgentDefinition, TaskExecution, WorkerRun
from aeos.workforce import agents, experiments, scheduler
from tests.conftest import ADMIN_PW, create_admin
from tests.unit.test_workforce_support import (
    FakeModules,
    make_plan,
    make_project,
    make_route,
    process_sandbox_exception,
    seed_and_enable_implementer,
)

runner = CliRunner()


@pytest.fixture(name="fakes")
def _fakes_fixture(monkeypatch: pytest.MonkeyPatch) -> FakeModules:
    return FakeModules().install(monkeypatch)


def _ready_env(db, fakes, tasks):
    project = make_project(db)
    plan, pv, specs = make_plan(db, project, tasks)
    fakes.approved[plan.id] = pv.id
    make_route(db)
    fakes.route_key = "worker.test-free"
    seed_and_enable_implementer(db)
    process_sandbox_exception(db)
    scheduler.ensure_executions(db, pv.id, actor="test", requested_by="owner")
    scheduler.refresh_readiness(db, pv.id)
    return plan, pv, specs


def _two_worker_policy(db):
    d = Decision(kind="POLICY", actor_user_id="owner", target_type="workforce", target_id="concurrency",
                 details={"max_active_workers": 2})
    db.add(d)
    db.flush()
    scheduler._set_setting(db, scheduler.KEY_CONCURRENCY, {"max_active": 2, "decision_id": d.id})
    return d


def _budget(db, n):
    from aeos.doctor.checks.host import set_resource_budget

    set_resource_budget(db, actor="local-operator", reason="measured on this machine", max_parallel_workers=n)


GRANT = {"days": 7, "domain": "PERSONAL"}  # the owner chooses the expiry and the domain (never defaulted)
THREE = [{"task_key": "a", "allowed_paths": ["a/**"]}, {"task_key": "b", "allowed_paths": ["b/**"]},
         {"task_key": "c", "allowed_paths": ["c/**"]}]


# ----------------------------------------------------------------------------- W6-145 resource budget
def test_owner_budget_caps_an_approved_two_worker_policy(db, fakes):
    """§51 P2: a recorded max_parallel_workers of 1 stops an approved 2-worker policy (the cap is the minimum)."""
    _plan, _pv, specs = _ready_env(db, fakes, THREE[:2])
    d = _two_worker_policy(db)
    assert scheduler.concurrency_cap(db) == (2, d.id)
    assert "not set" in scheduler.concurrency_detail(db)["budget_note"]  # UNKNOWN, never guessed
    _budget(db, 1)
    detail = scheduler.concurrency_detail(db)
    assert detail["cap"] == 1 and detail["budget_limited"] and detail["budget_max_parallel_workers"] == 1
    assert "max_parallel_workers=1" in detail["reason"]
    leased = scheduler.admit_ready_tasks(db)
    assert len(leased) == 1
    waiting = next(scheduler.get_te(db, s.id) for s in specs if scheduler.get_te(db, s.id).state == "READY")
    assert waiting.reason_code == "CONCURRENCY_CAP" and "max_parallel_workers=1" in waiting.reason
    _budget(db, 2)
    assert scheduler.concurrency_cap(db) == (2, d.id)
    assert len(scheduler.admit_ready_tasks(db)) == 1  # the second one now


def test_summary_states_the_cap_source_and_budget(personal_client, db, fakes):
    _two_worker_policy(db)
    _budget(db, 1)
    db.commit()
    s = personal_client.get("/api/workforce/summary").json()["scheduler"]
    assert s["concurrency_cap"] == 1 and s["budget_max_parallel_workers"] == 1
    assert "max_parallel_workers=1" in s["cap_reason"]


# ----------------------------------------------------------------------------- W6-122 / W6-131 experiment grant
def _c_run(db, task_ids, *, status="RUNNING", topology="C"):
    run = EvalRun(suite_key="eval.small-bugfix", configuration={"model_id": "m"}, configuration_hash="sha256:c",
                  conditions_hash="sha256:cond", topology=topology, status=status, mode="LIVE",
                  started_at=utcnow() - timedelta(minutes=5), role_key="feature-implementer",
                  bindings=[{"slot_id": f"s{i}#1", "case_id": f"s{i}", "repetition": 1, "task_id": t}
                            for i, t in enumerate(task_ids)])
    db.add(run)
    db.flush()
    return run


def test_experiment_grant_gives_two_workers_only_to_tasks_bound_to_the_running_c_run(db, fakes):
    _plan, _pv, specs = _ready_env(db, fakes, THREE)
    a, b, c = specs
    run = _c_run(db, [a.id, b.id])
    # no grant yet: the pilot cap holds even for bound tasks (the label C alone grants nothing)
    assert scheduler.concurrency_cap(db, a) == (1, None)
    with pytest.raises(ValidationFailed):
        experiments.grant_experiment(db, eval_run_id=run.id, actor="user:o", actor_user_id="owner", rationale="x",
                                     **GRANT)
    out = experiments.grant_experiment(db, eval_run_id=run.id, actor="user:o", actor_user_id="owner",
                                       rationale="bounded parallel experiment for suite small-bugfix", **GRANT)
    d = db.get(Decision, out["decision_id"])
    assert d.kind == "EXCEPTION" and d.target_type == "workforce" and d.target_id == f"concurrency-experiment:{run.id}"
    assert d.target_hash == experiments.experiment_binding_hash(run) and d.expires_at is not None
    assert scheduler.concurrency_cap(db, a) == (2, d.id)
    assert scheduler.concurrency_cap(db, c) == (1, None)  # not bound to the experiment
    assert scheduler.concurrency_cap(db) == (1, None)  # the global mode is unchanged
    leased = scheduler.admit_ready_tasks(db)
    assert len(leased) == 2
    rows = [db.get(WorkerRun, rid) for rid in leased]
    assert {r.task_id for r in rows} == {a.id, b.id}
    for r in rows:
        assert r.caps["concurrency_cap"] == 2 and r.caps["concurrency"]["source"] == "EXPERIMENT_GRANT"
        assert r.caps["concurrency"]["eval_run_id"] == run.id
    te_c = scheduler.get_te(db, c.id)
    assert te_c.state == "READY" and te_c.reason_code == "CONCURRENCY_CAP"
    # the run stops RUNNING -> the grant no longer applies (pilot cap again)
    run.status = "COMPLETED"
    db.flush()
    assert scheduler.concurrency_cap(db, a) == (1, None)


def test_experiment_never_adds_a_worker_next_to_unrelated_work(db, fakes):
    _plan, _pv, specs = _ready_env(db, fakes, THREE)
    a, b, c = specs
    run = _c_run(db, [a.id, b.id])
    experiments.grant_experiment(db, eval_run_id=run.id, actor="user:o", actor_user_id="owner",
                                 rationale="bounded parallel experiment for suite small-bugfix", **GRANT)
    scheduler.acquire_lease(db, c.id, actor="t")  # unrelated work is active first
    detail = scheduler.concurrency_detail(db, a)
    assert detail["cap"] == 1 and "not part of eval run" in detail["experiment"]["reason"]
    assert not scheduler.admission_check(db, a.id).admitted


@pytest.mark.parametrize("status, topology, code", [("PLANNED", "C", "EXPERIMENT_NOT_GRANTABLE"),
                                                    ("RUNNING", "A", "EXPERIMENT_NOT_GRANTABLE")])
def test_experiment_grant_needs_a_running_topology_c_run(db, fakes, status, topology, code):
    run = _c_run(db, [], status=status, topology=topology)
    with pytest.raises(Blocked) as exc:
        experiments.grant_experiment(db, eval_run_id=run.id, actor="user:o", actor_user_id="owner",
                                     rationale="bounded parallel experiment for suite small-bugfix", **GRANT)
    assert exc.value.code == code


def test_experiment_grant_respects_the_owner_budget(db, fakes):
    _plan, _pv, specs = _ready_env(db, fakes, THREE[:2])
    run = _c_run(db, [s.id for s in specs])
    _budget(db, 1)
    with pytest.raises(Blocked) as exc:
        experiments.grant_experiment(db, eval_run_id=run.id, actor="user:o", actor_user_id="owner",
                                     rationale="bounded parallel experiment for suite small-bugfix", **GRANT)
    assert exc.value.code == "RESOURCE_BUDGET_LIMIT"


def test_experiment_grant_needs_an_owner_expiry_and_the_runs_domain(db, fakes):
    """Like every EXCEPTION the grant expires on a date the owner chose; it is recorded in the domain of the run's
    tasks and is not found from the other domain (domain-first)."""
    from aeos.core.errors import NotFound

    _plan, _pv, specs = _ready_env(db, fakes, THREE[:2])
    run = _c_run(db, [s.id for s in specs])
    why = "bounded parallel experiment for suite small-bugfix"
    with pytest.raises(ValidationFailed) as exc:
        experiments.grant_experiment(db, eval_run_id=run.id, actor="user:o", actor_user_id="owner", rationale=why,
                                     days=None, domain="PERSONAL")
    assert exc.value.code == "EXPIRY_REQUIRED"
    with pytest.raises(NotFound):
        experiments.grant_experiment(db, eval_run_id=run.id, actor="user:o", actor_user_id="owner", rationale=why,
                                     days=7, domain="OFFICE")
    out = experiments.grant_experiment(db, eval_run_id=run.id, actor="user:o", actor_user_id="owner", rationale=why,
                                       days=7, domain="PERSONAL")
    d = db.get(Decision, out["decision_id"])
    assert d.domain == "PERSONAL" and d.expires_at is not None
    d.expires_at = utcnow() - timedelta(seconds=1)  # the grant lapses by itself at its expiry
    db.flush()
    assert scheduler.concurrency_cap(db, specs[0]) == (1, None)


def test_restarted_run_needs_a_new_grant(db, fakes):
    _plan, _pv, specs = _ready_env(db, fakes, THREE[:2])
    run = _c_run(db, [s.id for s in specs])
    experiments.grant_experiment(db, eval_run_id=run.id, actor="user:o", actor_user_id="owner",
                                 rationale="bounded parallel experiment for suite small-bugfix", **GRANT)
    assert scheduler.concurrency_cap(db, specs[0])[0] == 2
    run.started_at = utcnow()  # another start = another experiment
    db.flush()
    assert scheduler.concurrency_cap(db, specs[0]) == (1, None)


def test_concurrency_api_lists_experiments_and_grants(personal_client, db, fakes):
    _plan, _pv, specs = _ready_env(db, fakes, THREE[:2])
    run = _c_run(db, [s.id for s in specs])
    run.submitted_domain = "PERSONAL"
    db.commit()
    body = personal_client.get("/api/workforce/concurrency").json()
    assert body["concurrency"]["cap"] == 1 and body["items"][0]["eval_run_id"] == run.id
    assert body["items"][0]["grantable"] is True and body["items"][0]["grant_decision_id"] is None
    # an EXCEPTION without an owner-chosen expiry is refused (no default expiry)
    r = personal_client.post("/api/workforce/concurrency/experiment",
                             json={"eval_run_id": run.id, "rationale": "bounded parallel experiment now"})
    assert r.status_code == 422
    r = personal_client.post("/api/workforce/concurrency/experiment",
                             json={"eval_run_id": run.id, "rationale": "bounded parallel experiment now", "days": 3})
    assert r.status_code == 201, r.text
    assert r.json()["domain"] == "PERSONAL" and r.json()["expires_at"]
    body = personal_client.get("/api/workforce/concurrency").json()
    assert body["items"][0]["grant_decision_id"] == r.json()["decision_id"] and body["items"][0]["grant_active"]
    assert body["items"][0]["parallel_execution"]["status"] == "NOT_PROVEN"


def test_two_worker_scope_is_limited_to_the_role_the_suite_proved(db, fakes):
    from tests.unit.test_workforce_gaps import RECORD, _topology_runs

    cand, base = _topology_runs(db)
    with pytest.raises(ValidationFailed) as exc:
        scheduler.set_concurrency(db, max_active=2, actor="user:o", actor_user_id="owner", rationale="uji dua worker",
                                  eval_run_id=cand.id, baseline_eval_run_id=base.id, decision_record=RECORD,
                                  scope={"role_keys": ["feature-implementer", "backend-api-engineer"]})
    assert exc.value.code == "CONCURRENCY_SCOPE_OUTSIDE_EVIDENCE"
    cand2, base2 = _topology_runs(db, role_key=None)
    with pytest.raises(ValidationFailed) as exc:
        scheduler.set_concurrency(db, max_active=2, actor="user:o", actor_user_id="owner", rationale="uji dua worker",
                                  eval_run_id=cand2.id, baseline_eval_run_id=base2.id, decision_record=RECORD,
                                  scope={"risk_classes": ["R1"]})
    assert exc.value.code == "CONCURRENCY_TASK_CLASS_UNKNOWN"


# ----------------------------------------------------------------------------- agent enable / disable
def _ready_prereqs(monkeypatch, *, sandbox_kind="process", opencode="INSTALLED", image=True):
    from aeos.worker_runtime import facade as runtime
    from aeos.worker_runtime import opencode as oc
    from aeos.worker_runtime import sandbox
    from aeos.workforce import integrations as ext

    monkeypatch.setattr(sandbox, "sandbox_readiness", lambda session, *, domain: {
        "status": "READY", "kind": sandbox_kind, "reasons": [], "remedies": []})
    monkeypatch.setattr(oc, "opencode_status", lambda configured=None, refresh=False: {
        "status": opencode, "evidence": "test stand-in", "version": "1.19.3"})
    monkeypatch.setattr(runtime, "sandbox_capabilities", lambda: {"levels": {"container": {"image": {
        "tag": "dly-aeos-worker:1.19.3", "present": image, "evidence": "test stand-in"}}}})
    monkeypatch.setattr(ext, "capability_ready", lambda session, cap, **kw: (True, []))


def _seeded(db):
    from aeos.workforce import seed

    seed.run(db)
    make_route(db)
    db.commit()


def test_enable_lists_every_unmet_prerequisite_and_audits_the_refusal(personal_client, db):
    from aeos.workforce import seed

    seed.run(db)
    db.commit()
    pre = personal_client.get("/api/workforce/agents/opencode-implementer/prerequisites").json()
    assert pre["ready"] is False and pre["excluded_check"] == "worker.agent_definition"
    assert any("OpenCode" in r for r in pre["reasons"]) and any("EXCEPTION" in r for r in pre["reasons"])
    r = personal_client.post("/api/workforce/agents/opencode-implementer/enable", json={"reason": "pilot"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "AGENT_PREREQUISITES"
    assert r.json()["error"]["details"]["reasons"] == pre["reasons"]
    db.expire_all()
    denied = db.scalar(select(AuditEvent).where(AuditEvent.action == "workforce.agent.enable"))
    assert denied is not None and denied.result == "DENIED"


def test_enable_rechecks_the_doctor_agent_check_and_disable_reverts(personal_client, db, monkeypatch):
    """W6-164: enabling refreshes worker.agent_definition so CAN_RUN_WORKER does not keep the STALE result."""
    from aeos.doctor.runner import latest_results
    from aeos.routing import facade as routing
    from aeos.routing.facade import RouteDecision

    _seeded(db)
    _ready_prereqs(monkeypatch)
    monkeypatch.setattr(routing, "eligible_routes", lambda session, **kw: RouteDecision(route=None,
                                                                                         eligible=["worker.test-free"]))
    r = personal_client.post("/api/workforce/agents/opencode-implementer/enable", json={"reason": "pilot"})
    assert r.status_code == 200, r.text
    assert r.json()["doctor"] == {"status": "RECHECKED", "results": {"worker.agent_definition": "CONFIGURED"}}
    db.expire_all()
    assert latest_results(db, ["worker.agent_definition"])["worker.agent_definition"].status == "CONFIGURED"
    r = personal_client.post("/api/workforce/agents/opencode-implementer/disable", json={"reason": "stop"})
    assert r.status_code == 200 and r.json()["enabled"] is False
    db.expire_all()
    assert latest_results(db, ["worker.agent_definition"])["worker.agent_definition"].status == "MISSING"
    agent = db.scalar(select(AgentDefinition).where(AgentDefinition.agent_key == "opencode-implementer"))
    assert agent.enabled is False and "stop" in agent.enabled_reason


def test_container_sandbox_checks_the_worker_image_not_the_host_binary(db, monkeypatch):
    from aeos.routing import facade as routing
    from aeos.routing.facade import RouteDecision

    _seeded(db)
    monkeypatch.setattr(routing, "eligible_routes", lambda session, **kw: RouteDecision(route=None, eligible=["x"]))
    agent = agents.get_agent(db, "opencode-implementer")
    _ready_prereqs(monkeypatch, sandbox_kind="container", opencode="MISSING", image=True)
    assert agents.prerequisites(db, agent, domain="PERSONAL")["ready"] is True
    _ready_prereqs(monkeypatch, sandbox_kind="container", opencode="INSTALLED", image=False)
    pre = agents.prerequisites(db, agent, domain="PERSONAL")
    assert not pre["ready"] and "worker image dly-aeos-worker:1.19.3 missing" in pre["reasons"][0]
    _ready_prereqs(monkeypatch, sandbox_kind="process", opencode="MISSING")
    assert "OpenCode MISSING" in agents.prerequisites(db, agent, domain="PERSONAL")["reasons"][0]


def test_viewer_cannot_toggle_agents_or_grant_experiments(client, db):
    from aeos.core.enums import Domain
    from tests.conftest import VIEWER_PW, create_viewer

    _seeded(db)
    create_admin(db)
    create_viewer(db)
    client.login("viewer", VIEWER_PW)
    client.select_domain(Domain.PERSONAL)
    for url, body in (("/api/workforce/agents/opencode-implementer/enable", {"reason": "x"}),
                      ("/api/workforce/agents/opencode-implementer/disable", {"reason": "x"}),
                      ("/api/workforce/concurrency/experiment", {"eval_run_id": "x", "rationale": "0123456789"})):
        assert client.post(url, json=body).status_code == 403, url


# ----------------------------------------------------------------------------- CLI owner actions
def _cli():
    from aeos.cli import app

    return app


def test_cli_agent_enable_needs_the_owners_password_and_reports_prerequisites(db, monkeypatch):
    _seeded(db)
    create_admin(db)
    r = runner.invoke(_cli(), ["work", "agent-enable", "opencode-implementer", "--as", "owner", "--domain", "PERSONAL",
                               "--reason", "pilot"], input="wrong\n")
    assert r.exit_code != 0 and "password check failed" in r.output
    r = runner.invoke(_cli(), ["work", "agent-enable", "opencode-implementer", "--as", "owner", "--domain", "PERSONAL",
                               "--reason", "pilot"], input=ADMIN_PW + "\n")
    assert r.exit_code == 1 and "AGENT_PREREQUISITES" in r.output and "OpenCode" in r.output
    assert r.output.isascii()
    from aeos.routing import facade as routing
    from aeos.routing.facade import RouteDecision

    _ready_prereqs(monkeypatch)
    monkeypatch.setattr(routing, "eligible_routes", lambda session, **kw: RouteDecision(route=None, eligible=["x"]))
    r = runner.invoke(_cli(), ["work", "agent-enable", "opencode-implementer", "--as", "owner", "--domain", "PERSONAL",
                               "--reason", "pilot"], input=ADMIN_PW + "\n")
    assert r.exit_code == 0, r.output
    assert "enabled" in r.output and "RECHECKED" in r.output
    db.expire_all()
    assert agents.get_agent(db, "opencode-implementer").enabled is True
    r = runner.invoke(_cli(), ["work", "agent-disable", "opencode-implementer", "--reason", "stop"])
    assert r.exit_code == 0 and "disabled" in r.output
    db.expire_all()
    assert agents.get_agent(db, "opencode-implementer").enabled is False
    r = runner.invoke(_cli(), ["work", "agent-prereqs", "opencode-implementer", "--domain", "PERSONAL"])
    assert r.exit_code == 0 and "READY to enable" in r.output


def test_cli_execute_runs_an_approved_plan_in_the_chosen_domain_only(db, fakes):
    create_admin(db)
    project = make_project(db)
    plan, pv, _specs = make_plan(db, project, [{"task_key": "a"}])
    fakes.approved[plan.id] = pv.id
    db.commit()
    wrong = runner.invoke(_cli(), ["work", "execute", plan.id, "--as", "owner", "--domain", "OFFICE"],
                          input=ADMIN_PW + "\n")
    assert wrong.exit_code == 1 and "NOT_FOUND" in wrong.output.upper()
    r = runner.invoke(_cli(), ["work", "execute", plan.id, "--as", "owner", "--domain", "PERSONAL"],
                      input=ADMIN_PW + "\n")
    assert r.exit_code == 0, r.output
    assert "1 task(s)" in r.output and r.output.isascii()
    db.expire_all()
    te = db.scalar(select(TaskExecution).where(TaskExecution.plan_id == plan.id))
    assert te is not None and te.requested_by is not None


def test_cli_experiment_grant_records_the_owner_decision(db, fakes):
    owner = create_admin(db)
    _plan, _pv, specs = _ready_env(db, fakes, THREE[:2])
    run = _c_run(db, [s.id for s in specs])
    db.commit()
    base = ["work", "experiment-grant", run.id, "--as", "owner", "--rationale", "bounded parallel experiment now"]
    # an EXCEPTION always expires and the domain is chosen by the owner: both are required, never defaulted
    assert runner.invoke(_cli(), [*base, "--domain", "PERSONAL"], input=ADMIN_PW + "\n").exit_code == 2
    assert runner.invoke(_cli(), [*base, "--days", "5"], input=ADMIN_PW + "\n").exit_code == 2
    wrong = runner.invoke(_cli(), [*base, "--domain", "OFFICE", "--days", "5"], input=ADMIN_PW + "\n")
    assert wrong.exit_code == 1 and "NOT_FOUND" in wrong.output.upper()
    r = runner.invoke(_cli(), [*base, "--domain", "PERSONAL", "--days", "5"], input=ADMIN_PW + "\n")
    assert r.exit_code == 0, r.output
    assert r.output.isascii() and "expires" in r.output
    db.expire_all()
    d = db.scalar(select(Decision).where(Decision.target_id == f"concurrency-experiment:{run.id}"))
    assert d is not None and d.actor_user_id == owner.id and d.domain == "PERSONAL" and d.expires_at is not None


def test_cli_concurrency_back_to_one_and_role_adoption_refusal(db, fakes):
    create_admin(db)
    db.commit()
    r = runner.invoke(_cli(), ["work", "concurrency", "1", "--as", "owner", "--rationale", "back to the pilot"],
                      input=ADMIN_PW + "\n")
    assert r.exit_code == 0 and "effective cap 1" in r.output
    r = runner.invoke(_cli(), ["work", "adopt-role", "feature-implementer", "--eval-run", new_id(), "--as", "owner",
                               "--rationale", "adopt it", "--option", "keep", "--option", "adopt", "--goal", "g",
                               "--tradeoff", "t", "--invalidate-if", "i"], input=ADMIN_PW + "\n")
    assert r.exit_code == 1 and ("ROLE_RESULT_NOT_ADOPTABLE" in r.output or "ROLE_UNKNOWN" in r.output)


def test_cli_registers_a_projects_trusted_suite_as_the_owners_decision(personal_client, db, tmp_path):
    """W6-119: the trusted suite of a fixture project is registered from the terminal too (ADR-0060), as a human
    decision proven by the SUPER_ADMIN password, in the project's own domain only."""
    import shutil

    if shutil.which("git") is None:
        pytest.skip("git is required")
    from aeos.models.identity import User
    from tests.unit.test_w2_trusted_suite import _project, _repo

    repo, head = _repo(tmp_path)
    pid = _project(personal_client, repo)
    base = ["verify", "suite-register", pid, "--as", "owner", "--rationale", "fixture grader reviewed", "--commit",
            "main"]
    r = runner.invoke(_cli(), [*base, "--domain", "PERSONAL"], input="wrong\n")
    assert r.exit_code != 0 and "password check failed" in r.output
    r = runner.invoke(_cli(), [*base, "--domain", "OFFICE"], input=ADMIN_PW + "\n")
    assert r.exit_code != 0 and "NOT_FOUND" in r.output.upper()
    assert runner.invoke(_cli(), ["verify", "suite-status", pid, "--domain", "PERSONAL"]).output.find("BLOCKED") >= 0
    r = runner.invoke(_cli(), [*base, "--domain", "PERSONAL"], input=ADMIN_PW + "\n")
    assert r.exit_code == 0, r.output
    assert head in r.output and r.output.isascii()
    db.expire_all()
    owner = db.scalar(select(User).where(User.username == "owner"))
    d = db.scalar(select(Decision).where(Decision.target_type == "trusted_suite", Decision.target_id == pid))
    assert d is not None and d.kind == "POLICY" and d.actor_user_id == owner.id
    st = runner.invoke(_cli(), ["verify", "suite-status", pid, "--domain", "PERSONAL"])
    assert st.exit_code == 0 and "REGISTERED" in st.output and d.id in st.output


# ----------------------------------------------------------------------------- W6-131 ordered task set per slot
@pytest.fixture
def ev(db, monkeypatch):
    from aeos.evaluation import service
    from aeos.routing import seed as route_seed
    from tests.unit.test_routing_fakes import install_fakes

    install_fakes(monkeypatch, cost=False)
    route_seed.run(db)
    service.seed_suites(db)
    db.commit()
    return db


def _task_records(db, spec, *, state="DONE", accepted="ACCEPTED", start, minutes=10, tokens=150, route=None):
    from aeos.models.broker_ext import BrokerRequest
    from aeos.models.cost import UsageLedgerEntry
    from aeos.models.evidence import AcceptanceRecord
    from tests.unit.test_evaluation_execution import ROUTE

    route = route or ROUTE
    db.add(TaskExecution(task_id=spec.id, project_id=spec.project_id, domain=spec.domain, state=state,
                         attempts_used=1, created_at=start - timedelta(minutes=1)))
    run = WorkerRun(task_id=spec.id, project_id=spec.project_id, domain=spec.domain, attempt=1, model_route_key=route,
                    engine="opencode", engine_version="1.19.3", execution_status="DONE" if state == "DONE" else "FAILED",
                    started_at=start, finished_at=start + timedelta(minutes=minutes), verifier_status="PASS")
    db.add(run)
    db.flush()
    db.add(UsageLedgerEntry(run_id=run.id, task_id=spec.id, provider="opencode-zen", category="WORKER_API",
                            source="broker", cost_usd=0.01, input_tokens=100, output_tokens=50, route_key=route))
    db.add(BrokerRequest(grant_id="g", run_id=run.id, project_id=spec.project_id, domain=spec.domain, route_key=route,
                         outcome="OK", prompt_tokens=tokens - 50, completion_tokens=50))
    if accepted:
        db.add(AcceptanceRecord(task_id=spec.id, run_id=run.id, project_id=spec.project_id, artifact_hash="sha256:a",
                                acceptance_status=accepted, reasons=[]))
    db.flush()
    return run


def test_a_slot_binds_an_ordered_task_set_and_is_collected_as_one_sample(ev):
    from aeos.evaluation import executor, topology
    from tests.unit.test_evaluation_execution import plan

    db = ev
    run = plan(db, topology="C")
    executor.start_run(db, run.id, actor_user_id="owner")
    project = make_project(db)
    _, _, specs = make_plan(db, project, [{"task_key": f"T{i}"} for i in range(6)])
    slots = executor.slots(db, run)
    assert len(slots) == 5
    first = slots[0]["slot_id"]
    executor.bind_task(db, run.id, slot_id=first, task_id=specs[0].id, actor_user_id="owner")
    with pytest.raises(ValidationFailed):  # append needs a bound slot
        topology.bind_additional_task(db, run.id, slot_id=slots[1]["slot_id"], task_id=specs[1].id,
                                      actor_user_id="owner")
    topology.bind_additional_task(db, run.id, slot_id=first, task_id=specs[5].id, actor_user_id="owner")
    from aeos.core.errors import Conflict

    with pytest.raises(Conflict):  # a task fills one place only
        topology.bind_additional_task(db, run.id, slot_id=first, task_id=specs[5].id, actor_user_id="owner")
    with pytest.raises(Conflict):
        executor.bind_task(db, run.id, slot_id=slots[1]["slot_id"], task_id=specs[5].id, actor_user_id="owner")
    for s, spec in zip(slots[1:], specs[1:5], strict=True):
        executor.bind_task(db, run.id, slot_id=s["slot_id"], task_id=spec.id, actor_user_id="owner")
    began = utcnow() + timedelta(seconds=1)
    _task_records(db, specs[0], start=began, minutes=10)
    out = executor.collect(db, run.id)
    pending = {p["slot_id"]: p["reason"] for p in out["pending"]}
    assert "task 2/2" in pending[first]  # the second task of the set has not run yet: never a partial sample
    _task_records(db, specs[5], start=began + timedelta(minutes=4), minutes=10, tokens=250)  # overlaps 6 min
    for spec in specs[1:5]:
        _task_records(db, spec, start=began, minutes=5)
    out = executor.collect(db, run.id)
    assert out["recorded"] is True, out
    db.refresh(run)
    sample = next(s for s in run.samples if s["sample_id"] == first)
    assert sample["task_ids"] == [specs[0].id, specs[5].id] and sample["task_count"] == 2
    assert sample["status"] == "ACCEPTED"
    assert sample["latency_s"] == pytest.approx(14 * 60, abs=1)  # wall clock, not the sum (20 min)
    assert sample["parallel_overlap_s"] == pytest.approx(6 * 60, abs=1)
    assert sample["worker_tokens"] == 150 + 250 and sample["repair_count"] == 0
    assert sample["worker_cost_usd"] == pytest.approx(0.02)
    assert "coordination_tokens" in sample["unknown_fields"]


def test_slot_outcome_is_not_accepted_when_one_task_of_the_set_was_rejected(ev):
    from aeos.evaluation import executor, topology
    from tests.unit.test_evaluation_execution import plan

    db = ev
    run = plan(db, topology="D")
    executor.start_run(db, run.id, actor_user_id="owner")
    project = make_project(db)
    _, _, specs = make_plan(db, project, [{"task_key": "A"}, {"task_key": "B"}])
    slot = executor.slots(db, run)[0]["slot_id"]
    executor.bind_task(db, run.id, slot_id=slot, task_id=specs[0].id, actor_user_id="owner")
    topology.bind_additional_task(db, run.id, slot_id=slot, task_id=specs[1].id, actor_user_id="owner")
    began = utcnow() + timedelta(seconds=1)
    _task_records(db, specs[0], state="FAILED", accepted="REJECTED", start=began)
    _task_records(db, specs[1], start=began + timedelta(minutes=20))
    binding = next(b for b in db.get(EvalRun, run.id).bindings if b["slot_id"] == slot)
    state, sample, _why = topology.derive_slot_sample(db, run, binding)
    assert state == "READY" and sample["status"] == "REJECTED"
    assert [t["status"] for t in sample["tasks"]] == ["REJECTED", "ACCEPTED"]


def test_experiment_counts_tasks_of_an_ordered_set(db):
    run = EvalRun(suite_key="s", configuration={}, configuration_hash="h", bindings=[
        {"slot_id": "x#1", "task_id": "t1", "task_ids": ["t1", "t2"]}, {"slot_id": "y#1", "task_id": "t3"}])
    assert experiments.bound_task_ids(run) == ["t1", "t2", "t3"]


def test_bind_api_appends_to_a_slot(personal_client, ev):
    from aeos.evaluation import executor
    from tests.unit.test_evaluation_execution import plan

    db = ev
    run = plan(db, topology="C")
    executor.start_run(db, run.id, actor_user_id="owner")
    project = make_project(db)
    _, _, specs = make_plan(db, project, [{"task_key": "A"}, {"task_key": "B"}])
    db.commit()
    slot = executor.slots(db, run)[0]["slot_id"]
    r = personal_client.post(f"/api/eval/runs/{run.id}/bind", json={"slot_id": slot, "task_id": specs[0].id})
    assert r.status_code == 200, r.text
    r = personal_client.post(f"/api/eval/runs/{run.id}/bind",
                             json={"slot_id": slot, "task_id": specs[1].id, "append": True})
    assert r.status_code == 200, r.text
    db.expire_all()
    b = next(x for x in db.get(EvalRun, run.id).bindings if x["slot_id"] == slot)
    assert b["task_ids"] == [specs[0].id, specs[1].id]


def test_sum_or_unknown_never_turns_unknown_into_zero():
    from aeos.evaluation.topology import _sum_or_unknown

    assert _sum_or_unknown([{"x": 1}, {"x": None}], "x") is None
    assert _sum_or_unknown([{"x": 1}, {"x": 2}], "x") == 3


def test_slot_aggregation_keeps_an_unknown_regression_count_unknown_and_sums_new_measures(db):
    """A per-task measure that is UNKNOWN (e.g. critical regressions without a verifier baseline, W6-121) stays
    UNKNOWN for the slot - never 0 - and a measure the collector adds later is summed, not dropped."""
    from aeos.evaluation.topology import aggregate

    binding = {"slot_id": "x#1", "case_id": "x", "repetition": 1}
    parts = [{"status": "ACCEPTED", "first_pass": True, "queue_wait_s": 3.0, "critical_regressions": 0,
              "non_regression_unargued": 1, "provider_unavailable": False, "unknown_fields": ["coordination_tokens"]},
             {"status": "ACCEPTED", "first_pass": True, "queue_wait_s": 5.0, "critical_regressions": None,
              "non_regression_unargued": 2, "provider_unavailable": True, "unknown_fields": []}]
    s = aggregate(db, binding, ["t1", "t2"], parts)
    assert s["critical_regressions"] is None and "critical_regressions" in s["unknown_fields"]
    assert s["non_regression_unargued"] == 3 and s["provider_unavailable"] is True
    assert s["queue_wait_s"] == 5.0 and s["status"] == "ACCEPTED" and "coordination_tokens" in s["unknown_fields"]
    assert s["latency_s"] is None  # no recorded attempt windows: UNKNOWN, not 0
