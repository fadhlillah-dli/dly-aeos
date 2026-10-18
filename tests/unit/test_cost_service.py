"""Cost & quota service (master §14, §22, §42; §50 rows "Free quota habis / 429 -> tidak paid
fallback", "Budget DB/gateway tidak sehat -> admission ditolak", "Mock ... costs -> tidak menambah
usage actual")."""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest
from sqlalchemy import select

from aeos.core.db import session_factory
from aeos.core.errors import Blocked, Conflict, NotFound, ValidationFailed
from aeos.core.timeutil import utcnow
from aeos.cost import facade, service
from aeos.cost.ccusage import ccusage_estimate, parse_daily
from aeos.models.cost import Budget, BudgetReservation, UsageLedgerEntry
from aeos.models.decisions import Decision
from aeos.models.evidence import AcceptanceRecord
from aeos.models.portfolio import Project
from aeos.models.routing import ModelRoute
from aeos.models.workforce import WorkerRun
from tests.unit.test_cost_fakes import install_fake_policy, remove_policy


@pytest.fixture
def policy(monkeypatch):
    return install_fake_policy(monkeypatch)


def _routes(db):
    db.add(ModelRoute(route_key="free-flash", pool="WORKER", provider="opencode-zen", model_id="flash", is_free="YES"))
    db.add(ModelRoute(route_key="paid-pro", pool="WORKER", provider="openrouter", model_id="pro", is_free="NO",
                      pricing={"input_per_mtok": 1.0, "output_per_mtok": 2.0}, pricing_checked_at=utcnow()))
    db.add(ModelRoute(route_key="unknown-tier", pool="WORKER", provider="openrouter", model_id="x", is_free="UNKNOWN"))
    db.add(ModelRoute(route_key="paid-unpriced", pool="WORKER", provider="openrouter", model_id="y", is_free="NO"))
    db.commit()


def _req(run="r1", route="paid-pro", usd=0.25, tokens=100, project="p1", requests=1):
    return facade.ReservationRequest(run_id=run, project_id=project, domain="PERSONAL", route_key=route,
                                     est_usd=usd, est_tokens=tokens, est_requests=requests)


def _raise_global(db, usd=1.0, **kw):
    b = service.set_budget(db, actor_user_id="owner-1", scope_type="GLOBAL", scope_id="*", period="MONTH",
                           limit_usd=usd, rationale="owner raises worker budget", **kw)
    db.commit()
    return b


# ---------------------------------------------------------------- default $0 and admission


def test_default_global_budget_is_zero_hard_stop_free_routes_only(db):
    """§42: default worker cash budget $0 hard stop -> only free routes run."""
    _routes(db)
    r = facade.reserve(db, _req(route="free-flash", usd=0.0))
    db.commit()
    assert r.status == "RESERVED" and r.pricing_basis == "FREE_ROUTE"
    default = db.scalar(select(Budget).where(Budget.is_default.is_(True)))
    assert default is not None and Decimal(default.limit_usd) == 0 and default.hard_stop
    with pytest.raises(Blocked) as e:
        facade.reserve(db, _req(run="r2", route="paid-pro", usd=0.25))
    assert e.value.code == "BUDGET_EXCEEDED"
    db.rollback()


def test_paid_route_blocked_without_budget_decision_even_with_tiny_estimate(db):
    """§50 'Free quota habis / 429': no silent paid fallback; a paid route needs an owner BUDGET decision."""
    _routes(db)
    with pytest.raises(Blocked) as e:
        facade.reserve(db, _req(usd=0.000001))
    assert e.value.code == "BUDGET_EXCEEDED"
    db.rollback()
    assert db.scalar(select(BudgetReservation)) is None


