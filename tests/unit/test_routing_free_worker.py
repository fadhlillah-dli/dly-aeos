"""Owner decision C (ADR-0041 amendment 2026-10-26): a free OpenRouter route serves worker tasks for PUBLIC data only,
behind an explicit owner pool move and a new hash-bound activation; never INTERNAL/CONFIDENTIAL, never OFFICE, never
once it is not free; free first for PUBLIC tasks; the account's free-model limits make the task wait - never a
fallback to a route that is not free. respx only: no network, no real credentials, no real data directory."""

from __future__ import annotations

import copy
import importlib
from datetime import timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from aeos.api.routers import MODULE_ROUTERS
from aeos.broker import facade as broker
from aeos.core import config as config_mod
from aeos.core.errors import Blocked, PolicyDenied, ValidationFailed
from aeos.core.timeutil import utcnow
from aeos.models.audit import AuditEvent
from aeos.models.broker_ext import BrokerRequest
from aeos.models.cost import QuotaState, UsageLedgerEntry
from aeos.models.decisions import Decision
from aeos.models.routing import CapabilityGrant, ModelRoute
from aeos.models.workforce import WorkerRun
from aeos.routing import catalog, free_tier, seed, service
from aeos.routing.facade import eligible_routes, fallback_route, select_route
from aeos.workforce import integrations, scheduler
from tests.conftest import make_app
from tests.unit.test_routing_fakes import human_decision, install_fakes
from tests.unit.test_routing_service import GO_GLM, approve, task
from tests.unit.test_workforce_support import FakeModules, make_plan, make_project, make_route

# The real routers (and the services they import at module level) load before FakeModules replaces implementation
# modules in sys.modules (as in test_broker_model_proxy.py).
for _mod in MODULE_ROUTERS:
    importlib.import_module(_mod)

FREE = "openrouter/cohere/north-mini-code:free"  # one of the owner's G1 routes (catalog-discovered, ARENA)
FREE2 = "openrouter/google/gemma-4-31b-it:free"
CATALOG = {"data": [
    {"id": "cohere/north-mini-code:free", "name": "Cohere: North Mini Code (free)", "context_length": 131072,
     "pricing": {"prompt": "0", "completion": "0"}, "top_provider": {"is_moderated": False},
     "supported_parameters": ["max_tokens", "tools", "tool_choice"]},
    {"id": "google/gemma-4-31b-it:free", "name": "Google: Gemma 4 31B (free)", "context_length": 131072,
     "pricing": {"prompt": "0", "completion": "0"}, "top_provider": {"is_moderated": False},
     "supported_parameters": ["max_tokens", "tools"]},
]}
UPSTREAM = "https://openrouter.ai/api/v1/chat/completions"
KEY = "sk-or-v1-" + "f" * 48
FREE_PRICING = {"prompt": "0", "completion": "0", "basis": "OpenRouter catalog", "unit": "USD per token"}


@pytest.fixture
def routes(db):
    seed.run(db)
    db.commit()
    return db


def _discover(db, payload=None) -> dict:
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(catalog.OPENROUTER_MODELS_URL).mock(return_value=httpx.Response(200, json=payload or CATALOG))
        with httpx.Client() as c:
            out = catalog.refresh_openrouter_catalog(db, c)
    db.flush()
    return out


def _activate(db, key: str, **kw):
    r = service.get_route(db, key)
    return service.activate_route(db, key, actor_user_id="owner", mode="TRIAL", rationale="decision C: PUBLIC tasks",
                                  expected_config_hash=r.config_hash, terms_reviewed=True,
                                  terms_note="OpenRouter privacy: training-capable free endpoints enabled", **kw)


def _move(db, key: str, pool: str = "WORKER"):
    return service.set_route_pool(db, key, pool=pool, rationale="owner decision C 2026-10-26: free worker tier")


