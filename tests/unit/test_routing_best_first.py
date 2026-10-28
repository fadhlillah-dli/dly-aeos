"""Load-aware best-first selection (ADR-0124; owner decision R31 of 2026-10-28, answers 1-4).

Bite tests on fake routes (the seeded registry, activated as the owner would) and fake quota/ledger/in-flight rows:
each red case asserts its own reason text, each near miss is green. No network, no real credentials, no money: paid
routes are never chosen, INTERNAL never reaches a ``:free`` route, the agent preference only re-orders eligible routes,
and nothing here changes a route's configuration identity, the worker prompt revision or the policy version.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from aeos.core import config as config_mod
from aeos.core.errors import Blocked
from aeos.core.timeutil import utcnow
from aeos.models.cost import QuotaState, UsageLedgerEntry
from aeos.models.routing import ModelRoute
from aeos.models.workforce import WorkerRun
from aeos.routing import capacity, seed, service
from aeos.routing import facade as routing
from aeos.routing.facade import select_route
from aeos.workforce import integrations, scheduler
from tests.unit.test_routing_fakes import human_decision, install_fakes
from tests.unit.test_routing_free_worker import FREE, FREE2, _activate, _midday_utc, _move, _worker  # noqa: F401
from tests.unit.test_routing_service import approve, task
from tests.unit.test_workforce_support import make_plan, make_project

pytestmark = pytest.mark.usefixtures("worker_keys")

GO_GLM = "opencode-go/glm-5.3-flash"  # $60/month allowance: 5h window $12, cap 2, LiveBench prior $1.63
GO_MIMO = "opencode-go/mimo-v2.6-flash"  # $60/month, no LiveBench prior: DEFAULT estimate = half the window ($6)
GO_MINIMAX = "opencode-go/minimax-m3.1-flash"  # $20/month: 5h window $4, cap 1, prior $1.09
GO_WATCH = "opencode-go/glm-5.3"  # a WATCHLIST candidate
PAID = "openrouter/vendor/pro"

# Before this change (origin/main 27ebb58, computed 2026-10-28 on macOS arm64): the worker runtime fingerprint, the
# policy version and the configuration identity of every seeded WORKER/SUPERVISOR route. A change to any of them voids
# the owner's ACTIVE TRIALs; it needs an owner re-activation plan, never a silent update of these values.
GOLDEN_PROMPT_REVISION = "wp2-b8004ffcbf3575f1"
GOLDEN_POLICY_VERSION = "pol-b717185120149d19"
GOLDEN_ROUTE_HASHES = {
    "claude-native/fable": "sha256:ac8c68cea8b5c05592d858af8fa11f129f872fcdd8ef760e93cdd668d1013d19",
    "claude-native/opus": "sha256:62a5b7880ff53024f10e45371c47cd4735b2caad711c23d5934010e7f2c4c098",
    "opencode-go/glm-5.3-flash": "sha256:013ad474223f296ceb4364dd8e054196f946397635e9b3ecf253e919c50e4e2a",
    "opencode-go/hy3": "sha256:ee9129b85d225d1a838e6032a6e2cb19b2dbaa22216ae1ed023e7e1cd945f4bf",
    "opencode-go/kimi-k2.7-code": "sha256:46db60ce98336b860e87df37341b0780eadd8da7a3e04fe280528519ccd0f1c5",
    "opencode-go/mimo-v2.6-flash": "sha256:da53c0853853f240505c7bf5c6f495158ec4e8d2c227ab2948b862f5ea1a8df8",
    "opencode-go/minimax-m3.1-flash": "sha256:87d4dc4b9f1ee1cbceb59a12f5408bf4cd86d2717cf955555f8387a3dfe7be2a",
    "opencode-zen/deepseek-v4.1-flash-free": "sha256:d735aff4f3e10ffc74a86cbe8a2e25032dc69a097a7296ef54b0d93f7044a5e5",
    "opencode-zen/glm-5.3-flash-free": "sha256:5d4d17cc5f23d1d1fadf4134fb001f0da6e3a904457e1f74fec8b569a855a612",
}


@pytest.fixture
def routes(db):
    seed.run(db)
    db.commit()
    return db


def _go(db, *keys: str) -> None:
    """The owner's activation of Go WORKER routes: one plan-fee BUDGET decision, a reviewed region, a TRIAL each."""
    budget = human_decision(db, kind="BUDGET", target_type="model_budget", target_id="opencode-go",
                            auth_scope={"routes": list(keys)})
    for key in keys:
        service.get_route(db, key).region = "EU"
        approve(db, key, kind="TRIAL", details={"budget_decision_id": budget.id})


