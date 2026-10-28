"""Per-token paid routes only by ESCALATION or OVERFLOW (ADR-0128; owner decision R34 of 2026-10-28).

Bite tests on fake routes (the seeded Go registry activated as the owner would, plus one fake paid OpenRouter route) and
fake run/quota rows: each red case asserts its own reason text, each near miss is green. No network, no real key, no
money: nothing here calls a provider.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from aeos.core.errors import Blocked
from aeos.models.audit import AuditEvent, Event
from aeos.models.routing import ModelRoute
from aeos.models.workforce import WorkerRun
from aeos.routing import facade as routing
from aeos.routing import paid, seed, service
from aeos.routing.facade import select_route
from aeos.workforce import scheduler
from tests.unit.test_routing_best_first import (
    GO_GLM,
    GO_MINIMAX,
    GOLDEN_POLICY_VERSION,
    GOLDEN_PROMPT_REVISION,
    GOLDEN_ROUTE_HASHES,
    PAID,
    _broker_429,
    _go,
    _paid,
    _run,
    _set,
)
from tests.unit.test_routing_fakes import install_fakes
from tests.unit.test_routing_service import task
from tests.unit.test_workforce_support import (
    FakeModules,
    make_plan,
    make_project,
    make_route,
    process_sandbox_exception,
    seed_and_enable_implementer,
)

pytestmark = pytest.mark.usefixtures("worker_keys")

TASK_ID = "task-paid-escalation-1"


@pytest.fixture
def routes(db):
    seed.run(db)
    db.commit()
    return db


def _task(classification: str = "INTERNAL", **policy: Any):
    t = task(classification=classification, **policy)
    t.id = TASK_ID
    return t


def _attempt(db, route_key: str, *, outcome: str, status: str = "FAILED", verifier: str | None = None,
             acceptance: str | None = None) -> WorkerRun:
    """A finished attempt of the task on *route_key* (what the executor, verifier and review record)."""
    run = WorkerRun(task_id=TASK_ID, project_id="p", domain="PERSONAL", execution_status=status, mode="LIVE",
                    model_route_key=route_key, role_key="feature-implementer", outcome_code=outcome,
                    verifier_status=verifier, acceptance_status=acceptance)
    db.add(run)
    db.flush()
    return run


def _why(exc: Blocked) -> str:
    """The wait's reasons, the paid route's refusal included (what the admission shows)."""
    return " | ".join(str(r) for r in exc.details.get("reasons") or [])


def _eval(db, key: str, score: float) -> None:
    r = service.get_route(db, key)
    r.eval_results = {"feature-implementer": {"score": score, "config_hash": r.config_hash}}
    db.flush()


# ------------------------------------------------------------------ 1. tiering: paid below every $0-marginal tier


def test_a_paid_route_with_a_better_eval_and_the_first_preference_never_beats_a_zero_marginal_route(routes,
                                                                                                    monkeypatch):
    """Red before ADR-0128: INTERNAL had one tier for Go and paid, so a scored paid route (or an earlier preference)
    ranked first. Now the paid tier is last for INTERNAL and PUBLIC. Near miss: inside the paid tier the eval still
    orders."""
    install_fakes(monkeypatch)
    _go(routes, GO_GLM)
    _paid(routes)
    _eval(routes, PAID, 0.99)
    _eval(routes, GO_GLM, 0.10)
    sel = select_route(routes, task=_task(), preferred_routes=[PAID])
    assert sel.route.route_key == GO_GLM and sel.paid_admission is None
    assert select_route(routes, task=_task()).route.route_key == GO_GLM  # no preference: never lands on paid
    order = [x.route_key for x in service.rank(routes, [PAID, GO_GLM], "feature-implementer", preferred=[PAID],
                                                data_classification="INTERNAL")]
    assert order == [GO_GLM, PAID]
    paid_route, go_route = service.get_route(routes, PAID), service.get_route(routes, GO_GLM)
    assert (service.rank_tier(paid_route, "INTERNAL"), service.rank_tier(go_route, "INTERNAL")) == (2, 1)
    assert (service.rank_tier(paid_route, "PUBLIC"), service.rank_tier(go_route, "PUBLIC")) == (3, 0)
    assert paid.per_token_paid(paid_route) and not paid.per_token_paid(go_route)
    # near miss: a second paid route with a lower score ranks after the first inside the paid tier
    second = ModelRoute(**{c: getattr(paid_route, c) for c in ("pool", "provider", "endpoint", "is_free", "pricing",
                                                               "eligible_domains", "allowed_data_classes",
                                                               "supports_tools", "context_window", "harness",
                                                               "tool_set", "data_terms", "status", "mode")},
                        route_key="openrouter/vendor/pro-b", model_id="vendor/pro-b", priority=1,
                        eval_results={"feature-implementer": {"score": 0.5, "config_hash": "x"}})
    routes.add(second)
    routes.flush()
    ranked = service.rank(routes, [second.route_key, PAID, GO_GLM], "feature-implementer",
                          data_classification="INTERNAL")
    assert [x.route_key for x in ranked] == [GO_GLM, PAID, second.route_key]


# ------------------------------------------------------------------ 2. PUBLIC never paid


def test_a_public_task_is_never_paid_even_when_every_go_route_is_busy(routes, monkeypatch):
    """Red: the only Go route is busy and the BUDGET-approved paid route allows PUBLIC data: the PUBLIC task waits and
    the paid route's rejection says why. Near miss: an INTERNAL task in the same state overflows to it."""
    install_fakes(monkeypatch)
    _go(routes, GO_MINIMAX)
    _paid(routes)
    _run(routes, GO_MINIMAX)
    with pytest.raises(Blocked) as ei:
        select_route(routes, task=_task("PUBLIC"))
    assert ei.value.code == "ROUTE_CAPACITY" and ei.value.details["would_serve"] == PAID
    assert ("PAID_NOT_ADMITTED: paid route openrouter/vendor/pro: a PUBLIC task never runs on a per-token paid route"
            in _why(ei.value))
    near = select_route(routes, task=_task("INTERNAL"))
    assert near.route.route_key == PAID and near.paid_admission["reason"] == "OVERFLOW"


