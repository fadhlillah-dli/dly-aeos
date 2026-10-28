"""Owner decision C (ADR-0041 amendment 2026-10-26): a free OpenRouter route serves worker tasks for PUBLIC data only,
behind an explicit owner pool move and a new hash-bound activation; never INTERNAL/CONFIDENTIAL, never OFFICE, never
once it is not free; the account's free-model limits make the task wait - never a fallback to a route that is not $0
marginal. Decision C' (ADR-0124, owner 2026-10-28): for PUBLIC tasks the OpenCode Go routes (subscription, $0 marginal
within the plan) rank first and the free routes are the fallback. respx only: no network, no real credentials, no real
data directory."""

from __future__ import annotations

import copy
import importlib
import threading
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select

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

# routing keeps a route whose worker key is not stored out of eligibility: these tests store fake keys
pytestmark = pytest.mark.usefixtures("worker_keys")

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
OVERFLOW_DATE = "Mon, 01 Jan 2026 99999999999999999999:00:00 GMT"  # an HTTP date no datetime can hold


@pytest.fixture(autouse=True)
def _midday_utc(monkeypatch):
    """The free-model limits count per UTC day: every test runs at 12:00 UTC of the current date (the clock still
    advances in real time), so 'now - a few minutes' is always the same UTC day, whatever the wall clock says. Every
    AEOS timestamp comes from ``timeutil.utcnow``, which reads this clock."""
    from aeos.core import timeutil

    real = timeutil.datetime
    started = real.now(UTC)
    noon = started.replace(hour=12, minute=0, second=0, microsecond=0)

    class _MiddayClock(real):  # type: ignore[valid-type,misc]
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            at = noon + (real.now(UTC) - started)
            return at if tz is None or tz is UTC else at.astimezone(tz)

    monkeypatch.setattr(timeutil, "datetime", _MiddayClock)
    assert utcnow().hour in (12, 13)


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


def test_only_openrouter_free_routes_move_and_only_between_arena_and_worker(routes, monkeypatch):
    install_fakes(monkeypatch)
    _discover(routes)
    paid = ModelRoute(route_key="openrouter/vendor/pro", pool="ARENA", provider="openrouter", model_id="vendor/pro",
                      is_free="NO", eligible_domains=["PERSONAL"], allowed_data_classes=["PUBLIC"])
    routes.add(paid)
    routes.flush()
    for key, pool in (("opencode-zen/mimo-v2.6-flash-free", "WORKER"), (FREE, "LOCAL"), (FREE, "WATCHLIST"),
                      (GO_GLM, "ARENA"), (paid.route_key, "WORKER")):
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
    assert select_route(routes, task=task(classification="PUBLIC")).route.route_key == GO_GLM  # decision C': Go first
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


def test_a_non_free_openrouter_worker_route_keeps_the_existing_rules(routes, monkeypatch):
    """Decision C governs OpenRouter ':free' models only: a paid OpenRouter WORKER route is judged by the existing rules
    (BUDGET decision, data-class and terms rules) - no PUBLIC-only rule, no free-tier limit, no free-first rank."""
    install_fakes(monkeypatch)
    key = "openrouter/vendor/pro"
    paid = ModelRoute(route_key=key, pool="WORKER", provider="openrouter", model_id="vendor/pro",
                      endpoint="https://openrouter.ai/api/v1", is_free="NO",
                      pricing={"prompt": "0.000001", "completion": "0.000002", "basis": "OpenRouter catalog"},
                      eligible_domains=["PERSONAL"], allowed_data_classes=["PUBLIC", "INTERNAL"], supports_tools="YES",
                      context_window=131072, harness="opencode", tool_set=list(service.REVIEWED_WORKER_TOOLS),
                      data_terms={"may_use_inputs_for_training": False, "trial_only_no_confidential": False},
                      status="CANDIDATE", mode="LIVE")
    routes.add(paid)
    routes.flush()
    assert not free_tier.is_decision_c_route(paid) and free_tier.decision_c_problems(paid)  # it is not a free route
    approve(routes, key, kind="TRIAL")
    d = eligible_routes(routes, domain="PERSONAL", data_classification="INTERNAL")
    assert any("paid route" in x for x in d.rejected[key]) and not any("decision C" in x for x in d.rejected[key])
    budget = human_decision(routes, kind="BUDGET", target_type="model_route", target_id=key)
    approve(routes, key, kind="TRIAL", details={"budget_decision_id": budget.id})
    _forwarded(routes, 60)  # the account's :free count is used up: not this route's limit
    assert eligible_routes(routes, domain="PERSONAL", data_classification="INTERNAL").eligible == [key]
    assert select_route(routes, task=task(classification="INTERNAL")).route.route_key == key
    assert service.route_use_problem(routes, paid) is None
    assert service.free_worker_problem(routes, paid, run_id="no-such-run") is None  # no PUBLIC-only TaskSpec rule
    assert free_tier.route_limit_problem(routes, paid) is None and not service.free_first(paid)
    paid.eligible_domains = ["OFFICE"]  # the company OFFICE path decides, not decision C
    reasons = service.route_rejections(routes, paid, domain="OFFICE", data_classification="INTERNAL",
                                       needs_tools=True, min_context=None)
    assert any("OFFICE denied" in x for x in reasons) and not any("decision C" in x for x in reasons)


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