def _paid(db, *, budget: bool = True) -> ModelRoute:
    """A paid OpenRouter WORKER route (INTERNAL allowed), activated; with the owner's BUDGET decision when *budget*."""
    r = ModelRoute(route_key=PAID, pool="WORKER", provider="openrouter", model_id="vendor/pro",
                   endpoint="https://openrouter.ai/api/v1", is_free="NO", priority=50,
                   pricing={"prompt": "0.000001", "completion": "0.000002", "basis": "OpenRouter catalog"},
                   eligible_domains=["PERSONAL"], allowed_data_classes=["PUBLIC", "INTERNAL"], supports_tools="YES",
                   context_window=131072, harness="opencode", tool_set=list(service.REVIEWED_WORKER_TOOLS),
                   data_terms={"may_use_inputs_for_training": False, "trial_only_no_confidential": False},
                   status="CANDIDATE", mode="LIVE")
    db.add(r)
    db.flush()
    details = None
    if budget:
        details = {"budget_decision_id": human_decision(db, kind="BUDGET", target_type="model_route",
                                                        target_id=PAID).id}
    return approve(db, PAID, kind="TRIAL", details=details)


def _spec(db, classification: str = "INTERNAL", role: str = "feature-implementer"):
    p = make_project(db)
    _plan, _pv, specs = make_plan(db, p, [{"task_key": f"k{utcnow().timestamp()}".replace(".", ""),
                                            "data_classification": classification, "role_key": role}])
    return specs[0]


def _run(db, route_key: str, *, classification: str = "INTERNAL", status: str = "RUNNING",
         role: str = "feature-implementer", hold: str | None = None, used: str | None = None) -> WorkerRun:
    """A LIVE run on *route_key*: in flight (LEASED/RUNNING) or finished, with the hold admission recorded."""
    spec = _spec(db, classification, role)
    run = WorkerRun(task_id=spec.id, project_id=spec.project_id, domain=spec.domain, execution_status=status,
                    model_route_key=route_key, role_key=role, mode="LIVE",
                    caps={"allowance_hold": {"usd": hold, "basis": "PRIOR"}} if hold is not None else {},
                    usage_summary={"allowance_usd": float(used)} if used is not None else {})
    db.add(run)
    db.flush()
    return run


def _use(db, route_key: str, usd: str, *, run: WorkerRun | None = None, hours_ago: float = 1) -> None:
    """A recorded Go call (LIVE, REPORTED allowance) in the trailing 5h window."""
    db.add(UsageLedgerEntry(at=utcnow() - timedelta(hours=hours_ago), route_key=route_key, provider="opencode-go",
                            category="WORKER_API", source="broker", requests=1, cost_usd=0, charged_usd=0, mode="LIVE",
                            run_id=run.id if run is not None else None,
                            details={"billing": "SUBSCRIPTION", "allowance_usd": usd, "allowance_basis": "REPORTED"}))
    db.flush()


def _set(monkeypatch, **values: Any) -> None:
    for name, value in values.items():
        monkeypatch.setenv(f"AEOS_{name.upper()}", str(value))
    config_mod.reset_settings_cache()


def _skip(sel_or_exc, key: str) -> str:
    rejected = sel_or_exc.rejected if hasattr(sel_or_exc, "rejected") else sel_or_exc.details["rejected"]
    return " | ".join(rejected.get(key, []))


# ------------------------------------------------------------------ 1. ranking and the agent preference (contract a)


def test_a_preferred_free_route_never_serves_an_internal_task(routes, monkeypatch):
    """Red: the agent lists the :free route first; an INTERNAL task still never lands there (hard filter first).
    Near miss: the same preference re-orders two eligible Go routes."""
    install_fakes(monkeypatch)
    _worker(routes)
    _go(routes, GO_GLM, GO_MIMO)
    sel = select_route(routes, task=task(classification="INTERNAL"), preferred_routes=[FREE, GO_MIMO])
    assert sel.route.route_key == GO_MIMO and FREE not in sel.eligible
    assert "data classification INTERNAL not allowed" in _skip(sel, FREE)
    assert "agent preference #1" in sel.reason  # the first preferred key that is eligible
    assert select_route(routes, task=task(classification="INTERNAL")).route.route_key == GO_GLM  # priority basis