def test_budget_raise_requires_recorded_budget_decision(db, policy):
    _routes(db)
    b = _raise_global(db, usd=1.0)
    d = db.get(Decision, b.decision_id)
    assert d.kind == "BUDGET" and d.target_hash == b.terms_hash and d.actor_kind == "HUMAN"
    old = db.scalar(select(Budget).where(Budget.is_default.is_(True)))
    assert old.status == "SUPERSEDED" and old.superseded_by == b.id
    r = facade.reserve(db, _req(usd=0.25))
    db.commit()
    assert r.pricing_basis == "ROUTE_PRICING" and b.id in r.budget_ids


def test_budget_change_fails_closed_without_decision_registry(db, monkeypatch):
    remove_policy(monkeypatch)
    with pytest.raises(Blocked) as e:
        service.set_budget(db, actor_user_id="owner", scope_type="GLOBAL", scope_id="*", period="MONTH",
                           limit_usd=5, rationale="raise")
    assert e.value.code == "DECISION_REGISTRY_UNAVAILABLE"
    db.rollback()
    assert db.scalar(select(Budget).where(Budget.limit_usd > 0)) is None


@pytest.mark.parametrize("bad", [{"limit_usd": -1}, {"limit_usd": float("nan")}, {"limit_usd": None}])
def test_set_budget_validates_limits(db, policy, bad):
    with pytest.raises(ValidationFailed):
        service.set_budget(db, actor_user_id="o", scope_type="GLOBAL", scope_id="*", period="MONTH",
                           rationale="r", **bad)


def test_all_applicable_budgets_checked_project_route_run(db, policy):
    _routes(db)
    _raise_global(db, usd=10)
    service.set_budget(db, actor_user_id="o", scope_type="PROJECT", scope_id="p1", period="MONTH", limit_usd=0.5,
                       rationale="project cap", project_id="p1", domain="PERSONAL")
    service.set_budget(db, actor_user_id="o", scope_type="ROUTE", scope_id="paid-pro", period="DAY",
                       limit_usd=None, limit_requests=2, rationale="route req cap")
    service.set_budget(db, actor_user_id="o", scope_type="RUN", scope_id="r9", period="TOTAL", limit_usd=None,
                       limit_tokens=50, rationale="run token cap")
    db.commit()
    facade.reserve(db, _req(run="r1", usd=0.3))
    db.commit()
    with pytest.raises(Blocked) as e:  # project cap 0.5 < 0.3 + 0.3
        facade.reserve(db, _req(run="r2", usd=0.3))
    assert "PROJECT:p1:MONTH" in " ".join(e.value.details["reasons"])
    db.rollback()
    facade.reserve(db, _req(run="r3", usd=0.1, project="p2"))
    db.commit()
    with pytest.raises(Blocked) as e:  # route request cap 2
        facade.reserve(db, _req(run="r4", usd=0.1, project="p3"))
    assert "ROUTE:paid-pro:DAY" in " ".join(e.value.details["reasons"])
    db.rollback()
    with pytest.raises(Blocked) as e:  # run token cap 50
        facade.reserve(db, _req(run="r9", route="free-flash", usd=0.0, tokens=60, project="p4"))
    assert "RUN:r9:TOTAL" in " ".join(e.value.details["reasons"])
    db.rollback()


def test_concurrent_reservations_never_exceed_limit(settings, policy):
    """§14: budget/reservation atomic; concurrency must not open a way past the limit."""
    s = session_factory()()
    _routes(s)
    _raise_global(s, usd=1.0)
    s.close()
    barrier = threading.Barrier(12)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        sess = session_factory()()
        try:
            barrier.wait(timeout=10)
            facade.reserve(sess, _req(run=f"run-{i}", usd=0.3, project=f"p{i}"))
            sess.commit()
            res = "ok"
        except Blocked as exc:
            sess.rollback()
            res = exc.code
        finally:
            sess.close()
        with lock:
            outcomes.append(res)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert len(outcomes) == 12
    assert outcomes.count("ok") == 3, outcomes
    assert set(outcomes) <= {"ok", "BUDGET_EXCEEDED", "BUDGET_LOCK_UNAVAILABLE"}
    s = session_factory()()
    total = sum(Decimal(r.reserved_usd) for r in s.scalars(select(BudgetReservation).where(BudgetReservation.status == "RESERVED")))
    s.close()
    assert total <= Decimal("1.0")