def test_a_public_task_with_only_a_paid_route_is_not_admitted(routes, monkeypatch):
    install_fakes(monkeypatch)
    _paid(routes)
    with pytest.raises(Blocked) as ei:
        select_route(routes, task=_task("PUBLIC"))
    assert ei.value.code == "PAID_NOT_ADMITTED" and "a PUBLIC task never runs on a per-token paid route" in (
        ei.value.message)
    pa = routing.paid_admission(routes, task=_task("PUBLIC"), route=service.get_route(routes, PAID))
    assert pa["admitted"] is False and pa["code"] == "PAID_NOT_ADMITTED"


# ------------------------------------------------------------------ 3. ESCALATION after exactly 2 quality failures


def test_escalation_comes_after_exactly_two_quality_failures_and_never_after_quota_stops(routes, monkeypatch):
    """Red: quota, time-out, resource, lease and non-zero-exit stops never escalate, nor does one quality failure, nor a
    quality failure on the paid route itself. Two quality failures on Go (a verifier FAIL closed as REVIEW_FINDINGS,
    and a REJECTED acceptance) put the paid route first although Go is free."""
    install_fakes(monkeypatch)
    _go(routes, GO_GLM)
    _paid(routes)
    for outcome in ("QUOTA_EXHAUSTED", "TIMEOUT", "RESOURCE_LIMIT", "LEASE_LOST", "EXIT_NONZERO",
                    "CONTROLLER_SHUTDOWN", "CANCELLED"):
        _attempt(routes, GO_GLM, outcome=outcome)
    _attempt(routes, GO_GLM, outcome="QUOTA_EXHAUSTED", verifier="FAIL")  # never a quality failure, whatever it says
    _attempt(routes, PAID, outcome="REVIEW_FINDINGS", verifier="FAIL")  # on a paid route: does not count
    _attempt(routes, GO_GLM, outcome="EXITED_0", status="REVIEW", verifier="FAIL")  # not finished
    assert paid.quality_failures(routes, _task()) == []
    assert select_route(routes, task=_task()).route.route_key == GO_GLM
    first = _attempt(routes, GO_GLM, outcome="REVIEW_FINDINGS", verifier="FAIL")
    sel = select_route(routes, task=_task())
    assert sel.route.route_key == GO_GLM  # 1 of 2
    pa = routing.paid_admission(routes, task=_task(), route=service.get_route(routes, PAID))
    assert pa["admitted"] is False and "no ESCALATION (1 of 2 quality failures)" in pa["why"]
    second = _attempt(routes, GO_GLM, outcome="DISCARDED_BY_OWNER", acceptance="REJECTED")
    sel = select_route(routes, task=_task())
    assert sel.route.route_key == PAID and sel.paid_admission["reason"] == "ESCALATION"
    assert sel.paid_admission["quality_failures"] == [first.id, second.id]
    assert "paid ESCALATION (ADR-0128)" in sel.reason
    # a busy escalated paid route leaves the $0-marginal route after it (never a stuck task)
    _set(monkeypatch, route_max_active='{"openrouter/vendor/pro": 1}')
    _run(routes, PAID)
    assert select_route(routes, task=_task()).route.route_key == GO_GLM
    routes.query(WorkerRun).filter(WorkerRun.execution_status == "RUNNING").delete()
    routes.flush()
    # the owner's switch turns escalation off; PUBLIC never escalates
    _set(monkeypatch, paid_escalation="false")
    assert select_route(routes, task=_task()).route.route_key == GO_GLM
    _set(monkeypatch, paid_escalation="true")
    assert select_route(routes, task=_task()).route.route_key == PAID
    assert not paid.escalation_active(routes, _task("PUBLIC"))


