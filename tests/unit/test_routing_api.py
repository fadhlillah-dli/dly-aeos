"""Models & Routes API (§23, §35; §50 'VIEWER memanggil write endpoint langsung', 'Dashboard Personal
mencoba run/evidence Office', 'Model request sudah selesai -> LAST_USED')."""

from __future__ import annotations

import httpx
import respx

from aeos.core.enums import Domain
from aeos.core.timeutil import utcnow
from aeos.models.audit import AuditEvent, Event
from aeos.models.cost import UsageLedgerEntry
from aeos.routing import catalog, seed, service
from aeos.secrets.provider import reset_provider_cache
from tests.conftest import VIEWER_PW, AuthedClient, create_viewer
from tests.unit.test_routing_fakes import human_decision, install_fakes

DS = "opencode-zen/deepseek-v4.1-flash-free"
QWEN = "opencode-zen/qwen3.8-flash"
LAGUNA = "openrouter/poolside/laguna-s-2.1:free"


def _seed(db):
    """Seed + what an owner terms review and a catalog refresh record (none of it is part of the identity)."""
    seed.run(db)
    for r in db.query(service.ModelRoute).all():
        r.pricing_checked_at = r.terms_checked_at = utcnow()
        r.availability, r.availability_checked_at = "AVAILABLE", utcnow()
    db.commit()


def test_list_routes_is_design_and_unknown_by_default(personal_client: AuthedClient, db):
    _seed(db)
    r = personal_client.get("/api/routes")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "DESIGN" and body["counts"]["approved"] == 0
    item = next(i for i in body["items"] if i["route_key"] == DS)
    assert item["resolved_version"] == "UNKNOWN" and item["eval_summary"]["state"] == "BELUM_DIUJI"
    assert item["status"] == "CANDIDATE" and item["last_used"] is None and item["config_hash"].startswith("sha256:")
    assert item["eligible_domains"] == ["PERSONAL"]
    only = personal_client.get("/api/routes", params={"pool": "SUPERVISOR"}).json()["items"]
    assert {i["route_key"] for i in only} == {"claude-native/fable", "claude-native/opus"}
    assert personal_client.get("/api/routes", params={"domain": "OFFICE"}).json()["items"] == []


def test_route_detail_with_slashes_in_key(personal_client: AuthedClient, db):
    _seed(db)
    r = personal_client.get(f"/api/routes/{LAGUNA}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model_id"] == "poolside/laguna-s-2.1:free" and body["config"]["route_key"] == LAGUNA
    assert body["decisions"] == [] and body["free_until"].startswith("2026-10-31")
    assert personal_client.get("/api/routes/nope/missing").status_code == 404


def test_last_used_is_domain_scoped(personal_client: AuthedClient, db):
    _seed(db)
    db.add(UsageLedgerEntry(route_key=DS, provider="opencode-zen", category="WORKER_API", source="broker",
                            domain="OFFICE", run_id="office-run", at=utcnow()))
    db.commit()
    item = personal_client.get(f"/api/routes/{DS}").json()
    assert item["last_used"] is None  # Office usage never surfaces in a Personal session
    db.add(UsageLedgerEntry(route_key=DS, provider="opencode-zen", category="WORKER_API", source="broker",
                            domain="PERSONAL", run_id="personal-run", at=utcnow()))
    db.commit()
    item = personal_client.get(f"/api/routes/{DS}").json()
    assert item["last_used"]["run_id"] == "personal-run" and item["last_used"]["status"] == "LAST_USED"
    for i in range(25):  # a later burst of Office usage neither leaks nor hides the Personal one
        db.add(UsageLedgerEntry(route_key=DS, provider="opencode-zen", category="WORKER_API", source="broker",
                                domain="OFFICE", run_id=f"office-burst-{i}", at=utcnow()))
    db.commit()
    body = personal_client.get(f"/api/routes/{DS}").text
    assert '"run_id":"personal-run"' in body.replace(" ", "") and "office-burst" not in body