# ---------------------------------------------------------------- untrusted state -> refuse


def test_budget_without_decision_is_untrusted(db):
    _routes(db)
    db.add(Budget(scope_type="GLOBAL", scope_id="*", period="MONTH", limit_usd=100, hard_stop=True, status="ACTIVE"))
    db.commit()
    with pytest.raises(Blocked) as e:
        facade.reserve(db, _req(usd=0.1))
    assert e.value.code == "BUDGET_STATE_UNTRUSTED"
    db.rollback()


def test_tampered_budget_terms_are_untrusted(db, policy):
    _routes(db)
    b = _raise_global(db, usd=1.0)
    b.limit_usd = 1000  # edited outside the decision flow
    db.commit()
    with pytest.raises(Blocked) as e:
        facade.reserve(db, _req(usd=0.1))
    assert e.value.code == "BUDGET_STATE_UNTRUSTED"
    assert any("hash mismatch" in r for r in e.value.details["reasons"])
    db.rollback()


def test_invalidated_budget_decision_is_untrusted(db, policy):
    _routes(db)
    b = _raise_global(db, usd=1.0)
    db.get(Decision, b.decision_id).status = "INVALIDATED"
    db.commit()
    with pytest.raises(Blocked) as e:
        facade.reserve(db, _req(usd=0.1))
    assert e.value.code == "BUDGET_STATE_UNTRUSTED"
    db.rollback()


def test_duplicate_active_budgets_are_ambiguous(db, policy):
    _routes(db)
    b = _raise_global(db, usd=1.0)
    clone = Budget(scope_type=b.scope_type, scope_id=b.scope_id, period=b.period, limit_usd=b.limit_usd,
                   hard_stop=True, status="ACTIVE", decision_id=b.decision_id, terms_hash=b.terms_hash)
    db.add(clone)
    db.commit()
    with pytest.raises(Blocked) as e:
        facade.reserve(db, _req(usd=0.1))
    assert any("ambiguous" in r for r in e.value.details["reasons"])
    db.rollback()


@pytest.mark.parametrize("route,usd", [("unknown-tier", 0.1), ("paid-unpriced", 0.1), ("not-registered", 0.1),
                                        ("paid-pro", 0.0)])
def test_unknown_pricing_for_paid_route_refused(db, policy, route, usd):
    _routes(db)
    _raise_global(db, usd=100)
    with pytest.raises(Blocked) as e:
        facade.reserve(db, _req(route=route, usd=usd))
    assert e.value.code == "BUDGET_STATE_UNTRUSTED"
    db.rollback()


def test_unpriced_paid_usage_in_ledger_makes_state_untrusted(db, policy):
    _routes(db)
    _raise_global(db, usd=100)
    facade.record_usage(db, provider="openrouter", category="WORKER_API", source="broker", route_key="paid-pro",
                        cost_usd=None)
    db.commit()
    with pytest.raises(Blocked) as e:
        facade.reserve(db, _req(usd=0.1))
    assert e.value.code == "BUDGET_STATE_UNTRUSTED"
    db.rollback()


def test_exhausted_free_quota_blocks_admission_without_paid_fallback(db):
    """§50 'Free quota habis / 429 -> Backoff/queue/approved free fallback; tidak paid fallback'."""
    from datetime import timedelta

    _routes(db)
    service.set_quota_state(db, provider="opencode-zen", state="EXHAUSTED", source="observed-429",
                            resets_at=utcnow() + timedelta(hours=1))
    db.commit()
    with pytest.raises(Blocked) as e:
        facade.reserve(db, _req(route="free-flash", usd=0.0))
    assert e.value.code == "QUOTA_EXHAUSTED"
    db.rollback()
    assert db.scalar(select(BudgetReservation)) is None
    service.set_quota_state(db, provider="opencode-zen", state="EXHAUSTED", source="observed-429",
                            resets_at=utcnow() - timedelta(minutes=1))  # reset passed
    db.commit()
    assert facade.reserve(db, _req(route="free-flash", usd=0.0)).status == "RESERVED"