def _worker(db, key: str = FREE) -> ModelRoute:
    """The owner's flow: discovered in ARENA, moved into WORKER, activated as TRIAL (PUBLIC)."""
    _discover(db)
    _move(db, key)
    route, _ = _activate(db, key)
    return route


def _go(db) -> ModelRoute:
    """An activated OpenCode Go WORKER route (PR #19: SUBSCRIPTION, BUDGET decision, PUBLIC + INTERNAL)."""
    budget = human_decision(db, kind="BUDGET", target_type="model_budget", target_id="opencode-go",
                            auth_scope={"routes": [GO_GLM]})
    service.get_route(db, GO_GLM).region = "EU"
    return approve(db, GO_GLM, kind="TRIAL", details={"budget_decision_id": budget.id})


def _forwarded(db, n: int, *, at=None, model="cohere/north-mini-code:free") -> None:
    """*n* requests this instance already sent to an OpenRouter :free model."""
    for _ in range(n):
        db.add(BrokerRequest(grant_id="g", run_id="r", project_id="p", domain="PERSONAL", route_key=FREE,
                             provider="openrouter", upstream_model=model, outcome="OK", status_code=200,
                             created_at=at or utcnow()))
    db.flush()


# ------------------------------------------------------------------ eligibility (pool move, activation, hard filter)


def test_owner_moves_a_discovered_free_route_into_worker_and_must_reactivate(routes, monkeypatch):
    """The pool is part of the identity: the ARENA TRIAL (G1) never carries over, moving back never revives it, the
    WORKER configuration serves only after a new activation."""
    reg = install_fakes(monkeypatch)
    _discover(routes)
    arena_route, arena_trial = _activate(routes, FREE)
    assert arena_route.pool == "ARENA" and arena_route.status == "TRIAL"
    arena_hash = arena_route.config_hash
    route, change = _move(routes, FREE)
    assert route.pool == "WORKER" and route.config_hash != arena_hash and change["needs_new_trial"] is True
    assert routes.get(Decision, arena_trial.id).status == "INVALIDATED"
    assert any(i["invalidator"] == "model:route:ARENA" for i in reg.invalidations)  # the old pool's checks too
    d = eligible_routes(routes, domain="PERSONAL", data_classification="PUBLIC")
    assert FREE not in d.eligible and any("TRIAL without an ACTIVE TRIAL decision" in x for x in d.rejected[FREE])
    assert service.route_use_problem(routes, route) is not None
    _move(routes, FREE, "ARENA")  # back to the ARENA identity: the invalidated TRIAL stays invalid
    assert service.get_route(routes, FREE).config_hash == arena_hash
    assert service.route_use_problem(routes, service.get_route(routes, FREE)) is not None
    _move(routes, FREE)
    _activate(routes, FREE)
    assert eligible_routes(routes, domain="PERSONAL", data_classification="PUBLIC").eligible == [FREE]
    assert service.route_use_problem(routes, service.get_route(routes, FREE)) is None


@pytest.mark.parametrize(("edit", "needle"), [
    (lambda r: setattr(r, "model_id", "cohere/north-mini-code"), "does not end with ':free'"),
    (lambda r: setattr(r, "pricing", {"prompt": "0.0000001", "completion": "0"}), "not exactly zero"),
    (lambda r: setattr(r, "pricing", {"completion": "0"}), "'prompt' not observed"),
    (lambda r: setattr(r, "pricing", {**FREE_PRICING, "request": "0.001"}), "'request'"),
    (lambda r: setattr(r, "is_free", "NO"), "is_free=NO"),
    (lambda r: setattr(r, "free_until", utcnow() - timedelta(hours=1)), "free period ended"),
    (lambda r: setattr(r, "allowed_data_classes", ["PUBLIC", "INTERNAL"]), "exactly ['PUBLIC']"),
    (lambda r: setattr(r, "eligible_domains", ["PERSONAL", "OFFICE"]), "OFFICE default deny"),
])
def test_pool_move_into_worker_refuses_a_route_outside_decision_c(routes, monkeypatch, edit, needle):
    install_fakes(monkeypatch)
    _discover(routes)
    r = service.get_route(routes, FREE)
    edit(r)
    r.config_hash = service.compute_config_hash(r)
    with pytest.raises(ValidationFailed) as ei:
        _move(routes, FREE)
    assert ei.value.code == "FREE_WORKER_ROUTE_INELIGIBLE" and any(needle in p for p in ei.value.details["problems"])
    assert r.pool == "ARENA"