def test_a_preferred_watchlist_or_unbudgeted_paid_route_is_never_chosen(routes, monkeypatch):
    install_fakes(monkeypatch)
    _go(routes, GO_GLM)
    _paid(routes, budget=False)
    sel = select_route(routes, task=task(), preferred_routes=[GO_WATCH, PAID, "no/such-route"])
    assert sel.route.route_key == GO_GLM and sel.eligible == [GO_GLM]
    assert GO_WATCH not in sel.rejected  # another pool: never even a WORKER candidate
    assert "paid route (is_free=NO)" in _skip(sel, PAID) and PAID not in sel.eligible
    ranked = service.rank(routes, [GO_GLM], "feature-implementer", preferred=[GO_WATCH, PAID])
    assert [r.route_key for r in ranked] == [GO_GLM] and ranked[0].preferred is None  # never adds a route


def test_eval_outranks_the_preference_and_the_preference_outranks_priority(routes, monkeypatch):
    install_fakes(monkeypatch)
    _go(routes, GO_GLM, GO_MIMO, GO_MINIMAX)
    r = service.get_route(routes, GO_MINIMAX)
    r.eval_results = {"feature-implementer": {"score": 0.7, "config_hash": r.config_hash}}
    keys = [GO_GLM, GO_MIMO, GO_MINIMAX]
    order = [x.route_key for x in service.rank(routes, keys, "feature-implementer", preferred=[GO_MIMO])]
    assert order == [GO_MINIMAX, GO_MIMO, GO_GLM]  # eval first, then the preference, then priority
    assert [x.route_key for x in service.rank(routes, keys, "feature-implementer")] == [GO_MINIMAX, GO_GLM, GO_MIMO]


def test_admission_passes_the_agent_preferred_routes_to_routing(db, monkeypatch):
    """The scheduler reads ``route_policy.preferred_routes`` of the implementing agent (written by the role agents)."""
    from tests.unit.test_workforce_scheduler import _go_ready
    from tests.unit.test_workforce_support import FakeModules

    fakes = FakeModules().install(monkeypatch)
    _route, specs = _go_ready(db, fakes)
    agent = db.scalar(select(scheduler.AgentDefinition).where(
        scheduler.AgentDefinition.agent_key == scheduler.IMPLEMENTER_AGENT_KEY))
    agent.route_policy = {**(agent.route_policy or {}), "preferred_routes": [GO_MIMO, GO_GLM]}
    db.flush()
    seen: list[Any] = []
    real = integrations.select_route_detail

    def spy(session, t, *, preferred_routes=None):
        seen.append(preferred_routes)
        return real(session, t, preferred_routes=preferred_routes)

    monkeypatch.setattr(scheduler.ext, "select_route_detail", spy)
    scheduler.admission_check(db, specs[0].id)
    assert seen == [[GO_MIMO, GO_GLM]]


# ------------------------------------------------------------------ 2. decision C' (Go first for PUBLIC) and its cap


def test_public_picks_go_before_free_and_internal_rules_are_unchanged(routes, monkeypatch):
    install_fakes(monkeypatch)
    free = _worker(routes)
    _go(routes, GO_GLM)
    free.eval_results = {"feature-implementer": {"score": 0.99, "config_hash": free.config_hash}}
    sel = select_route(routes, task=task(classification="PUBLIC"), preferred_routes=[FREE])
    assert sel.route.route_key == GO_GLM and sel.eligible == [GO_GLM, FREE] and "Go first" in sel.reason
    assert select_route(routes, task=task(classification="INTERNAL")).route.route_key == GO_GLM


def test_public_is_held_at_its_share_of_the_go_window_while_internal_is_admitted(routes, monkeypatch):
    """go_public_share 0.5 of a $12 window = $6 for PUBLIC; the glm prior is $1.63. PUBLIC used $4.37: fits exactly
    (green); $4.38: PUBLIC_SHARE (red) while an INTERNAL task is still admitted on the same route."""
    install_fakes(monkeypatch)
    _go(routes, GO_GLM)
    done = _run(routes, GO_GLM, classification="PUBLIC", status="REVIEW", used="4.37")
    _use(routes, GO_GLM, "4.37", run=done)
    assert select_route(routes, task=task(classification="PUBLIC")).route.route_key == GO_GLM  # == fits
    _use(routes, GO_GLM, "0.01", run=done)
    with pytest.raises(Blocked) as ei:
        select_route(routes, task=task(classification="PUBLIC"))
    assert ei.value.code == "ROUTE_CAPACITY" and "never a paid route" in ei.value.message
    assert "PUBLIC_SHARE: route opencode-go/glm-5.3-flash: PUBLIC tasks have used their share" in _skip(ei.value, GO_GLM)
    assert "$" not in _skip(ei.value, GO_GLM)  # no allowance figures in a reason (they span domains)
    assert select_route(routes, task=task(classification="INTERNAL")).route.route_key == GO_GLM


# ------------------------------------------------------------------ 3. per-task allowance fit and in-flight holds