def test_escalation_under_the_pilot_attempt_cap_runs_only_on_the_owners_retry(db, monkeypatch):
    """Review finding (HEAD 5a463f7): the §21 pilot cap is 2 attempts per TaskSpec, so the second quality failure ends
    the task BLOCKED NEEDS_DECISION through request_repair and no third admission happens by itself. Red: after two
    reviewed attempts with findings (each a quality failure in ADR-0128's sense) the task is blocked at the cap and
    admission refuses it. Near miss: the owner's retry (an EXCEPTION decision with extra_attempts) makes it READY; that
    attempt is the one ESCALATION applies to (the routing side of two such failures is
    test_escalation_comes_after_exactly_two_quality_failures_and_never_after_quota_stops)."""
    from tests.unit.test_workforce_scheduler import _ready_env
    from tests.unit.test_workforce_support import make_owner

    fakes = FakeModules().install(monkeypatch)
    _plan, _pv, specs = _ready_env(db, fakes, [{"task_key": "a", "limits": {"max_cost_usd": 0.5}}])
    spec, tid = specs[0], specs[0].id
    assert scheduler.max_attempts(spec) == 2
    runs = []
    for attempt in (1, 2):
        run = scheduler.acquire_lease(db, tid, actor="t")
        assert run.attempt == attempt and run.mode == "LIVE" and run.run_type == "IMPLEMENTATION"
        scheduler.transition_task(db, tid, "RUNNING", reason="bound", actor="t")
        scheduler.set_run_status(db, run, "RUNNING", reason="bound", actor="t")
        scheduler.transition_task(db, tid, "REVIEW", reason="exit 0", actor="t")
        scheduler.set_run_status(db, run, "REVIEW", reason="exit 0", actor="t")
        te = scheduler.request_repair(db, tid, findings=["verifier FAIL"], review_record_id=None, actor="reviewer:x")
        assert te.state == ("READY" if attempt == 1 else "BLOCKED")
        runs.append(run)
    assert [(r.execution_status, r.outcome_code) for r in runs] == [("FAILED", "REVIEW_FINDINGS")] * 2
    assert all(paid.is_quality_failure(r) for r in runs)
    assert te.reason_code == "NEEDS_DECISION" and "attempt cap reached (2/2)" in te.reason
    adm = scheduler.admission_check(db, tid)
    assert not adm.admitted  # two quality failures alone never start a third attempt
    owner = make_owner(db)
    te = scheduler.retry_task(db, tid, actor="user:o", extra_attempts=1, rationale="escalate", actor_user_id=owner)
    assert te.state == "READY" and scheduler.max_attempts(spec, te) == 3


def _stamp(run: WorkerRun, minutes_ago: int) -> WorkerRun:
    from datetime import timedelta

    from aeos.core.timeutil import utcnow

    run.created_at = utcnow() - timedelta(minutes=minutes_ago)
    return run