def test_only_openrouter_routes_move_and_only_between_arena_and_worker(routes, monkeypatch):
    install_fakes(monkeypatch)
    _discover(routes)
    for key, pool in (("opencode-zen/mimo-v2.6-flash-free", "WORKER"), (FREE, "LOCAL"), (FREE, "WATCHLIST"),
                      (GO_GLM, "ARENA")):
        with pytest.raises(ValidationFailed) as ei:
            service.set_route_pool(routes, key, pool=pool, rationale="trying to move another route")
        assert ei.value.code == "ROUTE_POOL_MOVE_UNSUPPORTED"
    with pytest.raises(ValidationFailed):
        service.set_route_pool(routes, FREE, pool="WORKER", rationale="short")


def test_activation_of_a_free_worker_route_refuses_internal_data(routes, monkeypatch):
    install_fakes(monkeypatch)
    _discover(routes)
    _move(routes, FREE)
    with pytest.raises(ValidationFailed) as ei:
        _activate(routes, FREE, allowed_data_classes=["PUBLIC", "INTERNAL"],
                  terms_record={"may_use_inputs_for_training": False, "trial_only_no_confidential": False,
                                "note": "owner reading that would otherwise lift the PUBLIC restriction"})
    assert ei.value.code == "FREE_WORKER_ROUTE_INELIGIBLE"
    assert service.get_route(routes, FREE).status == "CANDIDATE"  # nothing recorded


def test_internal_and_confidential_tasks_never_route_to_it_and_office_is_denied(routes, monkeypatch):
    install_fakes(monkeypatch)
    _worker(routes)
    _go(routes)
    assert select_route(routes, task=task(classification="PUBLIC")).route.route_key == FREE
    sel = select_route(routes, task=task(classification="INTERNAL"))
    assert sel.route.route_key == GO_GLM and any("not allowed" in x for x in sel.rejected[FREE])
    with pytest.raises(Blocked) as ei:
        select_route(routes, task=task(classification="CONFIDENTIAL"))
    assert ei.value.code == "NO_ELIGIBLE_ROUTE"
    d = eligible_routes(routes, domain="OFFICE", data_classification="PUBLIC")
    assert FREE not in d.eligible and any("OFFICE" in x for x in d.rejected[FREE])


def test_a_stored_edit_outside_decision_c_is_rejected_by_routing_and_per_request(routes, monkeypatch):
    """Fail closed on every call, even if the row is edited and the decision re-bound to the edited hash."""
    install_fakes(monkeypatch)
    r = _worker(routes)
    for edit, needle in ((lambda: setattr(r, "free_until", utcnow() - timedelta(minutes=1)), "free period ended"),
                         (lambda: setattr(r, "allowed_data_classes", ["PUBLIC", "INTERNAL"]), "exactly ['PUBLIC']"),
                         # e.g. a company OFFICE authorisation (policy office_routes): decision C stays PERSONAL-only
                         (lambda: setattr(r, "eligible_domains", ["OFFICE"]), "OFFICE default deny")):
        saved = (r.free_until, list(r.allowed_data_classes), list(r.eligible_domains))
        edit()
        approve(routes, FREE, kind="TRIAL")  # a decision bound to the edited configuration
        reasons = service.route_rejections(routes, r, domain="PERSONAL", data_classification="PUBLIC",
                                           needs_tools=True, min_context=None)
        assert any(needle in x for x in reasons)
        assert needle in (service.route_use_problem(routes, r) or "")
        r.free_until, r.allowed_data_classes, r.eligible_domains = saved
        approve(routes, FREE, kind="TRIAL")
        assert service.route_use_problem(routes, r) is None