def test_allowance_fit_equal_fits_and_one_cent_more_skips_to_the_next_route(routes, monkeypatch):
    install_fakes(monkeypatch)
    _go(routes, GO_GLM, GO_MIMO)
    _use(routes, GO_GLM, "10.37")  # $12 window - $10.37 = $1.63 = the LiveBench prior of glm-5.3-flash
    fit = capacity.allowance_fit(routes, service.get_route(routes, GO_GLM), role="feature-implementer",
                                 data_classification="INTERNAL", runs=[])
    assert fit["skip"] is None and fit["estimate"]["basis"] == "PRIOR" and fit["estimate"]["usd"] == "1.63"
    assert "LiveBench 2.0 release 2026-10-25" in fit["estimate"]["source"]
    assert select_route(routes, task=task()).route.route_key == GO_GLM
    _use(routes, GO_GLM, "0.01")
    sel = select_route(routes, task=task())
    assert sel.route.route_key == GO_MIMO and "skipped opencode-go/glm-5.3-flash" in sel.reason
    assert _skip(sel, GO_GLM).startswith("ALLOWANCE_FIT: route opencode-go/glm-5.3-flash: the estimated allowance")


def test_an_in_flight_hold_prevents_a_second_concurrent_overshoot(routes, monkeypatch):
    install_fakes(monkeypatch)
    _go(routes, GO_GLM, GO_MIMO)
    _use(routes, GO_GLM, "9.00")  # $3.00 left: one task ($1.63) fits, two do not
    assert select_route(routes, task=task()).route.route_key == GO_GLM
    first = _run(routes, GO_GLM, hold="1.63", used="0.50")  # admitted and running: holds what it has not used yet
    _use(routes, GO_GLM, "0.50", run=first)
    sel = select_route(routes, task=task())
    assert sel.route.route_key == GO_MIMO and "ALLOWANCE_FIT" in _skip(sel, GO_GLM)
    first.execution_status = "REVIEW"  # finished: the hold is released, its usage stays in the ledger
    routes.flush()
    assert select_route(routes, task=task()).route.route_key == GO_GLM


def test_the_estimate_learns_from_this_instance_history_per_route_and_role(routes, monkeypatch):
    install_fakes(monkeypatch)
    _go(routes, GO_MIMO)
    route = service.get_route(routes, GO_MIMO)
    assert capacity.allowance_estimate(routes, route, "feature-implementer", window_limit=Decimal(12)) == {
        "usd": "6.00", "basis": "DEFAULT", "samples": 0,
        "source": "no history and no prior: half of the route's 5h allowance window (conservative default)"}
    for used in ("0.40", "0.90", "0.70"):
        _run(routes, GO_MIMO, status="REVIEW", used=used)
    _run(routes, GO_MIMO, status="REVIEW", used="5.00", role="qa-test-designer")  # another role: not a sample
    est = capacity.allowance_estimate(routes, route, "feature-implementer")
    assert est["basis"] == "LEDGER_HISTORY" and est["usd"] == "0.70" and est["samples"] == 3


def test_admission_records_the_hold_on_the_run(db, monkeypatch):
    from tests.unit.test_workforce_scheduler import _go_ready
    from tests.unit.test_workforce_support import FakeModules

    fakes = FakeModules().install(monkeypatch)
    _route, specs = _go_ready(db, fakes)
    adm = scheduler.admission_check(db, specs[0].id)
    assert adm.admitted, adm.reasons
    adm.answers["route_capacity"] = {"hold": {"usd": "1.63", "basis": "PRIOR"}}
    run = scheduler.acquire_lease(db, specs[0].id, actor="t", admission=adm)
    assert run.caps["allowance_hold"] == {"usd": "1.63", "basis": "PRIOR"}



GO_PRO = "opencode-go/kimi-k3.1-pro"  # $15/month: 5h window $3.00, cap 1, LiveBench prior $7.43 (bigger than the window)


def _promote(db, key: str) -> None:
    """The owner's WATCHLIST -> WORKER promotion of a Go route (ADR-0125's owner action, simulated on the row), then its
    activation: a new configuration, so a new hash before the BUDGET + TRIAL decisions."""
    r = service.get_route(db, key)
    r.pool, r.supports_tools = "WORKER", "YES"
    r.config_hash = service.compute_config_hash(r)
    db.flush()
    _go(db, key)


