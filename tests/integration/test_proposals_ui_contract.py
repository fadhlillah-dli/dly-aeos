"""API <-> UI contract guard for module group H pages (§35).

The frontend renders committed fixtures (``frontend/src/pages/*/__fixtures__``) that were captured
from these endpoints. If a response shape drifts, this test fails so fixtures/UI are updated
together instead of the UI silently showing blanks."""

from __future__ import annotations

import json

import pytest

from aeos.core.paths import repo_root
from aeos.cost import facade as cost
from aeos.evaluation import service as evals
from aeos.models.inventory import ComponentReference
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