def test_eligibility_requires_explicit_domain(personal_client: AuthedClient, db):
    _seed(db)
    assert personal_client.get("/api/routes/eligibility", params={"classification": "INTERNAL"}).status_code == 422
    r = personal_client.get("/api/routes/eligibility", params={"domain": "PERSONAL", "classification": "INTERNAL"})
    assert r.status_code == 200 and r.json()["eligible"] == [] and DS in r.json()["rejected"]
    r = personal_client.get("/api/routes/eligibility", params={"domain": "", "classification": "INTERNAL"})
    assert r.status_code == 422


def test_viewer_cannot_activate_disable_or_refresh(client: AuthedClient, db):
    from tests.conftest import create_admin

    create_admin(db)
    create_viewer(db)
    _seed(db)
    client.login("viewer", VIEWER_PW)
    h = service.get_route(db, DS).config_hash
    body = {"mode": "TRIAL", "rationale": "viewer tries to approve", "expected_config_hash": h}
    for url, payload in ((f"/api/routes/{DS}/activate", body), (f"/api/routes/{DS}/disable", {"reason": "x" * 5}),
                         ("/api/routes/refresh-catalog", {})):
        r = client.post(url, json=payload)
        assert r.status_code == 403 and r.json()["error"]["code"] == "READ_ONLY_ROLE"
    db.expire_all()
    assert service.get_route(db, DS).status == "CANDIDATE"


def test_admin_activation_flow_records_decision_audit_event(personal_client: AuthedClient, db, monkeypatch):
    install_fakes(monkeypatch)
    _seed(db)
    h = service.get_route(db, DS).config_hash
    r = personal_client.post(f"/api/routes/{DS}/activate", json={"mode": "TRIAL", "rationale": "pilot for slice 1",
                                                                  "expected_config_hash": h})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["decision_kind"] == "TRIAL" and out["route"]["status"] == "TRIAL"
    assert out["route"]["active_decision"]["id"] == out["decision_id"]
    assert db.query(AuditEvent).filter_by(action="route.activate", target_id=DS).count() == 1
    assert db.query(Event).filter_by(kind="route.activated", entity_id=DS).count() == 1
    elig = personal_client.get("/api/routes/eligibility", params={"domain": "PERSONAL", "classification": "INTERNAL"})
    assert [e["route_key"] for e in elig.json()["eligible"]] == [DS]
    r = personal_client.post(f"/api/routes/{DS}/disable", json={"reason": "terms changed"})
    assert r.status_code == 200 and r.json()["route"]["status"] == "DISABLED"


def test_activation_conflicts_and_budget(personal_client: AuthedClient, db, monkeypatch):
    install_fakes(monkeypatch)
    _seed(db)
    r = personal_client.post(f"/api/routes/{DS}/activate", json={"mode": "APPROVED", "rationale": "stale review here",
                                                                  "expected_config_hash": "sha256:" + "0" * 64})
    assert r.status_code == 409 and r.json()["error"]["code"] == "CONFIG_HASH_MISMATCH"
    qh = service.get_route(db, QWEN).config_hash
    r = personal_client.post(f"/api/routes/{QWEN}/activate", json={"mode": "APPROVED", "rationale": "paid but no budget",
                                                                    "expected_config_hash": qh})
    assert r.status_code == 422 and r.json()["error"]["code"] == "BUDGET_DECISION_REQUIRED"
    budget = human_decision(db, kind="BUDGET", target_type="model_route", target_id=QWEN)
    db.commit()
    r = personal_client.post(f"/api/routes/{QWEN}/activate", json={
        "mode": "APPROVED", "rationale": "paid with approved budget", "expected_config_hash": qh,
        "budget_decision_id": budget.id})
    # a budget is not test evidence: production needs recorded EvalRuns of this configuration (§23)
    assert r.status_code == 422 and r.json()["error"]["code"] == "TEST_EVIDENCE_REQUIRED", r.text
    assert "no EvalRun referenced" in str(r.json()["error"]["details"]["problems"])
    r = personal_client.post(f"/api/routes/{QWEN}/activate", json={
        "mode": "TRIAL", "rationale": "paid trial with approved budget", "expected_config_hash": qh,
        "budget_decision_id": budget.id})
    assert r.status_code == 200, r.text