def test_go_first_for_public_and_the_free_route_is_the_fallback(routes, monkeypatch):
    """Decision C' (owner 2026-10-28): PUBLIC tasks use OpenCode Go first, even against an evaluated free route; the free
    route serves when PUBLIC's share of the Go window is used up (go_public_share); INTERNAL is unaffected."""
    install_fakes(monkeypatch)
    free = _worker(routes)
    _go(routes)
    free.eval_results = {"feature-implementer": {"score": 0.99, "config_hash": free.config_hash}}
    sel = select_route(routes, task=task(classification="PUBLIC"))
    assert sel.route.route_key == GO_GLM and sel.eligible == [GO_GLM, FREE] and "Go first" in sel.reason
    assert select_route(routes, task=task(classification="INTERNAL")).route.route_key == GO_GLM
    monkeypatch.setenv("AEOS_GO_PUBLIC_SHARE", "0")
    config_mod.reset_settings_cache()
    sel = select_route(routes, task=task(classification="PUBLIC"))
    assert sel.route.route_key == FREE and "free (decision C)" in sel.reason
    assert any(x.startswith("PUBLIC_SHARE:") for x in sel.rejected[GO_GLM])
    assert select_route(routes, task=task(classification="INTERNAL")).route.route_key == GO_GLM


def test_rate_limited_free_route_makes_the_public_task_wait_never_a_paid_fallback(routes, monkeypatch):
    install_fakes(monkeypatch)
    _worker(routes)
    _go(routes)
    _forwarded(routes, 50)  # the default daily cap (50) used up this UTC day
    # decision C': Go ranks first for PUBLIC, so a free route at its limit does not matter while Go can take the task
    assert select_route(routes, task=task(classification="PUBLIC")).route.route_key == GO_GLM
    monkeypatch.setenv("AEOS_GO_PUBLIC_SHARE", "0")  # PUBLIC's share of the Go window used up: the free route is next
    config_mod.reset_settings_cache()
    with pytest.raises(Blocked) as ei:
        select_route(routes, task=task(classification="PUBLIC"))
    assert ei.value.code == "FREE_TIER_LIMIT_REACHED" and ei.value.details["limited"] == [FREE]
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
    assert select_route(routes, task=task(classification="PUBLIC")).route.route_key == GO_GLM  # decision C': Go first
    monkeypatch.setenv("AEOS_GO_PUBLIC_SHARE", "0")  # Go out for PUBLIC: both free routes are at the account's limit
    config_mod.reset_settings_cache()
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
    # a PUBLIC project: routing applies max(task class, project class), and the compiler never puts a PUBLIC task into
    # an INTERNAL project (ADR-0133); the INTERNAL task below is still refused
    p = make_project(db, classification="PUBLIC")
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


# ------------------------------------------------------------------ review fixes (2026-10-27)


def test_a_quota_stop_never_uses_up_an_attempt_and_the_task_returns_after_the_reset(db, monkeypatch):
    """A run the executor stops at a provider quota gives its attempt back; the task waits for the quota and is READY
    again after the reset. A bound on quota stops (per owner-granted round) keeps a task that never fits from looping."""
    from aeos.models.planning import TaskSpec
    from aeos.worker_runtime import executor
    from tests.unit.test_workforce_support import (
        make_owner,
        process_sandbox_exception,
        seed_and_enable_implementer,
    )

    fakes = FakeModules().install(monkeypatch)
    project = make_project(db)
    plan, pv, (spec,) = make_plan(db, project, [{"task_key": "a"}])
    fakes.approved[plan.id] = pv.id
    make_route(db)
    fakes.route_key = "worker.test-free"
    seed_and_enable_implementer(db)
    process_sandbox_exception(db)
    scheduler.ensure_executions(db, pv.id, actor="test", requested_by="owner")
    scheduler.refresh_readiness(db, pv.id)
    monkeypatch.setattr(scheduler, "QUOTA_STOPS_MAX", 3)

    def quota_stop() -> None:
        run = scheduler.acquire_lease(db, spec.id, actor="t")
        assert scheduler.get_te(db, spec.id).attempts_used == 1
        scheduler.transition_task(db, spec.id, "RUNNING", reason="bound", actor="t")
        te = scheduler.get_te(db, spec.id)
        out = executor._decide(db, run, db.get(TaskSpec, spec.id), te, exit_code=None, stop_reason="QUOTA_EXHAUSTED",
                               violations=[], protected_hits=[])
        assert out == "STOPPED"
        db.add(QuotaState(provider="openrouter", scope="worker.test-free", state="EXHAUSTED", source="observed-429",
                          resets_at=utcnow() + timedelta(minutes=5)))  # what the broker recorded on the 429s
        db.flush()

    for _ in range(2):
        quota_stop()
        te = scheduler.get_te(db, spec.id)
        assert te.state == "BLOCKED" and te.reason_code == "QUOTA_EXHAUSTED" and te.attempts_used == 0
        scheduler.refresh_readiness(db, pv.id)
        assert scheduler.get_te(db, spec.id).state == "BLOCKED"  # waits while the quota holds
        db.query(QuotaState).filter_by(scope="worker.test-free").delete()  # the provider reset
        scheduler.refresh_readiness(db, pv.id)
        te = scheduler.get_te(db, spec.id)
        assert te.state == "READY" and te.attempts_used == 0
    quota_stop()  # the third quota stop reaches the bound (3): an owner decision, never an endless loop
    db.query(QuotaState).filter_by(scope="worker.test-free").delete()
    scheduler.refresh_readiness(db, pv.id)
    te = scheduler.get_te(db, spec.id)
    assert te.state == "BLOCKED" and te.reason_code == "NEEDS_DECISION" and "provider quota" in te.reason
    with pytest.raises(Blocked):
        scheduler.retry_task(db, spec.id, actor="user:o")
    owner = make_owner(db)
    te = scheduler.retry_task(db, spec.id, actor="user:o", extra_attempts=3, rationale="the quota has reset",
                              actor_user_id=owner)
    # the decision resets the quota-stop count; it never raises the normal attempt cap
    assert te.state == "READY" and te.attempts_used == 0 and te.extra_attempts == 0
    assert scheduler.max_attempts(db.get(TaskSpec, spec.id), te) == 2
    for _ in range(2):  # exactly QUOTA_STOPS_MAX (3 here) more per owner decision, not 3 x (1 + extra attempts)
        quota_stop()
        db.query(QuotaState).filter_by(scope="worker.test-free").delete()
        scheduler.refresh_readiness(db, pv.id)
        assert scheduler.get_te(db, spec.id).state == "READY"
    quota_stop()
    db.query(QuotaState).filter_by(scope="worker.test-free").delete()
    scheduler.refresh_readiness(db, pv.id)
    te = scheduler.get_te(db, spec.id)
    assert te.state == "BLOCKED" and te.reason_code == "NEEDS_DECISION" and "since the last owner retry" in te.reason