def test_escalation_never_puts_a_paid_route_first_after_a_quota_stop_on_another_route(routes, monkeypatch):
    """Review finding (HEAD 5a463f7): ADR-0124 decision 4 lets a task whose last attempt stopped at a quota move only
    to a $0-marginal route (or back to the stopped route). Red before the fix: two quality failures on Go GLM, then a
    quota stop on GLM; with GLM and the paid route both free, select_route put the paid route first and the admission
    refused it on every tick (the task waited while GLM was free). Now GLM is selected and admission accepts it.
    Near misses: a quota stop on the paid route itself still lets it go first; without a quota stop it goes first."""
    install_fakes(monkeypatch)
    _go(routes, GO_GLM)
    _paid(routes)
    _stamp(_attempt(routes, GO_GLM, outcome="REVIEW_FINDINGS", verifier="FAIL"), 30)
    _stamp(_attempt(routes, GO_GLM, outcome="REVIEW_FINDINGS", verifier="FAIL"), 20)
    assert select_route(routes, task=_task()).route.route_key == PAID  # near miss: no quota stop, ESCALATION first
    stop = _stamp(_attempt(routes, GO_GLM, outcome="QUOTA_EXHAUSTED"), 10)
    routes.flush()
    assert paid.quota_stop_route(routes, _task()) == GO_GLM
    assert scheduler._last_quota_stop(routes, TASK_ID).model_route_key == GO_GLM  # the same row the scheduler reads
    sel = select_route(routes, task=_task())
    assert sel.route.route_key == GO_GLM and sel.paid_admission is None
    assert scheduler._reselection_problem(routes, _task(), sel.route) is None  # admission accepts it
    refused = scheduler._reselection_problem(routes, _task(), service.get_route(routes, PAID))
    assert refused is not None and "may only move to a $0-marginal route" in refused
    # GLM busy: the paid route is reached after the skip and admitted by ESCALATION, and ADR-0124 still makes the task
    # wait for a $0-marginal route (unchanged; GLM is offered again as soon as it is free)
    _set(monkeypatch, route_max_active='{"opencode-go/glm-5.3-flash": 1}')
    _run(routes, GO_GLM)
    assert select_route(routes, task=_task()).route.route_key == PAID
    routes.query(WorkerRun).filter(WorkerRun.execution_status == "RUNNING").delete()
    routes.flush()
    # near miss: the quota stop was on the paid route itself -> it may go first again
    stop.model_route_key = PAID
    routes.flush()
    assert paid.quota_stop_route(routes, _task()) == PAID
    assert scheduler._last_quota_stop(routes, TASK_ID).model_route_key == PAID
    sel = select_route(routes, task=_task())
    assert sel.route.route_key == PAID and sel.paid_admission["reason"] == "ESCALATION"
    assert scheduler._reselection_problem(routes, _task(), sel.route) is None
    # a later finished attempt that is not a quota stop ends the restriction (both readers agree)
    _stamp(_attempt(routes, GO_GLM, outcome="TIMEOUT"), 5)
    routes.flush()
    assert paid.quota_stop_route(routes, _task()) is None and scheduler._last_quota_stop(routes, TASK_ID) is None


# ------------------------------------------------------------------ 4. OVERFLOW only when every $0 route is blocked


def test_overflow_only_when_every_zero_marginal_route_is_blocked(routes, monkeypatch):
    """Near miss first: one Go route busy, the other open -> Go. Red: both blocked (cap, then cool-down) -> the paid
    route overflows, and the admission re-check agrees. The owner's switch turns it off (the task waits)."""
    install_fakes(monkeypatch)
    _go(routes, GO_GLM, GO_MINIMAX)
    _paid(routes)
    _run(routes, GO_MINIMAX)  # cap 1 on minimax
    sel = select_route(routes, task=_task())
    assert sel.route.route_key == GO_GLM and sel.paid_admission is None
    pa = routing.paid_admission(routes, task=_task(), route=service.get_route(routes, PAID))
    assert pa["admitted"] is False and "1 $0-marginal route(s) can take the task" in pa["why"]
    _run(routes, GO_GLM)  # the Go total cap (2) is reached
    sel = select_route(routes, task=_task())
    assert sel.route.route_key == PAID and sel.paid_admission["reason"] == "OVERFLOW"
    assert set(sel.paid_admission["blocked_zero_marginal"]) == {GO_GLM, GO_MINIMAX}
    assert "paid OVERFLOW (ADR-0128)" in sel.reason
    again = routing.paid_admission(routes, task=_task(), route=service.get_route(routes, PAID))
    assert again["admitted"] is True and again["reason"] == "OVERFLOW" and again["open_zero_marginal"] == []
    _set(monkeypatch, paid_overflow="false")
    with pytest.raises(Blocked) as ei:
        select_route(routes, task=_task())
    assert ei.value.code == "ROUTE_CAPACITY" and "overflow switched off (setting paid_overflow)" in _why(ei.value)
    _set(monkeypatch, paid_overflow="true")
    # a cool-down blocks the same way as a cap
    routes.query(WorkerRun).delete()
    routes.flush()
    assert select_route(routes, task=_task()).route.route_key in {GO_GLM, GO_MINIMAX}
    _broker_429(routes, GO_GLM)
    _broker_429(routes, GO_MINIMAX)
    sel = select_route(routes, task=_task())
    assert sel.route.route_key == PAID and sel.paid_admission["reason"] == "OVERFLOW"


