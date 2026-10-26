"""API <-> UI contract guard for module group H pages (§35).

The frontend renders committed fixtures (``frontend/src/pages/*/__fixtures__``) that were captured
from these endpoints. If a response shape drifts, this test fails so fixtures/UI are updated
together instead of the UI silently showing blanks."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from aeos.core.paths import repo_root
from aeos.cost import facade as cost
from aeos.evaluation import service as evals
from aeos.models.inventory import ComponentReference
from aeos.models.routing import ModelRoute
from aeos.research import service as research
from tests.conftest import AuthedClient
from tests.unit.test_cost_fakes import install_fake_policy

PAGES = repo_root() / "frontend" / "src" / "pages"


def _fixture(area: str, name: str) -> dict:
    return json.loads((PAGES / area / "__fixtures__" / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def policy(monkeypatch):
    return install_fake_policy(monkeypatch)


def test_proposal_detail_shape_matches_ui_fixture(personal_client: AuthedClient, db, policy):
    db.add(ComponentReference(component_id="opencode", kind="worker-engine", name="OpenCode", approved_pin="1.3.0"))
    db.commit()
    p = personal_client.post("/api/proposals", json={"kind": "REPLACE", "sources": ["https://github.com/anomalyco/opencode"],
                                                     "existing_component_ids": ["opencode"], "candidate_pin": "1.4.0"}).json()
    live = personal_client.post(f"/api/proposals/{p['id']}/review", json={}).json()
    fx = _fixture("proposals", "detail")
    assert set(live) == set(fx)
    assert set(live["review_package"]) == set(fx["review_package"])
    assert set(live["review_package"]["human_decision"]) == set(fx["review_package"]["human_decision"])
    tl = personal_client.get(f"/api/proposals/{p['id']}/timeline").json()
    assert set(tl) == set(_fixture("proposals", "timeline"))


def test_costs_summary_shape_matches_ui_fixture(personal_client: AuthedClient, db):
    cost.record_usage(db, provider="openrouter", category="WORKER_API", source="provider", cost_usd=0.1, domain="PERSONAL")
    db.commit()
    live = personal_client.get("/api/costs/summary").json()
    fx = _fixture("costs", "summary")
    assert set(live) == set(fx)
    assert set(live["native_subscription"]) == set(fx["native_subscription"])
    assert set(live["by_category"][0]) == set(fx["by_category"][0])
    assert set(personal_client.get("/api/budgets").json()["items"][0]) <= set(fx["stop_caps"][0]) | {"spend_hidden", "spend_note"}


def test_research_and_eval_shapes_match_ui_fixtures(personal_client: AuthedClient, db):
    research.seed_registry(db)
    evals.seed_suites(db)
    db.commit()
    item = personal_client.get("/api/research?kind=candidate").json()["items"][0]
    assert set(item) == set(_fixture("research", "candidates")["items"][0])
    board = personal_client.get("/api/eval/leaderboard").json()
    assert set(board) == set(_fixture("evaluation", "leaderboard"))
    suite = personal_client.get("/api/eval/suites").json()["items"][0]
    assert set(suite) == set(_fixture("evaluation", "suites")["items"][0])


def test_component_and_route_detail_shapes_match_ui_fixtures(personal_client: AuthedClient, db):
    """The explain-button page tests (W6-109) render real responses of these endpoints: keep their shape in sync."""
    for mod in ("aeos.policy.seed", "aeos.inventory.seed", "aeos.routing.seed"):
        __import__(mod, fromlist=["run"]).run(db)
    db.commit()
    comp = personal_client.get("/api/inventory/components/opencode").json()
    fx = _fixture("tools", "component-detail")
    assert set(comp) == set(fx)
    assert set(comp["usage_role"]) == set(fx["usage_role"]) and set(comp["identity"]) == set(fx["identity"])
    blast = personal_client.get("/api/inventory/blast-radius/opencode").json()
    assert set(blast) == set(_fixture("tools", "blast-radius"))
    key = _fixture("models", "route-detail")["route_key"]
    route = personal_client.get(f"/api/routes/{key}").json()
    assert set(route) == set(_fixture("models", "route-detail"))


def test_subscription_shapes_match_ui_fixtures(personal_client: AuthedClient, db):
    """OpenCode Go (ADR-0080 amendment): the Costs page and the route drawer render the plan + allowance windows of a
    SUBSCRIPTION route from these fixtures."""
    for mod in ("aeos.policy.seed", "aeos.routing.seed"):
        __import__(mod, fromlist=["run"]).run(db)
    key = _fixture("models", "route-detail-go")["route_key"]
    db.scalars(select(ModelRoute).where(ModelRoute.route_key == key)).one().status = "TRIAL"
    cost.record_usage(db, provider="opencode-go", category="WORKER_API", source="broker", route_key=key, domain="PERSONAL",
                      input_tokens=12000, output_tokens=1500, cost_usd=0.0, details={"reported_allowance_usd": 0.42})
    db.commit()
    route = personal_client.get(f"/api/routes/{key}").json()
    fx = _fixture("models", "route-detail-go")
    assert set(route) == set(fx)
    assert route["billing"] == fx["billing"] == "SUBSCRIPTION" and fx["budget_note"]
    assert set(route["subscription"]) == set(fx["subscription"])
    assert set(route["subscription"]["windows"][0]) == set(fx["subscription"]["windows"][0])
    assert [w["window"] for w in route["subscription"]["windows"]] == [w["window"] for w in fx["subscription"]["windows"]]
    summary = personal_client.get("/api/costs/summary").json()
    fx_sub = _fixture("costs", "summary")["subscriptions"][0]
    assert set(summary["subscriptions"][0]) == set(fx_sub)
    assert set(summary["subscriptions"][0]["windows"][0]) == set(fx_sub["windows"][0])