def test_the_executor_stops_a_run_only_on_consecutive_rate_limits(db):
    from aeos.worker_runtime import executor

    def req(outcome: str, seconds: int) -> None:
        db.add(BrokerRequest(grant_id="g", run_id="r1", project_id="p", domain="PERSONAL", outcome=outcome,
                             created_at=utcnow() - timedelta(seconds=100 - seconds)))
        db.flush()

    for i, outcome in enumerate(("RATE_LIMITED", "RATE_LIMITED", "OK", "RATE_LIMITED", "RATE_LIMITED")):
        req(outcome, i)
    assert executor._rate_limited_count(db, "r1") == 2  # the answered request in between reset the run of refusals
    req("RATE_LIMITED", 10)
    assert executor._rate_limited_count(db, "r1") == 3 == executor.QUOTA_STOP_AFTER


def test_catalog_clears_free_until_when_the_free_model_has_no_end_date_any_more(routes, monkeypatch):
    install_fakes(monkeypatch)
    dated = copy.deepcopy(CATALOG)
    dated["data"][0]["expiration_date"] = "2026-12-31"
    _discover(routes, dated)
    r = service.get_route(routes, FREE)
    assert r.free_until is not None and "2026-12-31" in r.expires_note
    garbled = copy.deepcopy(CATALOG)
    garbled["data"][0]["expiration_date"] = "soon"
    _discover(routes, garbled)  # an end date the catalog states but AEOS cannot read is kept (fail closed)
    assert service.get_route(routes, FREE).free_until is not None
    out = _discover(routes)  # listed free, no expiration_date
    r = service.get_route(routes, FREE)
    assert r.free_until is None and "no expiration_date" in r.expires_note and FREE in out["config_changed"]


def test_every_probe_that_reached_openrouter_counts_against_the_free_limit(proxy, db):
    with respx.mock(assert_all_mocked=True) as mock:
        up = mock.post(UPSTREAM)
        up.side_effect = [httpx.Response(503, json={"error": "upstream down"}),
                          httpx.ReadTimeout("no answer"), httpx.ConnectError("refused")]
        for expected in ("UPSTREAM_ERROR", "TIMEOUT", "UNREACHABLE"):
            assert broker.probe_route(db, route_key=FREE, actor="user:owner")["outcome"] == expected
            db.commit()  # what the API and the CLI do after every probe
    assert free_tier.usage(db)["day"] == 2  # the 503 and the read timeout were sent; the refused connection was not
    rows = db.scalars(select(BrokerRequest).where(BrokerRequest.exposed_model == "feasibility-probe")
                      .order_by(BrokerRequest.created_at)).all()
    assert [r.outcome for r in rows] == ["UPSTREAM_ERROR", "TIMEOUT", "NOT_SENT"]
    marks = [e.details.get("upstream") for e in db.scalars(select(AuditEvent).where(
        AuditEvent.action == "broker.route_feasibility.probe").order_by(AuditEvent.seq))]
    assert marks == ["SENT", "SENT", "NOT_SENT"]