def test_a_prior_bigger_than_the_whole_window_is_capped_so_the_route_still_serves(routes, monkeypatch):
    """Review finding 1. Red before the fix: the kimi-k3.1-pro prior ($7.43) exceeds its whole 5h window ($3.00), so no
    task ever fitted and no history could ever replace the prior. Now the estimate is capped at the window: the route
    takes a task on an empty window (equal fits, as a pinned primary too), and one cent of usage skips it."""
    install_fakes(monkeypatch)
    _promote(routes, GO_PRO)
    route = service.get_route(routes, GO_PRO)
    est = capacity.allowance_estimate(routes, route, "feature-implementer", window_limit=Decimal("3.00"))
    assert est["usd"] == "3.00" and est["basis"] == "PRIOR" and est["capped_at_window"] is True
    assert est["uncapped_usd"] == "7.43" and "capped at the route's whole 5h window" in est["source"]
    fit = capacity.allowance_fit(routes, route, role="feature-implementer", data_classification="INTERNAL", runs=[])
    assert fit["skip"] is None and fit["remaining_usd"] == "3.00"
    assert select_route(routes, task=task()).route.route_key == GO_PRO
    pinned = task()
    pinned.model_route_policy = {"primary": GO_PRO}
    assert select_route(routes, task=pinned).route.route_key == GO_PRO
    assert capacity.allowance_estimate(routes, service.get_route(routes, GO_GLM), "feature-implementer",
                                       window_limit=Decimal(12))["usd"] == "1.63"  # below its window: not capped
    _use(routes, GO_PRO, "0.01")
    with pytest.raises(Blocked) as ei:
        select_route(routes, task=task())
    assert ei.value.code == "ROUTE_CAPACITY"
    assert _skip(ei.value, GO_PRO).startswith("ALLOWANCE_FIT: route opencode-go/kimi-k3.1-pro: the estimated allowance")


def test_an_eval_bound_task_obeys_the_route_caps_and_waits(routes, monkeypatch):
    """Review finding 3. Red: a task bound to an evaluation of the cap-1 minimax route while another task runs there is
    refused with ROUTE_CAPACITY (it waits; never another route), and admission enforces the selected route's capacity
    answer (``wait``). Near miss: with the route free the evaluation task is served on exactly that route."""
    install_fakes(monkeypatch)
    _go(routes, GO_MINIMAX, GO_GLM)
    bound = {"status": "SERVED", "route_key": GO_MINIMAX, "eval_run_id": "ev-1", "slot_id": "s-1", "pool": "WORKER"}
    monkeypatch.setattr(service, "_eval_serving", lambda session, t: dict(bound))
    sel = select_route(routes, task=task())
    assert sel.route.route_key == GO_MINIMAX and sel.eval_run_id == "ev-1"  # near miss: free route, served
    assert capacity.route_capacity(routes, task=task(), route=service.get_route(routes, GO_MINIMAX))["wait"] is None
    _run(routes, GO_MINIMAX, hold="1.09")
    with pytest.raises(Blocked) as ei:
        select_route(routes, task=task())
    assert ei.value.code == "ROUTE_CAPACITY" and "bound to evaluation ev-1" in ei.value.message
    assert "ROUTE_BUSY: 1 active task(s) on route opencode-go/minimax-m3.1-flash" in _skip(ei.value, GO_MINIMAX)
    route, reasons, code = integrations.select_route_detail(routes, task())
    assert route is None and code == "ROUTE_CAPACITY" and code in scheduler.WAIT_CODES
    answer = capacity.route_capacity(routes, task=task(), route=service.get_route(routes, GO_MINIMAX))
    assert answer["wait"]["code"] == "ROUTE_CAPACITY" and answer["wait"]["reason"].startswith("ROUTE_BUSY: route ")
    routes.query(WorkerRun).delete()
    _use(routes, GO_MINIMAX, "3.50")  # $4.00 window: $0.50 left against the $1.09 prior
    answer = capacity.route_capacity(routes, task=task(), route=service.get_route(routes, GO_MINIMAX))
    assert answer["allowance_fit"]["fits"] is False and answer["wait"]["reason"].startswith("ALLOWANCE_FIT: route ")
    assert "$" not in answer["wait"]["reason"] and "0.50" not in answer["wait"]["reason"]  # no figures in a reason
    with pytest.raises(Blocked) as ei:
        select_route(routes, task=task())
    assert ei.value.code == "ROUTE_CAPACITY" and "ALLOWANCE_FIT" in _skip(ei.value, GO_MINIMAX)


