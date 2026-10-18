"""Costs & Quotas API (§35, §42; §50 "VIEWER memanggil write endpoint langsung -> Ditolak backend",
"Dashboard Personal mencoba ... Office -> Backend menolak")."""

from __future__ import annotations

import pytest

from aeos.core.enums import Domain
from aeos.cost import facade
from aeos.models.portfolio import Project
from tests.conftest import VIEWER_PW, AuthedClient, create_viewer
from tests.unit.test_cost_fakes import install_fake_policy, remove_policy


@pytest.fixture
def policy(monkeypatch):
    return install_fake_policy(monkeypatch)


def test_summary_requires_domain_selection(admin_client: AuthedClient):
    r = admin_client.get("/api/costs/summary")
    assert r.status_code == 409 and r.json()["error"]["code"] == "DOMAIN_NOT_SELECTED"


def test_summary_shapes_actual_estimate_unknown_and_native(personal_client: AuthedClient, db):
    facade.record_usage(db, provider="openrouter", category="WORKER_API", source="provider", cost_usd=0.4,
                        domain="PERSONAL")
    facade.record_usage(db, provider="claude-native", category="NATIVE_CLAUDE", source="bridge", cost_usd=2.0,
                        domain="PERSONAL")
    db.commit()
    body = personal_client.get("/api/costs/summary").json()
    assert body["totals"]["actual_usd"] == pytest.approx(0.4)
    assert body["native_subscription"]["dollar_cost"] is None
    assert body["native_subscription"]["remaining_quota"] == "UNKNOWN"
    assert body["stop_caps"][0]["scope_type"] == "GLOBAL" and body["stop_caps"][0]["limit_usd"] == 0
    assert {"source", "checked_at", "mode"} <= set(body)


def test_ledger_and_budgets_and_quotas_lists(personal_client: AuthedClient, db):
    facade.record_usage(db, provider="openrouter", category="WORKER_API", source="provider", cost_usd=None,
                        domain="PERSONAL")
    db.commit()
    led = personal_client.get("/api/costs/ledger").json()
    assert led["items"][0]["cost_kind"] == "UNKNOWN" and led["mode"] == "LIVE"
    b = personal_client.get("/api/budgets").json()
    assert b["items"][0]["implicit"] is True and b["policy"]["paid_fallback"] is False
    q = personal_client.get("/api/quotas").json()
    assert q["items"][0]["provider"] == "claude-native" and q["items"][0]["state"] == "UNKNOWN"


def test_post_budget_records_decision(personal_client: AuthedClient, policy):
    r = personal_client.post("/api/budgets", json={"scope_type": "GLOBAL", "limit_usd": 2.5,
                                                   "rationale": "pilot worker budget"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"]["kind"] == "BUDGET" and body["limit_usd"] == 2.5 and body["state"] == "OK"
    listed = personal_client.get("/api/budgets").json()["items"]
    assert [b["limit_usd"] for b in listed if b["scope_type"] == "GLOBAL"] == [2.5]


def test_post_budget_fails_closed_without_policy_registry(personal_client: AuthedClient, monkeypatch):
    remove_policy(monkeypatch)
    r = personal_client.post("/api/budgets", json={"scope_type": "GLOBAL", "limit_usd": 2.5, "rationale": "x"*5})
    assert r.status_code == 409 and r.json()["error"]["code"] == "DECISION_REGISTRY_UNAVAILABLE"
    assert personal_client.get("/api/budgets").json()["items"][0]["limit_usd"] == 0


def test_project_budget_cross_domain_is_404(personal_client: AuthedClient, db, policy):
    db.add(Project(id="po", project_key="office-x", name="Office X", domain="OFFICE"))
    db.commit()
    r = personal_client.post("/api/budgets", json={"scope_type": "PROJECT", "scope_id": "po", "limit_usd": 1,
                                                   "rationale": "cap office"})
    assert r.status_code == 404


def test_route_budget_requires_registered_route(personal_client: AuthedClient, policy):
    r = personal_client.post("/api/budgets", json={"scope_type": "ROUTE", "scope_id": "nope", "limit_requests": 5,
                                                   "rationale": "route cap"})
    assert r.status_code == 404


def test_viewer_cannot_change_budget_or_run_estimator(client: AuthedClient, db, policy):
    from tests.conftest import create_admin

    create_admin(db)
    create_viewer(db)
    client.login("viewer", VIEWER_PW)
    client.select_domain(Domain.PERSONAL)
    r = client.post("/api/budgets", json={"scope_type": "GLOBAL", "limit_usd": 99, "rationale": "viewer try"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "READ_ONLY_ROLE"
    assert client.post("/api/costs/native-estimate").status_code == 403
    assert client.get("/api/costs/summary").status_code == 200


def test_native_estimate_endpoint_reports_not_available(personal_client: AuthedClient, monkeypatch):
    monkeypatch.setattr("aeos.cost.ccusage.find_command", lambda: None)
    body = personal_client.post("/api/costs/native-estimate").json()
    assert body["status"] == "NOT_AVAILABLE" and body["mode"] == "ESTIMATE"