def test_a_429_without_retry_after_is_classified_from_the_rate_limit_headers(routes):
    from aeos.broker import model_proxy

    now = utcnow()
    reset = int((now + timedelta(hours=3)).timestamp() * 1000)  # epoch milliseconds
    out = free_tier.note_rate_limited(routes, route_key=FREE, retry_after_seconds=None,
                                      headers={"X-RateLimit-Limit": "1000", "X-RateLimit-Remaining": "0",
                                               "X-RateLimit-Reset": str(reset)})
    assert out["scope"] == "free-daily" and out["basis"] == "x-ratelimit-reset"
    assert abs(datetime.fromisoformat(out["resets_at"]) - (now + timedelta(hours=3))) < timedelta(seconds=2)
    out = free_tier.note_rate_limited(routes, route_key=FREE, retry_after_seconds=None,
                                      headers={"x-ratelimit-limit": "20", "x-ratelimit-remaining": "0"})
    assert out["scope"] == "free-rpm" and out["basis"] == "per-minute-window"
    routes.query(QuotaState).delete()
    out = free_tier.note_rate_limited(routes, route_key=FREE, retry_after_seconds=None,
                                      headers={"x-ratelimit-limit": "50", "x-ratelimit-remaining": "0"})
    assert out["scope"] == "free-daily" and out["basis"] == "daily-limit-header"  # the header names the daily limit
    assert out["resets_at"] == free_tier.next_utc_midnight(now).isoformat()
    routes.query(QuotaState).delete()
    out = free_tier.note_rate_limited(routes, route_key=FREE, retry_after_seconds=7200,
                                      headers={"x-ratelimit-limit": "20"})
    assert out["scope"] == "free-rpm"  # the header, not the length of the wait, names the limit
    seconds = int((now + timedelta(seconds=30)).timestamp())  # epoch seconds
    assert free_tier._header_reset(str(seconds), now) is not None
    assert free_tier._header_reset(str(seconds + 10 * 24 * 3600), now) is None  # beyond 48 h: not trusted
    http_date = format_datetime(now + timedelta(seconds=90), usegmt=True)
    assert 85 <= (model_proxy.parse_retry_after(http_date) or 0) <= 90


def test_a_429_never_shortens_a_longer_or_indefinite_block(routes):
    long_reset = utcnow() + timedelta(minutes=10)
    routes.add(QuotaState(provider="openrouter", scope="free-rpm", state="EXHAUSTED", source="observed-429",
                          resets_at=long_reset))
    routes.flush()
    out = free_tier.note_rate_limited(routes, route_key=FREE, retry_after_seconds=30)
    row = routes.scalar(select(QuotaState).where(QuotaState.provider == "openrouter", QuotaState.scope == "free-rpm"))
    assert out["kept"] is True and abs(row.resets_at - long_reset) < timedelta(seconds=1)
    row.resets_at = None  # an indefinite block
    routes.flush()
    out = free_tier.note_rate_limited(routes, route_key=FREE, retry_after_seconds=30)
    assert out["kept"] is True and routes.scalar(select(QuotaState).where(QuotaState.scope == "free-rpm")).resets_at is None
    row.resets_at = utcnow() - timedelta(seconds=1)  # an expired block is replaced
    routes.flush()
    assert free_tier.note_rate_limited(routes, route_key=FREE, retry_after_seconds=30)["recorded"] is True


def test_retry_after_parsing_is_shared_and_never_raises():
    from aeos.broker import model_proxy

    for raw, expected in (("120", 120), ("inf", None), ("-inf", None), ("nan", None), ("soon", None), ("", None),
                          (None, None), ("1e30", 86400), ("0", 1), ("-30", None), (OVERFLOW_DATE, None),
                          ("Mon, 01 Jan 99999999 10:00:00 GMT", None), ("Mon, 01 Jan 2026 10:00:00 +99999999", None)):
        assert model_proxy.parse_retry_after(raw) == expected, raw
    for raw in ("inf", "nan", "soon"):
        assert model_proxy._retry_after(httpx.Response(429, headers={"retry-after": raw})) == model_proxy.DEFAULT_RETRY_AFTER
    assert model_proxy._retry_after(httpx.Response(429, headers={"retry-after": "7200"})) == 3600
    assert model_proxy.reported_retry_after(httpx.Response(429, headers={"retry-after": "7200"})) == 7200


@respx.mock
def test_a_later_key_refresh_with_requests_left_clears_the_daily_block(routes, secrets_memory):
    secrets_memory.put("workers/openrouter", KEY, actor="test")
    used_up = {"data": {"limit": None, "usage": 0.0, "is_free_tier": True,
                        "free_model_daily_requests": {"used": 50, "limit": 50, "remaining": 0}}}
    after_purchase = {"data": {"limit": None, "usage": 10.0, "is_free_tier": False,
                               "free_model_daily_requests": {"used": 50, "limit": 1000, "remaining": 950}}}
    key = respx.get(catalog.OPENROUTER_KEY_URL)
    key.side_effect = [httpx.Response(200, json=used_up), httpx.Response(200, json=after_purchase)]
    for expected in ("EXHAUSTED", "UNKNOWN"):
        with httpx.Client() as c:
            catalog.refresh_openrouter_key_info(routes, c)
        daily = routes.scalar(select(QuotaState).where(QuotaState.provider == "openrouter",
                                                       QuotaState.scope == "free-daily"))
        assert daily.state == expected
    assert free_tier.limit_problem(routes) is None