# ------------------------------------------------------------------ catalog refresh


def test_catalog_refresh_keeps_the_worker_pool_and_stops_a_repriced_or_dropped_model(routes, monkeypatch):
    install_fakes(monkeypatch)
    _worker(routes)
    out = _discover(routes)  # unchanged catalog: same identity, still WORKER, still serving
    r = service.get_route(routes, FREE)
    assert r.pool == "WORKER" and FREE not in out["discovered"] and FREE not in out["config_changed"]
    assert service.route_use_problem(routes, r) is None
    paid = copy.deepcopy(CATALOG)
    paid["data"][0]["pricing"] = {"prompt": "0.0000002", "completion": "0.0000008"}
    out = _discover(routes, paid)
    r = service.get_route(routes, FREE)
    assert FREE in out["config_changed"] and r.pool == "WORKER" and r.is_free == "NO"
    assert service.route_use_problem(routes, r) is not None  # its TRIAL was bound to the free configuration
    reasons = service.route_rejections(routes, r, domain="PERSONAL", data_classification="PUBLIC", needs_tools=True,
                                       min_context=None)
    assert any("not exactly zero" in x for x in reasons) and any("TRIAL without" in x for x in reasons)
    with pytest.raises(Blocked):
        select_route(routes, task=task(classification="PUBLIC"))
    out = _discover(routes, {"data": CATALOG["data"][1:]})
    assert FREE in out["missing"] and service.get_route(routes, FREE).availability == "MISSING"
    assert service.get_route(routes, FREE).pool == "WORKER"


# ------------------------------------------------------------------ ranking and the no-paid-fallback rule


def test_free_first_for_public_even_against_an_evaluated_go_route(routes, monkeypatch):
    install_fakes(monkeypatch)
    _worker(routes)
    go = _go(routes)
    go.eval_results = {"feature-implementer": {"score": 0.99, "config_hash": go.config_hash}}
    sel = select_route(routes, task=task(classification="PUBLIC"))
    assert sel.route.route_key == FREE and sel.eligible == [FREE, GO_GLM] and "free first" in sel.reason
    assert select_route(routes, task=task(classification="INTERNAL")).route.route_key == GO_GLM


def test_rate_limited_free_route_makes_the_public_task_wait_never_a_go_fallback(routes, monkeypatch):
    install_fakes(monkeypatch)
    _worker(routes)
    _go(routes)
    _forwarded(routes, 50)  # the default daily cap (50) used up this UTC day
    with pytest.raises(Blocked) as ei:
        select_route(routes, task=task(classification="PUBLIC"))
    assert ei.value.code == "FREE_TIER_LIMIT_REACHED" and ei.value.details["would_serve"] == GO_GLM
    route, reasons, code = integrations.select_route_detail(routes, task(classification="PUBLIC"))
    assert route is None and code == "FREE_TIER_LIMIT_REACHED" and code in scheduler.QUOTA_CODES
    assert code in scheduler.WAIT_CODES
    assert "daily cap" in (scheduler.route_quota_exhausted(routes, FREE) or "")
    # INTERNAL never could use the free route: Go serves it as before
    assert select_route(routes, task=task(classification="INTERNAL")).route.route_key == GO_GLM
    # the fallback rule is unchanged: along the chain, free only - a Go route is never a fallback
    service.get_route(routes, FREE).fallback_chain = [GO_GLM]
    approve(routes, FREE, kind="TRIAL")
    with pytest.raises(Blocked) as ei:
        fallback_route(routes, task=task(classification="PUBLIC"), failed_route_key=FREE)
    assert ei.value.code == "NO_ELIGIBLE_FALLBACK"
    assert "paid fallback is never automatic" in str(ei.value.details["rejected"][GO_GLM])