def test_admission_enforces_the_route_capacity_wait(db, monkeypatch):
    """Red: the selected route's capacity answer carries a wait -> admission refuses with ROUTE_CAPACITY. Near miss:
    no wait -> admitted."""
    from tests.unit.test_workforce_scheduler import _go_ready
    from tests.unit.test_workforce_support import FakeModules

    fakes = FakeModules().install(monkeypatch)
    _route, specs = _go_ready(db, fakes)
    assert scheduler.admission_check(db, specs[0].id).admitted
    real = integrations.route_capacity
    monkeypatch.setattr(integrations, "route_capacity", lambda s, t, r: {
        **real(s, t, r), "wait": {"code": "ROUTE_CAPACITY", "reason": "ROUTE_BUSY: route x is at its concurrency cap"}})
    adm = scheduler.admission_check(db, specs[0].id)
    assert not adm.admitted and ("ROUTE_CAPACITY", "ROUTE_BUSY: route x is at its concurrency cap") in adm.waits


# ------------------------------------------------------------------ 4. cool-down after 429 / 5xx


def _broker_429(db, route_key: str, retry_after: int | None = None) -> None:
    """What the broker records for an upstream 429 (``model_proxy._record_quota``)."""
    from aeos.broker import model_proxy

    up = SimpleNamespace(provider="opencode-go", route_key=route_key, is_openrouter=False, model_id=route_key)
    p = SimpleNamespace(upstream=up, run_id="run-429")
    model_proxy._record_quota(db, p, retry_after or model_proxy.DEFAULT_RETRY_AFTER, {"retry_after": retry_after})
    db.flush()


def test_a_429_cools_the_route_down_for_new_tasks_and_the_running_attempt_keeps_it(routes, monkeypatch):
    install_fakes(monkeypatch)
    _go(routes, GO_GLM, GO_MIMO)
    running = _run(routes, GO_GLM)
    _broker_429(routes, GO_GLM, retry_after=30)
    row = routes.scalar(select(QuotaState).where(QuotaState.scope == GO_GLM))
    assert row.details["cooldown"]["seconds"] == 60 and row.details["cooldown"]["basis"] == "DOUBLING"
    sel = select_route(routes, task=task())
    assert sel.route.route_key == GO_MIMO and "COOLDOWN after HTTP 429 (streak 1)" in _skip(sel, GO_GLM)
    assert running.model_route_key == GO_GLM  # pinned: nothing re-routes an attempt
    assert service.route_use_problem(routes, service.get_route(routes, GO_GLM)) is None  # its requests still pass
    assert routing.reselect_allowed(routes, GO_GLM) is False  # a 60 s wait is below the 30 min threshold
    _broker_429(routes, GO_GLM)
    _broker_429(routes, GO_GLM)
    assert routes.scalar(select(QuotaState).where(QuotaState.scope == GO_GLM)).details["cooldown"]["seconds"] == 240
    for _ in range(6):
        _broker_429(routes, GO_GLM)
    assert routes.scalar(select(QuotaState).where(QuotaState.scope == GO_GLM)).details["cooldown"]["seconds"] == 1800


def test_a_provider_reset_longer_than_the_doubling_wins_and_allows_reselection(routes, monkeypatch):
    install_fakes(monkeypatch)
    _go(routes, GO_GLM)
    _broker_429(routes, GO_GLM, retry_after=3600)
    cd = routes.scalar(select(QuotaState).where(QuotaState.scope == GO_GLM)).details["cooldown"]
    assert cd["seconds"] == 3600 and cd["basis"] == "PROVIDER_RESET"
    assert routing.reselect_allowed(routes, GO_GLM) is True  # 60 min > route_reselect_after_minutes (30)
    _set(monkeypatch, route_reselect_after_minutes=90)
    assert routing.reselect_allowed(routes, GO_GLM) is False


def test_a_provider_5xx_starts_a_cool_down_and_its_expiry_returns_the_primary(routes, monkeypatch):
    install_fakes(monkeypatch)
    _go(routes, GO_GLM, GO_MIMO)
    routing.note_route_cooldown(routes, provider="opencode-go", route_key=GO_GLM, status=503, run_id="r")
    row = routes.scalar(select(QuotaState).where(QuotaState.scope == GO_GLM))
    assert row.source == "observed-5xx" and row.details["cooldown"]["streak"] == 1
    assert select_route(routes, task=task()).route.route_key == GO_MIMO
    row.resets_at = utcnow() - timedelta(seconds=1)  # the cool-down ended
    routes.flush()
    assert select_route(routes, task=task()).route.route_key == GO_GLM



