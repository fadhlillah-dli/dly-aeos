"""Model Evaluation API (§43, §44; §50 "VIEWER memanggil write endpoint -> Ditolak backend")."""

from __future__ import annotations

from sqlalchemy import select

from aeos.core.enums import Domain
from aeos.evaluation import definitions as D
from aeos.evaluation import service
from aeos.models.research import ChangeProposal
from aeos.models.routing import ModelRoute
from tests.conftest import VIEWER_PW, AuthedClient, create_admin, create_viewer

CFG = {"model_id": "glm-5.3-flash", "model_version": "UNKNOWN", "provider": "opencode-zen",
       "provider_route": "opencode-zen/glm-flash-free", "harness": "opencode", "harness_version": "1.4.0",
       "prompt_revision": "impl-v1", "tools": ["edit"], "policy_version": "pol-1", "context_params": {}}


def _seed(db):
    service.seed_suites(db)
    db.commit()


def test_suites_experiments_and_empty_leaderboard(personal_client: AuthedClient, db):
    _seed(db)
    suites = personal_client.get("/api/eval/suites").json()
    assert len(suites["items"]) == len(D.SUITES) == 28 and suites["mode"] == "DESIGN"
    assert all(s["held_out"] and s["protected"] for s in suites["items"])
    exp = personal_client.get("/api/eval/experiments").json()
    assert [e["key"] for e in exp["items"]] == ["A", "B", "C", "D"] and len(exp["dimensions"]) == 7
    board = personal_client.get("/api/eval/leaderboard", params={"role": "feature-implementer"}).json()
    assert board["status"] == "BELUM_DIUJI" and board["message"] == "Semua kandidat BELUM DIUJI dalam platform ini"