def test_invalid_request_amounts_rejected(db):
    for bad in (-1.0, float("inf"), float("nan")):
        with pytest.raises(ValidationFailed):
            facade.reserve(db, _req(usd=bad))
    with pytest.raises(ValidationFailed):
        facade.reserve(db, _req(tokens=-5))


# ---------------------------------------------------------------- release / commit


def test_release_frees_hold_and_is_idempotent(db, policy):
    _routes(db)
    _raise_global(db, usd=0.5)
    r1 = facade.reserve(db, _req(run="a", usd=0.4))
    db.commit()
    with pytest.raises(Blocked):
        facade.reserve(db, _req(run="b", usd=0.2))
    db.rollback()
    facade.release(db, r1.id)
    facade.release(db, r1.id)  # idempotent
    db.commit()
    assert db.get(BudgetReservation, r1.id).status == "RELEASED"
    facade.reserve(db, _req(run="b", usd=0.2))
    db.commit()
    with pytest.raises(NotFound):
        facade.release(db, "missing")


def test_record_usage_commits_reservation_actual_vs_estimate(db, policy):
    _routes(db)
    _raise_global(db, usd=1.0)
    r = facade.reserve(db, _req(run="a", usd=0.4, tokens=1000))
    db.commit()
    e = facade.record_usage(db, provider="openrouter", category="WORKER_API", source="provider", run_id="a",
                            reservation_id=r.id, input_tokens=300, output_tokens=200, cost_usd=0.12)
    db.commit()
    assert db.get(BudgetReservation, r.id).status == "COMMITTED"
    assert Decimal(e.cost_usd) == Decimal("0.12") and not e.is_estimate
    assert Decimal(db.get(BudgetReservation, r.id).committed_usd) == Decimal("0.12")
    with pytest.raises(Conflict):
        facade.record_usage(db, provider="openrouter", category="WORKER_API", source="provider", reservation_id=r.id)
    facade.release(db, r.id)  # no-op after commit
    assert db.get(BudgetReservation, r.id).status == "COMMITTED"
    budget = next(b for b in service.list_budgets(db, domain="PERSONAL", allowed_project_ids=None,
                                                  cross_domain_visible=True) if b["scope_type"] == "GLOBAL")
    assert budget["spent_usd"] == pytest.approx(0.12) and budget["held_usd"] == 0


def test_unknown_actual_cost_charges_reserved_estimate(db, policy):
    _routes(db)
    _raise_global(db, usd=1.0)
    r = facade.reserve(db, _req(run="a", usd=0.7))
    db.commit()
    e = facade.record_usage(db, provider="openrouter", category="WORKER_API", source="broker", reservation_id=r.id,
                            cost_usd=None)
    db.commit()
    assert e.cost_usd is None and Decimal(e.charged_usd) == Decimal("0.7")
    with pytest.raises(Blocked):  # 0.7 charged + 0.4 > 1.0 even though actual is UNKNOWN
        facade.reserve(db, _req(run="b", usd=0.4))
    db.rollback()
    assert service.ledger_view(e)["cost_kind"] == "UNKNOWN"