def test_a_5xx_after_a_longer_429_reset_never_shortens_it(routes, monkeypatch):
    """Review finding 2. Red before the fix: a 429 with Retry-After 3600, then a 503 on the same route, cut the reset to
    the 120 s doubling. Now the longer reset stays (EXISTING_RESET), so new tasks keep skipping the route and a
    quota-stopped task may still move. Near miss: a 5xx on a route with no recorded reset starts the plain doubling."""
    install_fakes(monkeypatch)
    _go(routes, GO_GLM, GO_MIMO)
    _broker_429(routes, GO_GLM, retry_after=3600)
    before = routes.scalar(select(QuotaState).where(QuotaState.scope == GO_GLM)).resets_at
    cd = routing.note_route_cooldown(routes, provider="opencode-go", route_key=GO_GLM, status=503, run_id="r")
    row = routes.scalar(select(QuotaState).where(QuotaState.scope == GO_GLM))
    assert cd["basis"] == "EXISTING_RESET" and cd["streak"] == 2 and row.resets_at == before
    assert cd["seconds"] > 3500 and row.source == "observed-5xx"
    assert routing.reselect_allowed(routes, GO_GLM) is True  # still about 60 min > 30 min
    assert select_route(routes, task=task()).route.route_key == GO_MIMO
    _broker_429(routes, GO_GLM, retry_after=10)  # a later, shorter 429 does not cut it either
    assert routes.scalar(select(QuotaState).where(QuotaState.scope == GO_GLM)).resets_at == before
    near = routing.note_route_cooldown(routes, provider="opencode-go", route_key=GO_MIMO, status=502)
    assert near["basis"] == "DOUBLING" and near["seconds"] == 60


def test_a_quota_stopped_task_moves_only_to_a_zero_marginal_route(routes, monkeypatch):
    """Between attempts the admission accepts a different route only when it is $0 marginal (red: the paid route)."""
    install_fakes(monkeypatch)
    _go(routes, GO_GLM)
    paid = _paid(routes)
    spec = _spec(routes)
    routes.add(WorkerRun(task_id=spec.id, project_id=spec.project_id, domain=spec.domain, execution_status="BLOCKED",
                         model_route_key=GO_MIMO, role_key=spec.role_key, mode="LIVE", outcome_code="QUOTA_EXHAUSTED"))
    routes.flush()
    problem = scheduler._reselection_problem(routes, spec, paid)
    assert problem is not None and "may only move to a $0-marginal route, not openrouter/vendor/pro" in problem
    assert scheduler._reselection_problem(routes, spec, service.get_route(routes, GO_GLM)) is None


# ------------------------------------------------------------------ 5. concurrency caps


def test_the_second_task_on_a_cap_one_route_goes_to_the_next_route(routes, monkeypatch):
    install_fakes(monkeypatch)
    _go(routes, GO_MINIMAX, GO_GLM)
    assert select_route(routes, task=task(), preferred_routes=[GO_MINIMAX]).route.route_key == GO_MINIMAX
    _run(routes, GO_MINIMAX, hold="1.09")
    sel = select_route(routes, task=task(), preferred_routes=[GO_MINIMAX])
    assert sel.route.route_key == GO_GLM
    assert ("ROUTE_BUSY: 1 active task(s) on route opencode-go/minimax-m3.1-flash; its cap is 1 (default for a "
            "subscription route with a monthly allowance of at most 30 USD)") in _skip(sel, GO_MINIMAX)
    _set(monkeypatch, route_max_active='{"opencode-go/minimax-m3.1-flash": 2}')
    assert select_route(routes, task=task(), preferred_routes=[GO_MINIMAX]).route.route_key == GO_MINIMAX


def test_the_go_total_cap_holds_across_routes_and_the_task_waits(routes, monkeypatch):
    install_fakes(monkeypatch)
    _go(routes, GO_GLM, GO_MIMO)
    _run(routes, GO_GLM)
    assert select_route(routes, task=task()).route.route_key == GO_GLM  # cap 2 on glm, 1 of 2 Go slots used
    _run(routes, GO_MIMO)
    with pytest.raises(Blocked) as ei:
        select_route(routes, task=task())
    assert ei.value.code == "ROUTE_CAPACITY" and ei.value.code in scheduler.WAIT_CODES | scheduler.QUOTA_CODES
    assert "their total cap is 2 (setting go_max_active_total)" in _skip(ei.value, GO_GLM)
    route, reasons, code = integrations.select_route_detail(routes, task())
    assert route is None and code == "ROUTE_CAPACITY" and any("ROUTE_BUSY" in r for r in reasons)


def test_the_openrouter_free_routes_share_one_account_wide_cap(routes, monkeypatch):
    install_fakes(monkeypatch)
    _worker(routes)
    _move(routes, FREE2)
    _activate(routes, FREE2)
    assert select_route(routes, task=task(classification="PUBLIC")).route.route_key in {FREE, FREE2}
    _run(routes, FREE, classification="PUBLIC")
    with pytest.raises(Blocked) as ei:
        select_route(routes, task=task(classification="PUBLIC"))
    assert "the account-wide cap is 1 (setting openrouter_free_max_active)" in _skip(ei.value, FREE2)