def test_no_zero_marginal_route_at_all_is_not_overflow(routes, monkeypatch):
    """Red: the paid route is the only eligible route: nothing is blocked, so it is not OVERFLOW (the task waits)."""
    install_fakes(monkeypatch)
    _paid(routes)
    with pytest.raises(Blocked) as ei:
        select_route(routes, task=_task())
    assert ei.value.code == "PAID_NOT_ADMITTED"
    assert "no OVERFLOW (no $0-marginal route is eligible for this task, so none is blocked)" in ei.value.message


# ------------------------------------------------------------------ 5. every earlier gate still holds


def test_no_budget_means_no_paid_route_even_under_escalation_or_overflow(routes, monkeypatch):
    install_fakes(monkeypatch)
    _go(routes, GO_MINIMAX)
    _paid(routes, budget=False)
    _run(routes, GO_MINIMAX)
    with pytest.raises(Blocked) as ei:  # overflow conditions hold, but the paid route is not eligible
        select_route(routes, task=_task())
    assert ei.value.code == "ROUTE_CAPACITY" and ei.value.details["would_serve"] is None
    rejected = service.eligible_routes(routes, domain="PERSONAL", data_classification="INTERNAL").rejected[PAID]
    assert any("paid route (is_free=NO): an explicit BUDGET decision id is required" in r for r in rejected)
    routes.query(WorkerRun).delete()
    routes.flush()
    _attempt(routes, GO_MINIMAX, outcome="REVIEW_FINDINGS", verifier="FAIL")
    _attempt(routes, GO_MINIMAX, outcome="REVIEW_FINDINGS", verifier="FAIL")
    sel = select_route(routes, task=_task())  # escalation holds, but only the $0-marginal route is eligible
    assert sel.route.route_key == GO_MINIMAX and PAID not in sel.eligible


def test_a_pinned_paid_primary_needs_escalation_or_overflow_too(routes, monkeypatch):
    install_fakes(monkeypatch)
    _go(routes, GO_GLM)
    _paid(routes)
    with pytest.raises(Blocked) as ei:
        select_route(routes, task=_task(primary=PAID))
    assert ei.value.code == "PAID_NOT_ADMITTED" and "1 $0-marginal route(s) can take the task" in ei.value.message
    _attempt(routes, GO_GLM, outcome="REVIEW_FINDINGS", verifier="FAIL")
    _attempt(routes, GO_GLM, outcome="REVIEW_FINDINGS", verifier="FAILED")
    sel = select_route(routes, task=_task(primary=PAID))
    assert sel.route.route_key == PAID and sel.paid_admission["reason"] == "ESCALATION"


# ------------------------------------------------------------------ 6. never silent: event + audit at the lease


def _paid_ready(db, fakes):
    p = make_project(db)
    plan, pv, specs = make_plan(db, p, [{"task_key": "a", "limits": {"max_cost_usd": 0.5}}])
    fakes.approved[plan.id] = pv.id
    make_route(db, key="worker.paid", is_free="NO")
    fakes.route_key = "worker.paid"
    seed_and_enable_implementer(db)
    process_sandbox_exception(db)
    scheduler.ensure_executions(db, pv.id, actor="t")
    scheduler.refresh_readiness(db, pv.id)
    return specs[0]