def test_overrun_is_recorded_and_blocks_further_paid_admission(db, policy):
    _routes(db)
    _raise_global(db, usd=1.0)
    r = facade.reserve(db, _req(run="a", usd=0.2))
    db.commit()
    e = facade.record_usage(db, provider="openrouter", category="WORKER_API", source="provider", reservation_id=r.id,
                            cost_usd=1.5)
    db.commit()
    assert e.details["overrun_usd"] == "1.300000"
    with pytest.raises(Blocked) as ex:
        facade.reserve(db, _req(run="b", usd=0.01))
    assert ex.value.code == "BUDGET_EXCEEDED"
    db.rollback()


def test_mock_usage_never_counts_as_actual_or_against_budget(db, policy):
    _routes(db)
    _raise_global(db, usd=1.0)
    facade.record_usage(db, provider="openrouter", category="WORKER_API", source="fixture", cost_usd=50, mode="MOCK",
                        domain="PERSONAL")
    db.commit()
    facade.reserve(db, _req(usd=0.9))
    db.commit()
    s = service.summary(db, domain="PERSONAL", allowed_project_ids=None, cross_domain_visible=True)
    assert s["totals"]["actual_usd"] == 0 and s["excluded_non_live"]["entries"] == 1


# ---------------------------------------------------------------- native Claude, categories, summary


def test_native_claude_usage_never_converted_to_dollars_or_percent(db):
    """§42: Claude tokens are not converted into dollars or % of remaining Max quota."""
    e = facade.record_usage(db, provider="claude-native", category="NATIVE_CLAUDE", source="bridge",
                            input_tokens=1000, output_tokens=500, cost_usd=3.21, is_estimate=True, domain="PERSONAL")
    db.commit()
    assert e.cost_usd is None and e.charged_usd is None and not e.is_estimate
    assert e.details["dollar_value_discarded"] is True and e.details["billing"] == "SUBSCRIPTION"
    s = service.summary(db, domain="PERSONAL", allowed_project_ids=None, cross_domain_visible=True)
    native = s["native_subscription"]
    assert native["dollar_cost"] is None and native["remaining_quota"] == "UNKNOWN"
    assert native["note"] == "Batas langganan berlaku; sisa kuota UNKNOWN"
    assert "percent" not in str(native).lower() and "%" not in native["note"]
    cat = next(c for c in s["by_category"] if c["category"] == "NATIVE_CLAUDE")
    assert cat["actual_usd"] is None and cat["estimate_usd"] is None and cat["billing"] == "SUBSCRIPTION"
    assert s["totals"]["actual_usd"] == 0 and s["totals"]["estimate_usd"] == 0 and s["totals"]["unknown_count"] == 0
    assert service.ledger_view(e)["cost_kind"] == "SUBSCRIPTION"


def test_native_quota_only_official_source_may_claim_remaining(db):
    with pytest.raises(ValidationFailed):
        service.set_quota_state(db, provider="claude-native", state="OK", source="ccusage")
    q = facade.set_quota_state(db, provider="claude-native", state="EXHAUSTED", source="observed-limit-message")
    db.commit()
    assert q.state == "EXHAUSTED"
    s = service.summary(db, domain="PERSONAL", allowed_project_ids=None, cross_domain_visible=True)
    assert s["native_subscription"]["remaining_quota"] == "EXHAUSTED"
    rows = service.list_quotas(db)
    assert rows[0]["provider"] == "claude-native" and rows[0]["source"] == "observed-limit-message"


def test_quota_list_shows_unknown_native_by_default(db):
    rows = service.list_quotas(db)
    assert rows[0]["provider"] == "claude-native" and rows[0]["state"] == "UNKNOWN" and rows[0]["implicit"]


