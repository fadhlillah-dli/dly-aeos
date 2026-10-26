"""Workforce Console API (§35): domain scoping, VIEWER read-only, operator controls, no fake activity."""

from __future__ import annotations

import pytest

from aeos.core.enums import Domain
from aeos.models.workforce import WorkerRun
from aeos.workforce import demo, scheduler, seed
from tests.conftest import VIEWER_PW, create_viewer
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


def test_summary_requires_explicit_domain(admin_client):
    r = admin_client.get("/api/workforce/summary")
    assert r.status_code == 409 and r.json()["error"]["code"] == "DOMAIN_NOT_SELECTED"


def test_summary_shape_and_honest_unknowns(personal_client):
    r = personal_client.get("/api/workforce/summary")
    assert r.status_code == 200
    d = r.json()
    assert d["domain"] == "PERSONAL" and d["active_runs"] == 0
    assert d["scheduler"]["concurrency_cap"] == 1 and d["scheduler"]["paused"] is False
    assert d["sandbox"]["status"] == "BLOCKED"  # no exception decision, no proven container
    assert d["engine"]["functional"] == "NOT_PROVEN"
    assert d["cost"]["cost_status"] == "NONE" and d["cost"]["cost_known_usd"] is None


def test_roles_grouped_and_inactive_not_working(personal_client, db):
    seed.run(db)
    db.commit()
    r = personal_client.get("/api/workforce/roles")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 8 and "permission" in body["note"]
    roles = [x for g in body["items"] for x in g["roles"]]
    assert len(roles) == 40
    assert all(x["active_runs"] == 0 for x in roles)
    assert {x["availability"] for x in roles} <= {"AVAILABLE_INACTIVE", "SERVICE", "HUMAN"}
    agents = personal_client.get("/api/workforce/agents").json()["items"]
    assert {a["agent_key"]: a["availability"] for a in agents}["opencode-implementer"] == "DISABLED"