@respx.mock
def test_refresh_catalog_endpoint(personal_client: AuthedClient, db, monkeypatch):
    reset_provider_cache()
    _seed(db)
    monkeypatch.setattr(catalog, "_opencode_binary", lambda s: None)
    respx.route(url__startswith="http://127.0.0.1:8200").mock(side_effect=httpx.ConnectError("vault down"))
    respx.get(catalog.OPENROUTER_MODELS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    r = personal_client.post("/api/routes/refresh-catalog")
    assert r.status_code == 200, r.text
    assert r.json()["results"]["openrouter_key"]["status"] == "UNKNOWN"
    listing = personal_client.get("/api/routes").json()
    assert listing["catalog_refresh"]["status"] in {"OK", "PARTIAL"}
    assert db.query(AuditEvent).filter_by(action="routes.catalog_refresh").count() == 1


def test_office_session_sees_no_personal_bias(admin_client: AuthedClient, db):
    """OFFICE session: eligibility for OFFICE is denied by default (no company authorisation)."""
    _seed(db)
    admin_client.select_domain(Domain.OFFICE)
    r = admin_client.get("/api/routes/eligibility", params={"domain": "OFFICE", "classification": "INTERNAL"})
    assert r.status_code == 200 and r.json()["eligible"] == []


def test_decision_view_effective_only_while_human_active_unexpired_and_current(
        personal_client: AuthedClient, db, monkeypatch):
    """The drawer may show a decision as approving only when it really authorises the route now."""
    install_fakes(monkeypatch)
    _seed(db)
    h = service.get_route(db, DS).config_hash
    r = personal_client.post(f"/api/routes/{DS}/activate", json={"mode": "TRIAL", "rationale": "pilot for slice 1",
                                                                  "expected_config_hash": h})
    assert r.status_code == 200, r.text
    h = r.json()["route"]["config_hash"]  # the activation binds the runtime in force: the decision's hash
    detail = personal_client.get(f"/api/routes/{DS}").json()
    assert [d["effective"] for d in detail["decisions"]] == [True]
    db.expire_all()
    d = db.get(service.Decision, r.json()["decision_id"])
    d.expires_at = utcnow().replace(year=2020)  # still ACTIVE, but expired
    db.commit()
    detail = personal_client.get(f"/api/routes/{DS}").json()
    assert detail["decisions"][0]["status"] == "ACTIVE" and detail["decisions"][0]["effective"] is False
    assert detail["active_decision"] is None
    human_decision(db, kind="TRIAL", target_type="model_route", target_id=DS, target_hash=h, actor_kind="AGENT")
    db.commit()
    agent = next(x for x in personal_client.get(f"/api/routes/{DS}").json()["decisions"] if x["actor_kind"] == "AGENT")
    assert agent["matches_current_config"] is True and agent["effective"] is False  # model output is never AUTH


def test_activation_with_same_classes_keeps_the_reviewed_hash(personal_client: AuthedClient, db, monkeypatch):
    """Re-confirming the reviewed classes (in any order) must not mint a new identity: eval results and the
    reviewed hash stay valid. Narrowing the classes is a real change and gets a new hash. (The first activation
    binds the runtime in force - policy version, worker prompt, context strategy - which is a new identity.)"""
    install_fakes(monkeypatch)
    _seed(db)
    h0 = service.get_route(db, DS).config_hash
    first = personal_client.post(f"/api/routes/{DS}/activate", json={
        "mode": "TRIAL", "rationale": "pilot for slice 1", "expected_config_hash": h0})
    assert first.status_code == 200, first.text
    h = first.json()["route"]["config_hash"]
    binding = first.json()["route"]["runtime_binding"]
    assert h != h0 and binding["status"] == "BOUND" and binding["bound"]["policy_version"].startswith("pol-")
    r = personal_client.post(f"/api/routes/{DS}/activate", json={
        "mode": "TRIAL", "rationale": "pilot for slice 1", "expected_config_hash": h,
        "allowed_data_classes": ["INTERNAL", "PUBLIC"]})
    assert r.status_code == 200, r.text
    assert r.json()["route"]["config_hash"] == h and r.json()["route"]["allowed_data_classes"] == ["PUBLIC", "INTERNAL"]
    r = personal_client.post(f"/api/routes/{DS}/activate", json={
        "mode": "TRIAL", "rationale": "public data only now", "expected_config_hash": h,
        "allowed_data_classes": ["PUBLIC"]})
    assert r.status_code == 200 and r.json()["route"]["config_hash"] != h


def test_approved_activation_with_recorded_evaluation_over_the_api(personal_client: AuthedClient, db, monkeypatch):
    """§23 'diizinkan dan telah diuji' end to end over HTTP: TRIAL -> EvalRun on exactly that configuration
    (recorded by the trusted collector) -> APPROVED referencing it -> the route view shows the tested role and
    the rollback target; the eligibility endpoint ranks by the evaluated result."""
    from aeos.evaluation import facade as evaluation
    from aeos.evaluation import service as evaluation_service

    install_fakes(monkeypatch)
    _seed(db)
    evaluation_service.seed_suites(db)
    db.commit()
    h0 = service.get_route(db, DS).config_hash
    t = personal_client.post(f"/api/routes/{DS}/activate", json={"mode": "TRIAL", "rationale": "pilot for slice 1",
                                                                  "expected_config_hash": h0})
    h = t.json()["route"]["config_hash"]
    route = service.get_route(db, DS)
    cfg = {"model_id": route.model_id, "model_version": "UNKNOWN", "provider": route.provider, "provider_route": DS,
           "harness": "opencode", "harness_version": "UNKNOWN", "prompt_revision": route.prompt_revision,
           "tools": route.tool_set, "policy_version": route.policy_version, "context_params": {}}
    run = personal_client.post("/api/eval/runs", json={"suite_key": "eval.small-bugfix", "configuration": cfg,
                                                       "repetitions": 1, "limits": {"max_attempts": 2, "max_minutes": 30,
                                                                                    "max_cost_usd": 0}})
    assert run.status_code == 200, run.text
    assert run.json()["route_config_hash"] == h and run.json()["route_binding"]["status"] == "ELIGIBLE"
    evaluation.record_result(db, run.json()["id"], result_source="collector",
                             samples=[{"sample_id": f"s{i}", "status": "ACCEPTED", "first_pass": True} for i in range(8)],
                             gate_results={"security": "PASS", "authority": "PASS", "evidence": "PASS"})
    db.commit()
    r = personal_client.post(f"/api/routes/{DS}/activate", json={
        "mode": "APPROVED", "rationale": "tested on the held-out suite", "expected_config_hash": h,
        "eval_run_ids": [run.json()["id"]]})
    assert r.status_code == 200, r.text
    view = r.json()["route"]
    assert view["status"] == "APPROVED" and view["eval_summary"]["state"] == "DIUJI"
    assert view["eval_summary"]["roles"]["feature-implementer"]["run_ids"] == [run.json()["id"]]
    assert view["active_decision"]["eval_run_ids"] == [run.json()["id"]]
    assert view["active_decision"]["rollback_target"]["action"] == "DISABLE_ROUTE"
    elig = personal_client.get("/api/routes/eligibility", params={"domain": "PERSONAL", "classification": "INTERNAL",
                                                                 "role": "feature-implementer"}).json()
    assert elig["eligible"][0]["route_key"] == DS and elig["eligible"][0]["basis"] == "EVAL"


def test_list_mode_stays_design_while_seeded_rows_exist(personal_client: AuthedClient, db):
    """A few catalog-discovered LIVE rows never relabel the seeded DESIGN registry as live runtime."""
    _seed(db)
    db.add(service.ModelRoute(route_key="openrouter/x/y:free", pool="ARENA", provider="openrouter", model_id="x/y:free",
                              mode="LIVE", origin="CATALOG_DISCOVERED", status="CANDIDATE"))
    db.commit()
    assert personal_client.get("/api/routes").json()["mode"] == "DESIGN"
    assert personal_client.get("/api/routes", params={"pool": "ARENA", "status": "CANDIDATE"}).json()["mode"] == "DESIGN"