def test_categories_kept_separate_and_actual_estimate_unknown_split(db):
    rec = facade.record_usage
    rec(db, provider="openrouter", category="WORKER_API", source="provider", cost_usd=0.5, domain="PERSONAL")
    rec(db, provider="openrouter", category="WORKER_API", source="estimator", cost_usd=0.25, is_estimate=True,
        domain="PERSONAL")
    rec(db, provider="openrouter", category="WORKER_API", source="broker", cost_usd=None, domain="PERSONAL")
    service.record_usage(db, provider="github-actions", category="CI", source="provider-ci", cost_usd=0.0,
                         quantity=12, unit="minutes", domain="PERSONAL")
    service.record_usage(db, provider="owner", category="HUMAN_TIME", source="manual", quantity=30,
                         unit="human_minutes", domain="PERSONAL")
    db.commit()
    s = service.summary(db, domain="PERSONAL", allowed_project_ids=None, cross_domain_visible=True)
    assert s["totals"]["actual_usd"] == pytest.approx(0.5)
    assert s["totals"]["estimate_usd"] == pytest.approx(0.25)
    assert s["totals"]["unknown_count"] == 2  # WORKER_API unknown + HUMAN_TIME (no dollar value)
    cats = {c["category"]: c for c in s["by_category"]}
    assert set(cats) == {"WORKER_API", "NATIVE_CLAUDE", "CI", "COMPUTE", "STORAGE", "HUMAN_TIME"}
    assert cats["CI"]["quantity"] == {"minutes": 12.0}
    assert cats["HUMAN_TIME"]["quantity"] == {"human_minutes": 30.0}
    with pytest.raises(ValidationFailed):
        rec(db, provider="x", category="BOGUS", source="x")


def test_summary_is_domain_scoped(db):
    db.add(Project(id="po", project_key="office-app", name="Office App", domain="OFFICE"))
    db.commit()
    facade.record_usage(db, provider="openrouter", category="WORKER_API", source="provider", cost_usd=9.0,
                        domain="OFFICE", project_id="po")
    facade.record_usage(db, provider="openrouter", category="WORKER_API", source="provider", cost_usd=1.0,
                        domain="PERSONAL")
    db.commit()
    s = service.summary(db, domain="PERSONAL", allowed_project_ids=None, cross_domain_visible=False)
    assert s["totals"]["actual_usd"] == pytest.approx(1.0)
    assert all(p["project_id"] != "po" for p in s["by_project"])
    glob = next(b for b in s["stop_caps"] if b["scope_type"] == "GLOBAL")
    assert glob["spent_usd"] is None and glob.get("spend_hidden")  # global spend includes the other domain
    ledger = service.list_ledger(db, domain="PERSONAL", allowed_project_ids=None)
    assert [e["cost_usd"] for e in ledger] == [1.0]


def test_cost_per_accepted_outcome_joins_acceptance_records(db):
    db.add(Project(id="pp", project_key="dly-aeos", name="DLY AEOS", domain="PERSONAL"))
    db.add(WorkerRun(id="run-a1", task_id="t1", project_id="pp", domain="PERSONAL"))
    db.add(WorkerRun(id="run-a2", task_id="t1", project_id="pp", domain="PERSONAL", attempt=2))
    db.add(AcceptanceRecord(task_id="t1", run_id="run-a2", project_id="pp", artifact_hash="sha256:x",
                            acceptance_status="ACCEPTED"))
    db.commit()
    for run, cost in (("run-a1", 0.3), ("run-a2", 0.2)):  # failed first attempt is real cost too
        facade.record_usage(db, provider="openrouter", category="WORKER_API", source="provider", run_id=run,
                            project_id="pp", domain="PERSONAL", cost_usd=cost)
    db.commit()
    out = service.summary(db, domain="PERSONAL", allowed_project_ids=None, cross_domain_visible=True)["per_accepted_outcome"]
    assert out["status"] == "COMPUTED" and out["accepted_outcomes"] == 1
    assert out["cost_per_accepted_usd"] == pytest.approx(0.5)
    assert out["per_task"][0]["cost_usd"] == pytest.approx(0.5)
    facade.record_usage(db, provider="openrouter", category="WORKER_API", source="broker", run_id="run-a2",
                        project_id="pp", domain="PERSONAL", cost_usd=None)
    db.commit()
    out = service.summary(db, domain="PERSONAL", allowed_project_ids=None, cross_domain_visible=True)["per_accepted_outcome"]
    assert out["status"] == "UNKNOWN" and out["cost_per_accepted_usd"] is None