def test_concurrent_free_requests_cannot_both_take_the_last_slot(proxy, db, monkeypatch):
    """The count and the PENDING row are one atomic step: with 19 of 20 per-minute requests used, two concurrent
    requests never both pass (the second waits for the write lock, then counts the first)."""
    from aeos.broker import model_proxy
    from aeos.routing import facade as routing_facade

    _forwarded(db, 19)
    db.commit()
    real = routing_facade.free_tier_limit_problem

    def slow(session, route):
        out = real(session, route)
        time.sleep(0.4)  # widen the window between the count and the PENDING row
        return out

    monkeypatch.setattr(routing_facade, "free_tier_limit_problem", slow)
    headers = {"authorization": f"Bearer {proxy['PUBLIC']['token']}"}
    body = {"model": "free", "messages": [{"role": "user", "content": "hi"}]}
    results: list[object] = []

    def call() -> None:
        try:
            results.append(model_proxy._prepare(headers, {}, body))
        except model_proxy.ProxyError as exc:
            results.append(exc)

    threads = [threading.Thread(target=call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    refused = [r for r in results if isinstance(r, model_proxy.ProxyError)]
    assert len(results) == 2 and len(refused) == 1, results
    assert refused[0].status == 429 and refused[0].code == "FREE_TIER_LIMIT_REACHED"


# ------------------------------------------------------------------ delta verification fixes (2026-10-27)


def test_a_429_with_an_unparseable_retry_after_is_still_recorded_as_a_rate_limit(proxy, db):
    with respx.mock(assert_all_mocked=True) as mock:
        up = mock.post(UPSTREAM).mock(return_value=httpx.Response(429, headers={"retry-after": OVERFLOW_DATE},
                                                                  json={"error": {"code": 429}}))
        r = _call(proxy)
        assert r.status_code == 429 and r.json()["error"]["code"] == "UPSTREAM_RATE_LIMITED" and up.call_count == 1
    db.expire_all()
    req = db.scalar(select(BrokerRequest).where(BrokerRequest.upstream_model.is_not(None)))
    assert req.outcome == "RATE_LIMITED"  # never left PENDING by a header the parser could not read
    assert db.scalar(select(QuotaState).where(QuotaState.provider == "openrouter", QuotaState.scope == FREE)) is not None
    assert db.scalar(select(QuotaState).where(QuotaState.scope == "free-rpm")).state == "EXHAUSTED"


def _probe_rows() -> list[BrokerRequest]:
    from aeos.core.db import session_factory

    with session_factory()() as s:  # a fresh connection: what is committed, not what this test's session holds
        return list(s.scalars(select(BrokerRequest).where(BrokerRequest.exposed_model == "feasibility-probe")))


def test_a_probe_stays_counted_when_the_database_refuses_its_bookkeeping(proxy, db, settings):
    """A REAL flush failure after the request was sent: another connection holds the SQLite write lock (BEGIN
    IMMEDIATE) from the moment the upstream answers. The probe's count was committed before sending, so it survives;
    the error says the request was sent and counted (no PendingRollbackError, no 500)."""
    import sqlite3

    holder = sqlite3.connect(str(settings.layout.db_path), isolation_level=None, timeout=1)

    def answer(request):
        holder.execute("BEGIN IMMEDIATE")  # from here on every write of this AEOS instance waits, then fails
        return httpx.Response(503, json={"error": "upstream down"})

    try:
        with respx.mock(assert_all_mocked=True) as mock:
            mock.post(UPSTREAM).mock(side_effect=answer)
            with pytest.raises(Blocked) as ei:
                broker.probe_route(db, route_key=FREE, actor="user:owner")
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert ei.value.code == "PROBE_BOOKKEEPING_FAILED"
    assert "was sent and is counted against the OpenRouter free-model limit" in ei.value.message
    (row,) = _probe_rows()
    assert row.outcome == "PENDING"  # its settlement was refused too: it keeps counting, never under-counted
    from aeos.core.db import session_factory

    with session_factory()() as s:
        assert free_tier.usage(s)["day"] == 1


def test_a_probe_error_after_sending_keeps_its_count(proxy, db, monkeypatch):
    from aeos.broker import feasibility

    def refuse(*_a, **_k):
        raise Blocked("evidence store refused", code="EVIDENCE_REFUSED")

    monkeypatch.setattr(feasibility, "_evidence", refuse)
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(UPSTREAM).mock(return_value=httpx.Response(503))
        with pytest.raises(Blocked) as ei:
            broker.probe_route(db, route_key=FREE, actor="user:owner")
    assert ei.value.code == "EVIDENCE_REFUSED"
    assert [r.outcome for r in _probe_rows()] == ["UPSTREAM_ERROR"]  # settled right after the attempt: counted
    assert free_tier.usage(db)["day"] == 1


def test_a_probe_that_was_never_sent_says_so_and_releases_its_count(proxy, db):
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(UPSTREAM).mock(side_effect=httpx.InvalidURL("no such url"))  # not an HTTPError: a bookkeeping path
        with pytest.raises(Blocked) as ei:
            broker.probe_route(db, route_key=FREE, actor="user:owner")
    assert ei.value.code == "PROBE_BOOKKEEPING_FAILED"
    assert "the request was not sent (its count reservation was released)" in ei.value.message
    assert "counted against" not in ei.value.message
    assert [r.outcome for r in _probe_rows()] == ["NOT_SENT"] and free_tier.usage(db)["day"] == 0


def test_a_probe_reserves_its_count_before_sending_so_a_worker_request_cannot_take_the_same_slot(proxy, db):
    _forwarded(db, 19)  # 19 of 20 per-minute requests used
    db.commit()
    seen: dict[str, int] = {}

    def answer(request):
        # while the probe's request is in flight, a worker request asks for the last slot: the probe already holds it
        seen["worker_status"] = _call(proxy).status_code
        return httpx.Response(503)

    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(UPSTREAM).mock(side_effect=answer)
        assert broker.probe_route(db, route_key=FREE, actor="user:owner")["outcome"] == "UPSTREAM_ERROR"
    assert seen["worker_status"] == 429
    assert free_tier.usage(db)["minute"] == 20


def test_negative_retry_after_falls_back_to_the_rate_limit_reset_header(routes):
    from aeos.broker import model_proxy

    reset = int((utcnow() + timedelta(minutes=20)).timestamp() * 1000)
    info = model_proxy.rate_limit_info(httpx.Response(429, headers={"retry-after": "-30",
                                                                    "x-ratelimit-reset": str(reset)}))
    assert info["retry_after"] is None
    out = free_tier.note_rate_limited(routes, route_key=FREE, retry_after_seconds=info["retry_after"],
                                      headers=info["headers"])
    assert out["basis"] == "x-ratelimit-reset"


def test_the_seed_end_date_of_a_free_route_survives_a_catalog_refresh(routes, monkeypatch):
    """The seeded Laguna S 2.1 end date comes from the research source (S11), not from the catalog: a catalog that
    lists the model free without an expiration_date never clears it, and decision C refuses the route after it."""
    install_fakes(monkeypatch)
    laguna = "openrouter/poolside/laguna-s-2.1:free"
    seeded = service.get_route(routes, laguna).free_until
    assert seeded is not None and "S11" in service.get_route(routes, laguna).expires_note
    listing = {"data": [{"id": "poolside/laguna-s-2.1:free", "name": "Poolside: Laguna S 2.1 (free)",
                         "context_length": 262144, "pricing": {"prompt": "0", "completion": "0"},
                         "supported_parameters": ["tools"]}]}
    _discover(routes, listing)
    r = service.get_route(routes, laguna)
    assert r.free_until == seeded and "S11" in r.expires_note
    after = datetime(2026, 11, 2, 12, tzinfo=UTC)
    assert any("free period ended" in p for p in free_tier.decision_c_problems(r, now=after))


def test_a_busy_count_lock_refuses_with_503_and_sends_nothing(proxy, db, settings):
    import sqlite3

    before = db.scalar(select(func.count()).select_from(BrokerRequest))
    holder = sqlite3.connect(str(settings.layout.db_path), isolation_level=None, timeout=1)
    holder.execute("BEGIN IMMEDIATE")  # another writer holds the SQLite write lock beyond the busy timeout
    try:
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
            up = mock.post(UPSTREAM).mock(return_value=httpx.Response(200, json=_ok()))
            r = _call(proxy)
            assert r.status_code == 503 and r.json()["error"]["code"] == "FREE_TIER_LOCK_UNAVAILABLE"
            assert up.call_count == 0
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(BrokerRequest)) == before


def test_the_worker_agent_can_be_enabled_with_a_public_only_free_route(routes, monkeypatch):
    from aeos.workforce import agents
    from tests.unit.test_workforce_support import seed_and_enable_implementer

    install_fakes(monkeypatch)
    agent = seed_and_enable_implementer(routes)
    no_route = "no APPROVED worker route is eligible for this domain"
    assert no_route in agents.prerequisites(routes, agent, domain="PERSONAL")["reasons"]
    _worker(routes)  # only a decision C route (PUBLIC) is activated
    assert no_route not in agents.prerequisites(routes, agent, domain="PERSONAL")["reasons"]
    assert no_route in agents.prerequisites(routes, agent, domain="OFFICE")["reasons"]  # OFFICE stays denied
    with pytest.raises(Blocked) as ei:
        select_route(routes, task=task(classification="INTERNAL"))
    assert ei.value.code == "NO_ELIGIBLE_ROUTE" and FREE in ei.value.details["rejected"]


# ------------------------------------------------------------------ third verification round (2026-10-27)

LAGUNA = "openrouter/poolside/laguna-s-2.1:free"


def _laguna(pricing: dict, expiration: str | None) -> dict:
    entry = {"id": "poolside/laguna-s-2.1:free", "name": "Poolside: Laguna S 2.1 (free)", "context_length": 262144,
             "pricing": pricing, "supported_parameters": ["tools"]}
    if expiration is not None:
        entry["expiration_date"] = expiration
    return {"data": [entry]}


@pytest.mark.parametrize("first_pricing", [{"prompt": "0.000001", "completion": "0.000002"}, {}],
                         ids=["priced", "unknown-pricing"])
def test_a_priced_or_unknown_listing_never_takes_over_the_seeded_end_date(routes, monkeypatch, first_pricing):
    """Seeded Laguna S 2.1 (free until 2026-10-31 per S11): a listing that is priced or has no price states an
    expiration_date AEOS does not take; a later free listing without a date then leaves the seeded date alone, and
    decision C refuses the route after it."""
    install_fakes(monkeypatch)
    seeded = service.get_route(routes, LAGUNA).free_until
    _discover(routes, _laguna(first_pricing, "2026-12-31"))
    r = service.get_route(routes, LAGUNA)
    assert r.free_until == seeded and "S11" in r.expires_note
    assert r.data_terms["catalog"]["free_until_from_catalog"] is False
    _discover(routes, _laguna({"prompt": "0", "completion": "0"}, None))
    r = service.get_route(routes, LAGUNA)
    assert r.free_until == seeded and r.is_free == "YES"
    assert any("free period ended" in p for p in free_tier.decision_c_problems(r, now=datetime(2026, 11, 2, 12, tzinfo=UTC)))


def test_seed_keeps_the_note_of_a_catalog_end_date_which_the_catalog_can_clear_later(routes, monkeypatch):
    install_fakes(monkeypatch)
    _discover(routes, _laguna({"prompt": "0", "completion": "0"}, "2026-12-31"))  # the catalog's own end date
    r = service.get_route(routes, LAGUNA)
    assert r.free_until.date().isoformat() == "2026-12-31" and r.data_terms["catalog"]["free_until_from_catalog"]
    seed.run(routes)  # an untouched CANDIDATE seed row: its catalog note is not reset to the seed's S11 note
    r = service.get_route(routes, LAGUNA)
    assert r.expires_note == "catalog expiration_date 2026-12-31"
    _discover(routes, _laguna({"prompt": "0", "completion": "0"}, None))
    r = service.get_route(routes, LAGUNA)
    assert r.free_until is None and "no expiration_date" in r.expires_note
    seed.run(routes)  # the stored date is no longer the seed's: its S11 note would contradict it and is not written
    r = service.get_route(routes, LAGUNA)
    assert r.free_until is None and "no expiration_date" in r.expires_note and "S11" not in r.expires_note


def test_only_an_active_retry_decision_resets_the_quota_stop_count(db, monkeypatch):
    FakeModules().install(monkeypatch)
    project = make_project(db)
    _plan, pv, (spec,) = make_plan(db, project, [{"task_key": "a"}])
    scheduler.ensure_executions(db, pv.id, actor="t")
    te = scheduler.get_te(db, spec.id)
    monkeypatch.setattr(scheduler, "QUOTA_STOPS_MAX", 2)
    for _ in range(2):
        db.add(WorkerRun(task_id=spec.id, project_id=project.id, domain="PERSONAL", outcome_code="QUOTA_EXHAUSTED",
                         created_at=utcnow() - timedelta(minutes=5)))
    db.flush()
    assert scheduler.quota_stop_problem(db, spec, te)

    def decision(operations: list[str]) -> Decision:
        d = Decision(kind="EXCEPTION", actor_user_id="owner", actor_kind="HUMAN", target_type="task", target_id=spec.id,
                     domain="PERSONAL", allowed_operations=operations, rationale="owner retry decision")
        db.add(d)
        db.flush()
        return d

    decision(["run_worker_process_sandbox"])  # an EXCEPTION for something else resets nothing
    assert scheduler.quota_stop_problem(db, spec, te)
    retry = decision(["retry"])
    assert scheduler.quota_stop_problem(db, spec, te) is None
    retry.status = "INVALIDATED"  # a revoked retry decision no longer resets the count
    db.flush()
    assert scheduler.quota_stop_problem(db, spec, te)


# ------------------------------------------------------------------ final review round (2026-10-27)


def test_an_interrupt_after_the_request_reached_the_transport_keeps_it_counted(proxy, db):
    received: list[str] = []

    def interrupted(request):
        received.append(str(request.url))
        raise KeyboardInterrupt  # Ctrl-C while waiting for the answer: the request is already on its way

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:  # respx counts no call that raised
        mock.post(UPSTREAM).mock(side_effect=interrupted)
        with pytest.raises(KeyboardInterrupt):
            broker.probe_route(db, route_key=FREE, actor="user:owner")
    assert received == [UPSTREAM]
    assert [r.outcome for r in _probe_rows()] == ["UNKNOWN"]
    assert free_tier.usage(db)["day"] == 1


def test_an_unclassified_transport_error_is_counted_and_says_it_is_not_known_whether_it_was_sent(proxy, db):
    def broken(request):
        raise RuntimeError("transport bug")

    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(UPSTREAM).mock(side_effect=broken)
        with pytest.raises(Blocked) as ei:
            broker.probe_route(db, route_key=FREE, actor="user:owner")
    assert ei.value.code == "PROBE_BOOKKEEPING_FAILED"
    assert "it is not known whether the request was sent; it is counted" in ei.value.message
    assert "not sent (" not in ei.value.message
    assert [r.outcome for r in _probe_rows()] == ["UNKNOWN"] and free_tier.usage(db)["day"] == 1


def test_a_failed_probe_bookkeeping_is_audited_as_sent_when_the_database_works(proxy, db, monkeypatch):
    from aeos.broker import feasibility

    monkeypatch.setattr(feasibility, "_evidence", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(UPSTREAM).mock(return_value=httpx.Response(503))
        with pytest.raises(Blocked) as ei:
            broker.probe_route(db, route_key=FREE, actor="user:owner")
    assert ei.value.code == "PROBE_BOOKKEEPING_FAILED"
    db.commit()  # what the API and the CLI do on an AeosError
    failed = db.scalars(select(AuditEvent).where(AuditEvent.action == "broker.route_feasibility.probe")
                        .order_by(AuditEvent.seq.desc())).first()
    assert failed.details["upstream"] == "SENT" and "PROBE_BOOKKEEPING_FAILED" in failed.reason


def test_a_probe_from_a_session_with_uncommitted_writes_is_refused_up_front(proxy, db):
    from aeos.core.errors import Conflict

    db.add(QuotaState(provider="openrouter", scope="unrelated", state="OK", source="test"))  # a pending write
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        up = mock.post(UPSTREAM).mock(return_value=httpx.Response(200, json=_ok()))
        with pytest.raises(Conflict) as ei:
            broker.probe_route(db, route_key=FREE, actor="user:owner")
        db.flush()  # a write already sent to the database: the connection holds a write transaction
        with pytest.raises(Conflict):
            broker.probe_route(db, route_key=FREE, actor="user:owner")
        assert up.call_count == 0
    assert ei.value.code == "PROBE_SESSION_NOT_CLEAN" and _probe_rows() == []
    db.rollback()
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(UPSTREAM).mock(return_value=httpx.Response(200, json=_ok()))
        assert broker.probe_route(db, route_key=FREE, actor="user:owner")["outcome"] == "REACHABLE"


def test_probe_rows_are_marked_in_the_broker_request_list_and_carry_their_usage(proxy, db, admin_client):
    from aeos.core.enums import Domain

    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(UPSTREAM).mock(return_value=httpx.Response(200, json=_ok()))
        broker.probe_route(db, route_key=FREE, actor="user:owner")
    db.commit()
    (settled,) = _probe_rows()
    assert (settled.outcome, settled.prompt_tokens, settled.completion_tokens) == ("OK", 11, 3)
    db.add(BrokerRequest(grant_id="feasibility-probe", run_id="r-old", project_id="feasibility-probe",
                         domain="PERSONAL", route_key=FREE, provider="openrouter", exposed_model="feasibility-probe",
                         upstream_model="cohere/north-mini-code:free", outcome="PENDING",
                         created_at=utcnow() - timedelta(days=1)))
    db.commit()
    admin_client.select_domain(Domain.PERSONAL)
    items = admin_client.get("/api/broker/requests").json()["items"]
    probes = [i for i in items if i["kind"] == "FEASIBILITY_PROBE"]
    assert sorted(i["outcome"] for i in probes) == ["ABANDONED", "OK"]  # a probe never settled on an earlier day
    assert all(i["run_id"] is None for i in probes)
    assert {i["kind"] for i in items} == {"FEASIBILITY_PROBE"}  # no worker call was made in this test
    assert next(i for i in probes if i["outcome"] == "OK")["prompt_tokens"] == 11


def _registry(db, settings):
    from aeos.doctor.checks.model_route import route_registry
    from aeos.doctor.model import CheckContext
    from tests.unit.test_inventory_fakes import base_runner

    return route_registry(CheckContext(settings=settings, session=db, runner=base_runner(), repo=settings.data_dir,
                                       now=utcnow()))


def test_bite_a_public_only_free_worker_trial_makes_the_registry_ready_for_public_work(routes, settings, monkeypatch):
    """Decision C promised that a WORKER TRIAL serves PUBLIC tasks at once, but the readiness check (and so
    CAN_RUN_WORKER and agent-enable) judged eligibility for INTERNAL only, so a PUBLIC-only route never counted
    (owner's Mac, 2026-10-27: '1 route APPROVED/TRIAL tetapi tidak ada yang eligible ... INTERNAL not allowed')."""
    install_fakes(monkeypatch)
    before = _registry(routes, settings)
    assert before.status == "MISSING" and before.error_code == "NO_APPROVED_WORKER_ROUTE"  # near miss: nothing yet
    _worker(routes)
    out = _registry(routes, settings)
    assert out.status == "CONFIGURED", out.summary
    assert "PERSONAL/PUBLIC saja (keputusan C)" in out.summary and FREE in out.summary
    assert "tugas INTERNAL menunggu" in out.summary
    assert out.evidence["eligible_by_class"] == {"PUBLIC": [FREE]}
    assert out.evidence["evaluated_for"]["data_classifications"] == ["INTERNAL", "PUBLIC"]
    # routing itself is unchanged: an INTERNAL task still never reaches the free route
    assert FREE not in eligible_routes(routes, domain="PERSONAL", data_classification="INTERNAL").eligible