def test_an_observed_429_excludes_every_free_route_account_wide_and_waits(routes, monkeypatch):
    install_fakes(monkeypatch)
    _worker(routes)
    _move(routes, FREE2)
    _activate(routes, FREE2)
    _go(routes)
    free_tier.note_rate_limited(routes, route_key=FREE, retry_after_seconds=30)
    row = routes.scalar(select(QuotaState).where(QuotaState.provider == "openrouter", QuotaState.scope == "free-rpm"))
    assert row.state == "EXHAUSTED" and row.details["reset_basis"] == "retry-after"
    d = eligible_routes(routes, domain="PERSONAL", data_classification="PUBLIC")
    assert d.eligible == [GO_GLM] and all(any("quota EXHAUSTED" in x for x in d.rejected[k]) for k in (FREE, FREE2))
    with pytest.raises(Blocked) as ei:
        select_route(routes, task=task(classification="PUBLIC"))
    assert ei.value.code == "FREE_TIER_LIMIT_REACHED" and set(ei.value.details["limited"]) == {FREE, FREE2}
    row.resets_at = utcnow() - timedelta(seconds=1)
    routes.flush()
    assert select_route(routes, task=task(classification="PUBLIC")).route.route_key in {FREE, FREE2}


def test_429_without_retry_after_waits_for_the_day_only_when_the_daily_count_is_used_up(routes):
    _forwarded(routes, 3)
    out = free_tier.note_rate_limited(routes, route_key=FREE, retry_after_seconds=None)
    assert out["scope"] == "free-rpm" and out["basis"] == "per-minute-window"
    _forwarded(routes, 47)
    out = free_tier.note_rate_limited(routes, route_key=FREE, retry_after_seconds=None)
    assert out["scope"] == "free-daily" and out["resets_at"] == free_tier.next_utc_midnight(utcnow()).isoformat()


# ------------------------------------------------------------------ limits are owner configuration


def test_daily_cap_is_the_owner_setting_lowered_by_the_key_endpoint(routes, monkeypatch):
    _forwarded(routes, 50, at=utcnow() - timedelta(seconds=61))  # this UTC day, outside the minute window
    assert free_tier.limit_problem(routes)["kind"] == "DAILY"  # default 50/day
    monkeypatch.setenv("AEOS_OPENROUTER_FREE_DAILY_LIMIT", "1000")  # after the owner bought >= 10 credits
    config_mod.reset_settings_cache()
    assert free_tier.limits(routes)["daily"] == 1000 and free_tier.limit_problem(routes) is None
    routes.add(QuotaState(provider="openrouter", scope="credits", state="OK", source="official-key-endpoint",
                          details={"free_model_daily_requests": {"used": 50, "limit": 50, "remaining": 0}}))
    routes.flush()
    lim = free_tier.limits(routes)
    assert lim["daily"] == 50 and lim["daily_basis"] == "KEY_ENDPOINT"  # the account is not upgraded yet
    for name, bad in (("AEOS_OPENROUTER_FREE_DAILY_LIMIT", "1001"), ("AEOS_OPENROUTER_FREE_RPM_LIMIT", "21"),
                      ("AEOS_OPENROUTER_FREE_DAILY_LIMIT", "0")):
        monkeypatch.setenv(name, bad)
        with pytest.raises(ValueError):
            config_mod.Settings()
        monkeypatch.setenv(name, "20" if "RPM" in name else "1000")


def test_rpm_counts_only_requests_that_reached_openrouter_in_the_last_minute(routes):
    _forwarded(routes, 19)
    _forwarded(routes, 5, at=utcnow() - timedelta(minutes=5))  # outside the minute window
    routes.add(BrokerRequest(grant_id="g", run_id="r", project_id="p", domain="PERSONAL", route_key=FREE,
                             outcome="DENIED"))  # refused by the broker itself: never sent
    _forwarded(routes, 3, model="opencode-go-model")  # not a :free model
    routes.flush()
    assert free_tier.limit_problem(routes) is None
    _forwarded(routes, 1)
    p = free_tier.limit_problem(routes)
    assert p["kind"] == "RPM" and 1 <= p["retry_after_seconds"] <= 60