def test_no_accepted_outcomes_reports_status_not_zero(db):
    out = service.summary(db, domain="PERSONAL", allowed_project_ids=None, cross_domain_visible=True)["per_accepted_outcome"]
    assert out["status"] == "NO_ACCEPTED_OUTCOMES" and out["cost_per_accepted_usd"] is None


def test_record_usage_redacts_details(db):
    e = facade.record_usage(db, provider="openrouter", category="WORKER_API", source="provider", cost_usd=0.1)
    e2 = service.record_usage(db, provider="openrouter", category="WORKER_API", source="provider", cost_usd=0.1,
                              details={"api_key": "sk-or-v1-" + "a" * 40, "note": "token=supersecretvalue"})
    db.commit()
    assert e.id and "supersecret" not in str(e2.details) and "sk-or-v1" not in str(e2.details)


# ---------------------------------------------------------------- ccusage estimator


CCUSAGE_JSON = """{"daily":[{"date":"2026-10-17","inputTokens":1200,"outputTokens":300,"cacheCreationTokens":10,
"cacheReadTokens":20,"totalTokens":1530,"totalCost":4.56,"modelsUsed":["claude-fable-5-1"]}],
"totals":{"inputTokens":1200,"outputTokens":300,"totalTokens":1530,"totalCost":4.56}}"""


def test_ccusage_parse_drops_dollar_figures():
    out = parse_daily(CCUSAGE_JSON)
    assert out["totals"]["total_tokens"] == 1530
    assert "4.56" not in str(out) and "cost" not in str(out).lower()


def test_ccusage_estimate_is_labelled_estimate_and_never_installs(monkeypatch):
    import subprocess

    calls = []

    def runner(cmd, timeout, env):
        calls.append(cmd)
        assert "--offline" in cmd and not any(k.upper().endswith("API_KEY") for k in env)
        return subprocess.CompletedProcess(cmd, 0, stdout=CCUSAGE_JSON, stderr="")

    out = ccusage_estimate(runner=runner, command=["ccusage"])
    assert out["status"] == "ESTIMATE" and out["mode"] == "ESTIMATE"
    assert "invoice" in out["authority"].lower() or "Bukan invoice" in out["authority"]
    assert "4.56" not in str(out)
    monkeypatch.setattr("aeos.cost.ccusage.find_command", lambda: None)
    na = ccusage_estimate()
    assert na["status"] == "NOT_AVAILABLE" and na["available"] is False


def test_ccusage_failure_reported_honestly():
    import subprocess

    def runner(cmd, timeout, env):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="npx: command not found ccusage")

    out = ccusage_estimate(runner=runner, command=["npx", "--no-install", "ccusage"])
    assert out["status"] == "FAILED" and "ccusage" in out["reason"]


@pytest.mark.integration
def test_ccusage_real_binary_when_present():
    import shutil

    if shutil.which("ccusage") is None:
        pytest.skip("ccusage not installed (never installed automatically)")
    out = ccusage_estimate(command=[shutil.which("ccusage")])
    assert out["status"] in ("ESTIMATE", "FAILED") and out["mode"] == "ESTIMATE"


def test_ledger_entries_are_not_merged_across_reservations(db, policy):
    _routes(db)
    _raise_global(db, usd=2)
    r = facade.reserve(db, _req(run="a", usd=0.5))
    db.commit()
    facade.record_usage(db, provider="openrouter", category="WORKER_API", source="provider", reservation_id=r.id,
                        cost_usd=0.5)
    db.commit()
    rows = list(db.scalars(select(UsageLedgerEntry)))
    assert len(rows) == 1 and rows[0].reservation_id == r.id and rows[0].run_id == "a"
