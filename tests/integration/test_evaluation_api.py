"""Model Evaluation API (§43, §44; §50 "VIEWER memanggil write endpoint -> Ditolak backend")."""

from __future__ import annotations

from sqlalchemy import select

from aeos.core.enums import Domain
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
    assert len(suites["items"]) == 8 and suites["mode"] == "DESIGN"
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