def test_a_paid_route_is_never_chosen_after_a_skip(routes, monkeypatch):
    """Red: Go is busy and the owner's BUDGET-approved paid route is next: the task waits (would_serve names it).
    Near miss: with a Go slot free, Go serves."""
    install_fakes(monkeypatch)
    _go(routes, GO_MINIMAX)
    _paid(routes)
    assert select_route(routes, task=task()).route.route_key == GO_MINIMAX
    _run(routes, GO_MINIMAX)
    with pytest.raises(Blocked) as ei:
        select_route(routes, task=task())
    assert ei.value.code == "ROUTE_CAPACITY" and ei.value.details["would_serve"] == PAID
    _broker_429(routes, GO_MINIMAX)  # a cool-down (a limit) is held the same way
    routes.query(WorkerRun).delete()
    routes.flush()
    with pytest.raises(Blocked) as ei:
        select_route(routes, task=task())
    assert ei.value.code == "ROUTE_CAPACITY" and ei.value.details["would_serve"] == PAID


# ------------------------------------------------------------------ settings and invariance


def test_route_caps_never_exceed_the_scheduler_hard_cap(monkeypatch, settings):
    assert config_mod.MAX_ROUTE_ACTIVE == scheduler.HARD_MAX_ACTIVE == 2  # this phase (owner answer 4)
    for name, bad, needle in (("AEOS_GO_MAX_ACTIVE_TOTAL", "3", "route concurrency caps must be 1..2"),
                              ("AEOS_OPENROUTER_FREE_MAX_ACTIVE", "0", "route concurrency caps must be 1..2"),
                              ("AEOS_ROUTE_MAX_ACTIVE", '{"opencode-go/x": 3}', "route_max_active values must be"),
                              ("AEOS_GO_PUBLIC_SHARE", "1.5", "go_public_share must be 0..1"),
                              ("AEOS_ROUTE_RESELECT_AFTER_MINUTES", "0", "route_reselect_after_minutes must be")):
        monkeypatch.setenv(name, bad)
        with pytest.raises(ValueError, match=needle):
            config_mod.Settings()
        monkeypatch.delenv(name)
    s = config_mod.Settings()
    assert (s.go_public_share, s.go_max_active_total, s.openrouter_free_max_active, s.route_max_active,
            s.route_reselect_after_minutes) == (0.5, 2, 1, {}, 30)


def test_prompt_revision_policy_version_and_seeded_route_hashes_are_unchanged(routes, monkeypatch):
    """Owner invariant: no ACTIVE TRIAL is voided. The worker runtime fingerprint, the policy version and every seeded
    WORKER/SUPERVISOR configuration hash equal origin/main's, and stay so through ranking, caps, fits and cool-downs."""
    from aeos.worker_runtime import executor

    def snapshot() -> tuple[str, str | None, dict[str, str]]:
        rows = routes.scalars(select(ModelRoute).where(ModelRoute.route_key.in_(list(GOLDEN_ROUTE_HASHES))))
        return (executor.worker_prompt_revision(), service.current_policy_version(),
                {r.route_key: service.compute_config_hash(r) for r in rows})

    assert snapshot() == (GOLDEN_PROMPT_REVISION, GOLDEN_POLICY_VERSION, GOLDEN_ROUTE_HASHES)
    install_fakes(monkeypatch)
    _go(routes, GO_GLM, GO_MIMO, GO_MINIMAX)
    hashes = {k: service.get_route(routes, k).config_hash for k in (GO_GLM, GO_MIMO, GO_MINIMAX)}
    _run(routes, GO_MINIMAX, hold="1.09")
    _use(routes, GO_GLM, "11.00")
    _broker_429(routes, GO_MIMO)
    _set(monkeypatch, go_public_share="0.2", route_max_active='{"opencode-go/glm-5.3-flash": 1}')
    with pytest.raises(Blocked):
        select_route(routes, task=task(classification="PUBLIC"), preferred_routes=[GO_MIMO])
    routing.route_capacity(routes, task=task(), route=service.get_route(routes, GO_GLM))
    after = {k: service.compute_config_hash(service.get_route(routes, k)) for k in hashes}
    assert after == hashes and all(service.get_route(routes, k).config_hash == h for k, h in hashes.items())
    assert snapshot()[:2] == (GOLDEN_PROMPT_REVISION, GOLDEN_POLICY_VERSION)