def test_plan_run_is_planned_only(personal_client: AuthedClient, db):
    _seed(db)
    r = personal_client.post("/api/eval/runs", json={"suite_key": "eval.docs-only-change", "configuration": CFG,
                                                    "topology": "A", "limits": {"max_attempts": 1, "max_minutes": 20,
                                                                                "max_cost_usd": 0}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "PLANNED" and body["execution"]["status"] == "NOT_STARTED"
    assert body["configuration_hash"].startswith("sha256:")
    runs = personal_client.get("/api/eval/runs").json()["items"]
    assert len(runs) == 1 and runs[0]["role_key"] == "technical-writer"
    bad = personal_client.post("/api/eval/runs", json={"suite_key": "eval.docs-only-change",
                                                      "configuration": {"model_id": "x"},
                                                      "limits": {"max_attempts": 1, "max_minutes": 1, "max_cost_usd": 0}})
    assert bad.status_code == 422


def test_scorecard_opens_proposal_and_never_changes_routes(personal_client: AuthedClient, db):
    _seed(db)
    db.add(ModelRoute(route_key="opencode-zen/glm-flash-free", pool="WORKER", provider="opencode-zen",
                      model_id="glm-5.3-flash", status="CANDIDATE"))
    db.commit()
    r = personal_client.post("/api/eval/scorecard", json={"role": "feature-implementer"})
    assert r.status_code == 200, r.text
    assert r.json()["route_changed"] is False
    p = db.get(ChangeProposal, r.json()["proposal_id"])
    assert p.kind == "COMPARE" and p.status == "SUBMITTED" and p.domain == "PERSONAL"
    route = db.scalar(select(ModelRoute).where(ModelRoute.route_key == "opencode-zen/glm-flash-free"))
    assert route.status == "CANDIDATE"


def test_viewer_cannot_plan_runs(client: AuthedClient, db):
    create_admin(db)
    create_viewer(db)
    _seed(db)
    client.login("viewer", VIEWER_PW)
    client.select_domain(Domain.PERSONAL)
    r = client.post("/api/eval/runs", json={"suite_key": "eval.small-bugfix", "configuration": CFG,
                                           "limits": {"max_attempts": 1, "max_minutes": 1, "max_cost_usd": 0}})
    assert r.status_code == 403
    assert client.get("/api/eval/leaderboard").status_code == 200


def test_office_planned_runs_never_reach_personal_views(admin_client: AuthedClient, db):
    """§23/§35: a run planned in an OFFICE session (its notes may describe Office work) is not listed or
    ranked in PERSONAL; platform rows (no session domain) stay visible to both."""
    _seed(db)
    admin_client.select_domain(Domain.OFFICE)
    r = admin_client.post("/api/eval/runs", json={"suite_key": "eval.small-bugfix", "configuration": CFG,
                                                 "notes": "office pilot for client ACME",
                                                 "limits": {"max_attempts": 1, "max_minutes": 5, "max_cost_usd": 0}})
    assert r.status_code == 200, r.text
    assert len(admin_client.get("/api/eval/runs").json()["items"]) == 1
    office_board = admin_client.get("/api/eval/leaderboard", params={"role": "feature-implementer"}).json()
    assert any(c.get("configuration_hash") for c in office_board["candidates_not_tested"])
    admin_client.select_domain(Domain.PERSONAL)
    assert admin_client.get("/api/eval/runs").json()["items"] == []
    board = admin_client.get("/api/eval/leaderboard", params={"role": "feature-implementer"}).json()
    assert "ACME" not in str(board) and not any(c.get("configuration_hash") for c in board["candidates_not_tested"])


def test_execution_endpoints_start_bind_collect_and_office_isolation(admin_client: AuthedClient, db):
    """Owner starts a run, binds executed tasks to its slots and triggers the trusted collector over HTTP; an
    OFFICE session can neither see nor drive a PERSONAL run."""
    from aeos.routing import seed as route_seed
    from tests.unit.test_evaluation_execution import _executed
    from tests.unit.test_workforce_support import make_plan, make_project

    route_seed.run(db)
    _seed(db)
    admin_client.select_domain(Domain.PERSONAL)
    cfg = CFG | {"model_id": "opencode/deepseek-v4.1-flash-free", "provider_route": "opencode-zen/deepseek-v4.1-flash-free",
                 "harness_version": "1.19.3"}
    run = admin_client.post("/api/eval/runs", json={"suite_key": "eval.small-bugfix", "configuration": cfg,
                                                   "repetitions": 1, "is_baseline": True,
                                                   "limits": {"max_attempts": 1, "max_minutes": 5, "max_cost_usd": 0}})
    assert run.status_code == 200, run.text
    rid = run.json()["id"]
    assert run.json()["is_baseline"] is True and run.json()["execution"]["status"] == "NOT_STARTED"
    started = admin_client.post(f"/api/eval/runs/{rid}/start")
    assert started.status_code == 200, started.text
    slots = started.json()["slots"]
    assert len(slots) == 5 and started.json()["execution"]["status"] == "COLLECTING"
    project = make_project(db)
    _, _, specs = make_plan(db, project, [{"task_key": f"T{i}"} for i in range(5)])
    db.commit()
    for s, spec in zip(slots, specs, strict=True):
        r = admin_client.post(f"/api/eval/runs/{rid}/bind", json={"slot_id": s["slot_id"], "task_id": spec.id})
        assert r.status_code == 200, r.text
    out = admin_client.post(f"/api/eval/runs/{rid}/collect").json()
    assert out["recorded"] is False and len(out["pending"]) == 5
    for spec in specs:
        _executed(db, spec, state="DONE", accepted="ACCEPTED")
    db.commit()
    admin_client.select_domain(Domain.OFFICE)
    assert admin_client.get(f"/api/eval/runs/{rid}").status_code == 404
    assert admin_client.post(f"/api/eval/runs/{rid}/collect").status_code == 404
    admin_client.select_domain(Domain.PERSONAL)
    out = admin_client.post(f"/api/eval/runs/{rid}/collect").json()
    assert out["recorded"] is True and out["status"] == "COMPLETED"
    detail = admin_client.get(f"/api/eval/runs/{rid}").json()
    assert detail["result_source"] == "collector" and detail["sample_size"] == 5
    assert detail["execution"]["status"] == "RECORDED" and detail["route_binding"]["route_key"].startswith("opencode-zen/")
    # the executed run reaches the leaderboard - once the owner has reviewed the route's terms (terms gate)
    suite = next(s for s in admin_client.get("/api/eval/leaderboard", params={"role": "feature-implementer"}).json()["suites"]
                 if s["suite_key"] == "eval.small-bugfix")
    assert suite["excluded"][0]["eligibility"]["status"] == "EXCLUDED_ROUTE_TERMS"
    route = db.scalar(select(ModelRoute).where(ModelRoute.route_key == "opencode-zen/deepseek-v4.1-flash-free"))
    from aeos.core.timeutil import utcnow

    route.terms_checked_at = route.pricing_checked_at = utcnow()  # owner re-read pricing + terms (no date bomb)
    db.commit()
    suite = next(s for s in admin_client.get("/api/eval/leaderboard", params={"role": "feature-implementer"}).json()["suites"]
                 if s["suite_key"] == "eval.small-bugfix")
    assert suite["ranking"][0]["run_ids"] == [rid] and suite["ranking"][0]["sample_size"] == 5


def test_comparison_weights_cases_and_blind_review_endpoints(personal_client: AuthedClient, db):
    from aeos.models.workforce import WorkerRun
    from tests.unit.test_workforce_support import make_project

    _seed(db)
    p = ChangeProposal(kind="UPGRADE", title="new harness", submitted_by="owner", status="TRIAL_APPROVED",
                       domain="PERSONAL")
    db.add(p)
    db.commit()
    limits = {"max_attempts": 1, "max_minutes": 5, "max_cost_usd": 0}
    r = personal_client.post("/api/eval/comparisons", json={
        "proposal_id": p.id, "suite_keys": ["eval.small-bugfix"], "baseline": CFG,
        "candidate": CFG | {"harness_version": "1.5.0"}, "limits": limits, "repetitions": 1})
    assert r.status_code == 200, r.text
    assert len(r.json()["run_ids"]) == 2
    comp = personal_client.get(f"/api/eval/comparisons/{p.id}").json()
    assert comp["status"] == "IN_PROGRESS" and comp["suites"][0]["verdict"] == "NOT_RECORDED"
    assert len(personal_client.get("/api/eval/runs", params={"proposal_id": p.id}).json()["items"]) == 2
    w = personal_client.post("/api/eval/weights", json={"role": "feature-implementer", "rationale": "bugfix dominates",
                                                       "weights": {"eval.small-bugfix": 2, "eval.feature-fe-api": 1}})
    assert w.status_code == 200, w.text
    board = personal_client.get("/api/eval/leaderboard", params={"role": "feature-implementer"}).json()
    assert board["combined"]["weights"] == {"eval.small-bugfix": 2.0, "eval.feature-fe-api": 1.0}
    project = make_project(db)
    failed = WorkerRun(task_id="t9", project_id=project.id, domain="PERSONAL", execution_status="FAILED",
                       outcome_code="TIMEOUT")
    db.add(failed)
    db.commit()
    c = personal_client.post("/api/eval/cases", json={"source_type": "worker_run", "source_id": failed.id,
                                                     "title": "Timeout on long migration",
                                                     "expected": "Task finishes within its time limit or stops cleanly"})
    assert c.status_code == 200, c.text
    assert c.json()["status"] == "CANDIDATE"
    assert [x["id"] for x in personal_client.get("/api/eval/cases").json()["items"]] == [c.json()["id"]]
    adopted = personal_client.post(f"/api/eval/cases/{c.json()['id']}/adopt", json={
        "expected_case_hash": c.json()["case_hash"], "rationale": "reviewed: a real regression case"})
    assert adopted.status_code == 200, adopted.text
    assert adopted.json()["suite"]["protected"] is True
    assert personal_client.get("/api/eval/blind-review", params={"suite_key": "eval.small-bugfix"}).json()["items"] == []