@respx.mock
def test_key_endpoint_reports_the_daily_free_requests_used_up(routes, secrets_memory):
    secrets_memory.put("workers/openrouter", KEY, actor="test")
    info = {"data": {"limit": None, "usage": 0.0, "is_free_tier": False,
                     "free_model_daily_requests": {"used": 1000, "limit": 1000, "remaining": 0}}}
    respx.get(catalog.OPENROUTER_KEY_URL).mock(return_value=httpx.Response(200, json=info))
    with httpx.Client() as c:
        catalog.refresh_openrouter_key_info(routes, c)
    daily = routes.scalar(select(QuotaState).where(QuotaState.provider == "openrouter",
                                                   QuotaState.scope == "free-daily"))
    assert daily.state == "EXHAUSTED" and daily.source == "official-key-endpoint"
    assert free_tier.limit_problem(routes)["kind"] == "OBSERVED"


# ------------------------------------------------------------------ broker: per request, 429, ledger


@pytest.fixture
def proxy(settings, db, secrets_memory, monkeypatch):
    """The real routing rule behind the broker, a decision C route and a PUBLIC task's run."""
    fk = FakeModules().install(monkeypatch, skip=("aeos.routing.service",))
    secrets_memory.put("workers/openrouter", KEY, actor="test")
    p = make_project(db)
    route = make_route(db, key=FREE, model_id="cohere/north-mini-code:free", status="TRIAL",
                       pricing=dict(FREE_PRICING), data_classes=["PUBLIC"])
    _plan, _pv, specs = make_plan(db, p, [{"task_key": "pub", "data_classification": "PUBLIC"},
                                          {"task_key": "int", "data_classification": "INTERNAL"}])
    out = {"route": route, "db": db, "fakes": fk, "project": p}
    for spec in specs:
        run = WorkerRun(project_id=p.id, domain="PERSONAL", execution_status="RUNNING", model_route_key=FREE,
                        task_id=spec.id)
        db.add(run)
        db.flush()
        raw, grant = broker.issue_grant(db, run_id=run.id, project_id=p.id, domain="PERSONAL", kind="MODEL_PROXY",
                                        scope={"models": {"free": FREE}}, ttl_seconds=600, max_requests=50,
                                        max_cost_usd=0.0)
        out[spec.data_classification] = {"token": raw, "grant": grant, "run": run}
    db.commit()
    with TestClient(make_app(settings), base_url="http://testserver") as c:
        out["client"] = c
        yield out


def _call(proxy, cls="PUBLIC"):
    return proxy["client"].post("/broker/v1/chat/completions",
                                json={"model": "free", "messages": [{"role": "user", "content": "hi"}]},
                                headers={"Authorization": f"Bearer {proxy[cls]['token']}"})