def test_enable_agent_blocked_until_prerequisites(personal_client, db, opencode_absent):
    seed.run(db)
    db.commit()
    r = personal_client.post("/api/workforce/agents/opencode-implementer/enable", json={"reason": "go"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "AGENT_PREREQUISITES"
    reasons = " ".join(r.json()["error"]["details"]["reasons"])
    assert "OpenCode" in reasons and "EXCEPTION" in reasons


def test_runs_list_demo_labelled_and_office_isolated(personal_client, db):
    demo.insert(db, domain="PERSONAL")
    office = WorkerRun(project_id="office-proj", domain="OFFICE", execution_status="RUNNING", mode="LIVE")
    db.add(office)
    db.commit()
    r = personal_client.get("/api/runs")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "DEMO" and {i["mode"] for i in body["items"]} == {"DEMO"}
    assert office.id not in {i["run_id"] for i in body["items"]}
    assert personal_client.get(f"/api/runs/{office.id}").status_code == 404  # §50 Personal vs Office scope


def test_run_detail_has_separate_status_fields(personal_client, db, fakes):
    p = make_project(db)
    plan, pv, specs = make_plan(db, p, [{"task_key": "a", "allowed_paths": ["src/**"]}])
    fakes.approved[plan.id] = pv.id
    make_route(db)
    fakes.route_key = "worker.test-free"
    seed_and_enable_implementer(db)
    process_sandbox_exception(db)
    scheduler.ensure_executions(db, pv.id, actor="t")
    scheduler.refresh_readiness(db, pv.id)
    run = scheduler.acquire_lease(db, specs[0].id, actor="t")
    db.commit()
    r = personal_client.get(f"/api/runs/{run.id}")
    assert r.status_code == 200
    d = r.json()
    assert set(d["statuses"]) == {"worker_reported", "verifier", "review", "acceptance", "merged"}
    assert d["task_spec"]["allowed_paths"] == ["src/**"] and d["task_execution"]["state"] == "LEASED"
    assert d["task_spec"]["verifier_policy"] == {}  # no opt-ins: stated as an empty policy, never omitted
    assert d["lease"]["state"] == "ACTIVE" and any(a["kind"] == "run.state_changed" for a in d["actions"])
    assert d["owner_requirements"]
    adm = personal_client.get(f"/api/tasks/{specs[0].id}/admission").json()
    assert adm["state"] == "LEASED"
    # cancel through the API
    c = personal_client.post(f"/api/runs/{run.id}/cancel", json={"reason": "stop"})
    assert c.status_code == 200 and c.json()["execution_state"] == "CANCELLED"
    assert "tidak dibatalkan otomatis" in c.json()["note"]


def test_run_detail_states_the_task_verifier_policy_read_only(personal_client, db, fakes):
    p = make_project(db)
    policy = {"required": True, "suites": ["unit"], "negative_tests_required": True,
              "integration_tests": "run", "contract_breaking": "approved"}
    plan, pv, specs = make_plan(db, p, [{"task_key": "a", "allowed_paths": ["src/**"], "verifier_policy": policy}])
    fakes.approved[plan.id] = pv.id
    make_route(db)
    fakes.route_key = "worker.test-free"
    seed_and_enable_implementer(db)
    process_sandbox_exception(db)
    scheduler.ensure_executions(db, pv.id, actor="t")
    scheduler.refresh_readiness(db, pv.id)
    run = scheduler.acquire_lease(db, specs[0].id, actor="t")
    db.commit()
    d = personal_client.get(f"/api/runs/{run.id}").json()
    assert d["task_spec"]["verifier_policy"] == policy


def test_viewer_is_read_only(client, db):
    from tests.conftest import create_admin

    create_admin(db)
    create_viewer(db)
    client.login("viewer", VIEWER_PW)
    client.select_domain(Domain.PERSONAL)
    run = WorkerRun(project_id="p", domain="PERSONAL", execution_status="RUNNING")
    db.add(run)
    db.commit()
    assert client.get("/api/runs").status_code == 200
    for url, body in ((f"/api/runs/{run.id}/cancel", {}), ("/api/workforce/pause", {}), ("/api/workforce/resume", {}),
                      ("/api/tasks/x/retry", {}), ("/api/tasks/x/reassign", {"agent_key": "a"}),
                      ("/api/plans/x/execute", {}), ("/api/workforce/scheduler", {"enabled": True})):
        r = client.post(url, json=body)
        assert r.status_code == 403, url
    assert client.get("/api/broker/grants").status_code == 403  # admin view only
    db.expire_all()
    assert db.get(WorkerRun, run.id).execution_status == "RUNNING"


def test_pause_resume_and_scheduler_toggle(personal_client):
    assert personal_client.post("/api/workforce/pause", json={"reason": "maintenance"}).status_code == 200
    s = personal_client.get("/api/workforce/summary").json()["scheduler"]
    assert s["paused"] is True and s["paused_reason"] == "maintenance"
    assert personal_client.post("/api/workforce/resume", json={}).status_code == 200
    assert personal_client.post("/api/workforce/scheduler", json={"enabled": True}).status_code == 200
    assert personal_client.get("/api/workforce/summary").json()["scheduler"]["enabled"] is True


def test_csrf_required_for_writes(personal_client):
    r = personal_client.client.post("/api/workforce/pause", json={})
    assert r.status_code == 403 and r.json()["error"]["code"] == "CSRF_FAILED"


def test_execute_plan_blocked_without_approval(personal_client, db):
    p = make_project(db)
    plan, _pv, _specs = make_plan(db, p, [{"task_key": "a"}])
    db.commit()
    r = personal_client.post(f"/api/plans/{plan.id}/execute")
    assert r.status_code == 409 and r.json()["error"]["code"] == "PLAN_NOT_APPROVED"


def test_execute_plan_cross_domain_is_404(personal_client, db):
    p = make_project(db, domain="OFFICE")
    plan, _pv, _specs = make_plan(db, p, [{"task_key": "a"}])
    db.commit()
    assert personal_client.post(f"/api/plans/{plan.id}/execute").status_code == 404


def test_execute_plan_ok_with_modules(personal_client, db, fakes):
    p = make_project(db)
    plan, pv, specs = make_plan(db, p, [{"task_key": "a"}])
    fakes.approved[plan.id] = pv.id
    db.commit()
    r = personal_client.post(f"/api/plans/{plan.id}/execute")
    assert r.status_code == 200 and r.json()["ready"] == 1
    retry = personal_client.post(f"/api/tasks/{specs[0].id}/retry", json={})
    assert retry.status_code == 409  # READY: nothing to retry
    bad = personal_client.post(f"/api/tasks/{specs[0].id}/reassign", json={"agent_key": "claude-checkpoint-reviewer"})
    assert bad.status_code in (404, 422)


def test_broker_grants_view_has_no_tokens(personal_client, db):
    from aeos.broker import facade as broker

    raw, g = broker.issue_grant(db, run_id="r1", project_id="p1", domain="PERSONAL", kind="MODEL_PROXY",
                                scope={"models": {"m": "r"}}, ttl_seconds=600)
    db.commit()
    r = personal_client.get("/api/broker/grants")
    assert r.status_code == 200 and raw not in r.text and g.token_hash not in r.text
    assert r.json()["items"][0]["kind"] == "MODEL_PROXY"
    rv = personal_client.post("/api/broker/runs/r1/revoke", json={"reason": "x"})
    assert rv.status_code == 200 and rv.json()["revoked"] == 1


def test_worker_runtime_endpoints(personal_client):
    assert personal_client.get("/api/worker-runtime/sandbox").json()["status"] == "BLOCKED"
    assert personal_client.get("/api/worker-runtime/engine").status_code == 200
    assert personal_client.get("/api/worker-runtime/containment").json()["items"] == []
    assert personal_client.post("/api/worker-runtime/containment", json={"kind": "vm"}).status_code == 422


def test_summary_states_the_integrity_hold_scoped_to_the_domain(personal_client, db):
    """Wave 7b UI: the Control Center shows the host-wide hold from the summary (code + details), and another
    domain's run id and findings reach this domain's view only as a count (AGENTS.md invariant 9)."""
    from aeos.worker_runtime import integrity

    hold = integrity.raise_hold(db, source="worker run", subject_id="office-run-1", project_id="office-project",
                                domain="OFFICE",
                                findings=[{"kind": "db", "path": "decisions:office-decision-1", "change": "added"}])
    db.commit()
    sb = personal_client.get("/api/workforce/summary").json()["sandbox"]
    assert sb["status"] == "BLOCKED" and sb["code"] == "INTEGRITY_HOLD" and sb["kind"] is None
    shown = sb["integrity_hold"]
    assert shown["id"] == hold["id"] and shown["subject_id"] is None and shown["findings"] == []
    assert shown["other_domain_findings"] == 1
    assert "office-run-1" not in str(sb) and "office-decision-1" not in str(sb)
    assert hold["id"] in sb["remedies"][0] and "<PERSONAL|OFFICE>" in sb["remedies"][0]


def test_summary_without_a_hold_has_no_hold_code(personal_client):
    sb = personal_client.get("/api/workforce/summary").json()["sandbox"]
    assert sb["code"] != "INTEGRITY_HOLD" and sb["integrity_hold"] is None


def test_run_detail_model_calls_state_the_reasoning_levels(personal_client, db):
    """ADR-0041 amendment 2026-10-25: the run's model-call view shows the level the broker sent and the level the
    worker asked for (dropped, never forwarded); a row recorded before the columns existed stays None (UNKNOWN)."""
    from aeos.models.broker_ext import BrokerRequest

    run = WorkerRun(project_id="p", domain="PERSONAL", execution_status="RUNNING", mode="LIVE")
    db.add(run)
    db.flush()
    db.add(BrokerRequest(grant_id="g", run_id=run.id, project_id="p", domain="PERSONAL", route_key="worker.r",
                         outcome="OK", prompt_tokens=10, completion_tokens=5, reasoning_effort="NOT_SENT",
                         reasoning_requested="high"))
    db.add(BrokerRequest(grant_id="g", run_id=run.id, project_id="p", domain="PERSONAL", route_key="worker.r",
                         outcome="OK", prompt_tokens=10, completion_tokens=5))
    db.commit()
    calls = personal_client.get(f"/api/runs/{run.id}").json()["model_calls"]
    assert {(c["reasoning_effort"], c["reasoning_requested"]) for c in calls} == {("NOT_SENT", "high"), (None, None)}


def _ui_reconcile_body(hold_id: str) -> dict:
    """Exactly what the Control Center's 'Catat rekonsiliasi' sends (frontend/src/pages/work/IntegrityHold.tsx)."""
    from datetime import timedelta

    from aeos.core.timeutil import utcnow

    return {"kind": "RISK_ACCEPTANCE", "target_type": "worker_integrity_hold", "target_id": hold_id,
            "rationale": "git checkout -- config.py; data checked",
            "auth_scope": {"integrity_hold": hold_id, "findings_reviewed": "yes", "controller_and_data_restored": "yes",
                           "recorded_via": "Control Center"},
            "allowed_operations": [], "expires_at": (utcnow() + timedelta(days=1)).isoformat()}


def test_the_control_center_reconciliation_lifts_exactly_this_hold(personal_client, db):
    """Wave 7b UI review: the UI's reconciliation body goes through the existing Decision API as a HUMAN
    RISK_ACCEPTANCE bound to the hold id and lifts the hold; the summary then no longer states INTEGRITY_HOLD."""
    from aeos.worker_runtime import integrity

    hold = integrity.raise_hold(db, source="worker run", subject_id="run-1", domain="PERSONAL",
                                findings=[{"kind": "controller", "path": "backend/aeos/core/config.py",
                                           "change": "modified"}])
    db.commit()
    assert personal_client.get("/api/workforce/summary").json()["sandbox"]["code"] == "INTEGRITY_HOLD"
    r = personal_client.post("/api/decisions", json=_ui_reconcile_body(hold["id"]))
    assert r.status_code == 201, r.text
    sb = personal_client.get("/api/workforce/summary").json()["sandbox"]
    assert sb["code"] != "INTEGRITY_HOLD" and sb["integrity_hold"] is None
    db.expire_all()
    assert integrity.hold_view(db)["reconciled_by_decision"] == r.json()["id"]


def test_a_viewer_cannot_reconcile_the_hold_or_set_the_route_behaviour_threshold(client, db):
    from aeos.worker_runtime import integrity
    from tests.conftest import create_admin

    create_admin(db)
    create_viewer(db)
    hold = integrity.raise_hold(db, source="worker run", subject_id="run-1", domain="PERSONAL",
                                findings=[{"kind": "db", "path": "decisions:x", "change": "added"}])
    db.commit()
    client.login("viewer", VIEWER_PW)
    client.select_domain(Domain.PERSONAL)
    assert client.post("/api/decisions", json=_ui_reconcile_body(hold["id"])).status_code == 403
    assert client.post("/api/decisions", json=_ui_threshold_body(0.05)).status_code == 403
    db.expire_all()
    assert integrity.hold_status(db) is not None


def _ui_threshold_body(rate: float) -> dict:
    """Exactly what Kesiapan -> worker.route_behaviour -> 'Tetapkan ambang pemilik' sends
    (frontend/src/components/doctor/RouteBehaviourEvidence.tsx)."""
    return {"kind": "POLICY", "target_type": "worker_route_behaviour", "target_id": "tool_schema_failures",
            "rationale": "pilot: at most 5% of samples may fail",
            "auth_scope": {"threshold": "worker.route_behaviour tool_schema_failures", "recorded_via": "Control Center"},
            "allowed_operations": [], "details": {"max_failure_rate": rate}}


def test_the_control_center_threshold_is_the_one_the_doctor_check_reads(personal_client, db):
    from aeos.doctor.checks import worker as worker_checks

    assert worker_checks._threshold(db) == (None, None)  # UNDECIDED: reported, never judged
    r = personal_client.post("/api/decisions", json=_ui_threshold_body(0.05))
    assert r.status_code == 201, r.text
    db.expire_all()
    assert worker_checks._threshold(db) == (0.05, r.json()["id"])