def test_a_paid_admission_records_an_event_and_an_audit_record(db, monkeypatch):
    """Red: a paid route that cannot be checked is not admitted (fail closed). Admitted (OVERFLOW): the lease records
    the reason on the run, an event route.paid_admitted and an audit record. Near miss: a free route's lease records
    neither."""
    fakes = FakeModules().install(monkeypatch)
    spec = _paid_ready(db, fakes)
    adm = scheduler.admission_check(db, spec.id)
    waits = dict(adm.waits)
    assert "cannot be checked for ESCALATION or OVERFLOW" in waits["PAID_NOT_ADMITTED"]
    assert "PAID_NOT_ADMITTED" in scheduler.WAIT_CODES
    decided = {"route_key": "worker.paid", "admitted": True, "reason": "OVERFLOW", "code": None,
               "why": "paid route worker.paid: OVERFLOW", "quality_failures": [],
               "blocked_zero_marginal": ["opencode-go/glm-5.3-flash"]}
    monkeypatch.setattr(scheduler.ext, "paid_admission", lambda session, t, route: dict(decided))
    adm = scheduler.admission_check(db, spec.id)
    assert adm.admitted, adm.reasons
    run = scheduler.acquire_lease(db, spec.id, actor="system:scheduler", admission=adm)
    assert run.caps["paid_admission"]["reason"] == "OVERFLOW" and run.caps["max_cost_usd"] == 0.5
    ev = db.scalar(select(Event).where(Event.kind == "route.paid_admitted", Event.run_id == run.id))
    assert ev is not None and ev.payload["reason"] == "OVERFLOW" and ev.domain == run.domain
    au = db.scalar(select(AuditEvent).where(AuditEvent.action == "routing.route.paid_admitted"))
    assert au is not None and au.target_id == "worker.paid" and au.details["run_id"] == run.id


def test_a_free_route_lease_records_no_paid_admission(db, monkeypatch):
    from tests.unit.test_workforce_scheduler import _ready_env

    fakes = FakeModules().install(monkeypatch)
    _plan, _pv, specs = _ready_env(db, fakes, [{"task_key": "a"}])
    adm = scheduler.admission_check(db, specs[0].id)
    assert adm.admitted and "paid_admission" not in adm.answers
    run = scheduler.acquire_lease(db, specs[0].id, actor="system:scheduler", admission=adm)
    assert "paid_admission" not in run.caps
    assert db.scalar(select(Event).where(Event.kind == "route.paid_admitted")) is None


# ------------------------------------------------------------------ 7. invariance


def test_settings_default_on_and_identity_unchanged(routes, monkeypatch, settings):
    """The owner's default: both paths on. The worker prompt revision, the policy version and every seeded route's
    configuration hash equal origin/main's, and stay so through escalation and overflow selections."""
    from aeos.core import config as config_mod
    from aeos.worker_runtime import executor

    s = config_mod.get_settings()
    assert (s.paid_escalation, s.paid_overflow) == (True, True)

    def snapshot() -> tuple[str, str | None, dict[str, str]]:
        rows = routes.scalars(select(ModelRoute).where(ModelRoute.route_key.in_(list(GOLDEN_ROUTE_HASHES))))
        return (executor.worker_prompt_revision(), service.current_policy_version(),
                {r.route_key: service.compute_config_hash(r) for r in rows})

    assert snapshot() == (GOLDEN_PROMPT_REVISION, GOLDEN_POLICY_VERSION, GOLDEN_ROUTE_HASHES)
    install_fakes(monkeypatch)
    _go(routes, GO_GLM, GO_MINIMAX)
    _paid(routes)
    hashes = {k: service.get_route(routes, k).config_hash for k in (GO_GLM, GO_MINIMAX, PAID)}
    _run(routes, GO_MINIMAX)
    _run(routes, GO_GLM)
    assert select_route(routes, task=_task()).route.route_key == PAID
    _attempt(routes, GO_GLM, outcome="REVIEW_FINDINGS", verifier="FAIL")
    _attempt(routes, GO_GLM, outcome="REVIEW_FINDINGS", verifier="FAIL")
    assert select_route(routes, task=_task()).route.route_key == PAID
    after = {k: service.compute_config_hash(service.get_route(routes, k)) for k in hashes}
    assert after == hashes and all(service.get_route(routes, k).config_hash == h for k, h in hashes.items())
    assert snapshot()[:2] == (GOLDEN_PROMPT_REVISION, GOLDEN_POLICY_VERSION)