def _ok(cost=0):
    return {"model": "cohere/north-mini-code:free", "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3, "cost": cost}}


def test_broker_refuses_an_internal_task_and_records_a_free_call_at_zero(proxy, db):
    with respx.mock(assert_all_mocked=True) as mock:
        up = mock.post(UPSTREAM).mock(return_value=httpx.Response(200, json=_ok()))
        r = _call(proxy, "INTERNAL")
        assert r.status_code == 403 and r.json()["error"]["code"] == "FREE_WORKER_ROUTE_INELIGIBLE"
        assert "PUBLIC data only" in r.json()["error"]["message"] and up.call_count == 0
        assert _call(proxy).status_code == 200 and up.call_count == 1
    db.expire_all()
    entry = db.scalar(select(UsageLedgerEntry).where(UsageLedgerEntry.route_key == FREE))
    assert float(entry.cost_usd) == 0.0 and entry.input_tokens == 11 and entry.output_tokens == 3
    assert db.get(CapabilityGrant, proxy["INTERNAL"]["grant"].id).used_requests == 0  # refused before counting


def test_broker_stops_a_repriced_model_after_the_first_reported_charge(proxy, db):
    """Between catalog refreshes: the run's $0 cost cap stops the run, and the route stops serving any run until the
    owner re-checks the catalog and re-activates it - a repriced model is not billed again."""
    with respx.mock(assert_all_mocked=True) as mock:
        up = mock.post(UPSTREAM).mock(return_value=httpx.Response(200, json=_ok(cost=0.0004)))
        assert _call(proxy).status_code == 200
        r = _call(proxy)
        assert r.status_code == 403 and r.json()["error"]["code"] == "CAP_EXCEEDED" and up.call_count == 1
        p = proxy["project"]
        run = WorkerRun(project_id=p.id, domain="PERSONAL", execution_status="RUNNING", model_route_key=FREE,
                        task_id=proxy["PUBLIC"]["run"].task_id)
        db.add(run)
        db.flush()
        raw, _grant = broker.issue_grant(db, run_id=run.id, project_id=p.id, domain="PERSONAL", kind="MODEL_PROXY",
                                         scope={"models": {"free": FREE}}, ttl_seconds=600, max_requests=5,
                                         max_cost_usd=0.0)
        db.commit()
        proxy["fresh"] = {"token": raw}
        r = _call(proxy, "fresh")
        assert r.status_code == 403 and r.json()["error"]["code"] == "FREE_WORKER_ROUTE_INELIGIBLE"
        assert "repriced" in r.json()["error"]["message"] and up.call_count == 1


def test_broker_429_from_openrouter_is_recorded_not_success_and_never_falls_back(proxy, db):
    with respx.mock(assert_all_mocked=True) as mock:
        up = mock.post(UPSTREAM).mock(return_value=httpx.Response(
            429, headers={"x-ratelimit-limit": "20", "x-ratelimit-remaining": "0"},
            json={"error": {"code": 429, "message": "Rate limit exceeded",
                            "metadata": {"error_type": "rate_limit_exceeded"}}}))
        r = _call(proxy)
        assert r.status_code == 429 and r.json()["error"]["code"] == "UPSTREAM_RATE_LIMITED"
        assert up.call_count == 1 and len(mock.calls) == 1  # no other route, no other model
        r = _call(proxy)  # the account-wide state now refuses before any upstream call
        assert r.status_code == 429 and r.json()["error"]["code"] == "FREE_TIER_LIMIT_REACHED"
        assert int(r.headers["retry-after"]) >= 1 and up.call_count == 1
    db.expire_all()
    req = db.scalar(select(BrokerRequest).where(BrokerRequest.status_code == 429, BrokerRequest.upstream_model.is_not(None)))
    assert req.outcome == "RATE_LIMITED"
    assert db.scalar(select(UsageLedgerEntry).where(UsageLedgerEntry.route_key == FREE)) is None  # not a success
    audit = db.scalar(select(AuditEvent).where(AuditEvent.action == "broker.model_proxy.call"))
    assert audit.result == "FAILED"
    row = db.scalar(select(QuotaState).where(QuotaState.provider == "openrouter", QuotaState.scope == "free-rpm"))
    assert row.state == "EXHAUSTED" and row.details["rate_limit_headers"]["x-ratelimit-limit"] == "20"


def test_broker_enforces_rpm_and_daily_caps_before_any_upstream_call(proxy, db, monkeypatch):
    _forwarded(db, 20)
    db.commit()
    with respx.mock(assert_all_mocked=True) as mock:
        up = mock.post(UPSTREAM).mock(return_value=httpx.Response(200, json=_ok()))
        r = _call(proxy)
        assert r.status_code == 429 and r.json()["error"]["code"] == "FREE_TIER_LIMIT_REACHED"
        assert 1 <= int(r.headers["retry-after"]) <= 60 and up.call_count == 0
        db.query(BrokerRequest).filter(BrokerRequest.upstream_model.is_not(None)).update(
            {BrokerRequest.created_at: utcnow() - timedelta(minutes=2)})  # out of the minute, still this UTC day
        _forwarded(db, 30, at=utcnow() - timedelta(minutes=2))
        db.commit()
        r = _call(proxy)  # 50 today: the default daily cap
        assert r.status_code == 429 and "daily cap" in r.json()["error"]["message"] and up.call_count == 0
        monkeypatch.setenv("AEOS_OPENROUTER_FREE_DAILY_LIMIT", "1000")
        config_mod.reset_settings_cache()
        assert _call(proxy).status_code == 200 and up.call_count == 1
    db.expire_all()
    run = db.get(WorkerRun, proxy["PUBLIC"]["run"].id)
    assert run.quota_status == "RATE_LIMITED"  # shown as WAITING_QUOTA while the limit held
    local = db.scalars(select(BrokerRequest).where(BrokerRequest.outcome == "RATE_LIMITED",
                                                   BrokerRequest.upstream_model.is_(None))).all()
    assert len(local) == 2  # recorded for the executor's quota stop, never counted as sent to OpenRouter
    assert db.get(CapabilityGrant, proxy["PUBLIC"]["grant"].id).used_requests == 1


def test_probe_respects_the_free_model_limit(proxy, db):
    _forwarded(db, 20)
    db.commit()
    with respx.mock(assert_all_mocked=True) as mock:
        with pytest.raises(PolicyDenied) as ei:
            broker.probe_route(db, route_key=FREE, actor="user:owner")
        assert ei.value.code == "FREE_TIER_LIMIT_REACHED" and not mock.calls


def test_route_view_and_api_pool_move(admin_client, db):
    seed.run(db)
    _discover(db)
    db.commit()
    r = admin_client.post(f"/api/routes/{FREE}/pool", json={"pool": "WORKER", "rationale": "owner decision C move"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["route"]["pool"] == "WORKER" and body["needs_new_trial"] is False
    view = body["route"]["free_tier"]
    assert view["rpm_limit"] == 20 and view["daily_limit"] == 50 and view["decision_c"]["status"] == "ELIGIBLE_IF_ACTIVATED"
    assert admin_client.post(f"/api/routes/{GO_GLM}/pool",
                             json={"pool": "ARENA", "rationale": "not an OpenRouter route"}).status_code == 422
    assert db.query(AuditEvent).filter_by(action="route.set_pool", target_id=FREE).count() == 1


def test_cli_set_pool_needs_the_admin_password_and_prints_ascii(routes, monkeypatch):
    from typer.testing import CliRunner

    from aeos.cli import app
    from tests.conftest import ADMIN_PW, create_admin

    install_fakes(monkeypatch)
    create_admin(routes)
    _discover(routes)
    routes.commit()
    runner = CliRunner()
    base = ["routes", "set-pool", FREE, "--pool", "WORKER", "--as", "owner", "--rationale", "owner decision C move"]
    wrong = runner.invoke(app, base, input="nope\n")
    assert wrong.exit_code != 0 and "password check failed" in wrong.output
    ok = runner.invoke(app, base, input=ADMIN_PW + "\n")
    assert ok.exit_code == 0, ok.output
    assert "pool=WORKER (was ARENA)" in ok.output and ok.output.isascii() and ADMIN_PW not in ok.output
    listed = runner.invoke(app, ["routes", "list", "--pool", "WORKER"])
    assert "decision-c=PUBLIC-ONLY" in listed.output.split(FREE)[1].splitlines()[0]
    bad = runner.invoke(app, ["routes", "set-pool", GO_GLM, "--pool", "ARENA", "--as", "owner", "--rationale",
                              "not an OpenRouter route"], input=ADMIN_PW + "\n")
    assert bad.exit_code == 1 and "ROUTE_POOL_MOVE_UNSUPPORTED" in bad.output
    routes.expire_all()
    assert routes.query(AuditEvent).filter_by(action="route.set_pool", target_id=FREE).count() == 1
